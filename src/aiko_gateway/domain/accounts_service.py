"""Account deletion — irreversible application-level cascade (Apple 5.1.1(v)).

Tears down the authenticated user's account in ONE transaction. Children are
removed before the parent so the result is correct whether or not SQLite FK
enforcement is on — this codebase never relies on `ON DELETE CASCADE` (cf.
`channels_service.hard_delete_channel`).

**Message handling is TOMBSTONE, not delete.** A chat message lives in a shared
conversation; hard-deleting a departing user's messages would shred *other*
participants' history and gap their ULID-ordered timelines. So a deleted user's
messages stay in place as tombstones — the row/slot survives, but every part
that carries the person is destroyed: `sender_user_id → NULL` (already a
first-class state, used for non-gateway bus actors), `sender_label → "[deleted
user]"`, and BOTH free-text vectors — `body → "[deleted]"` and the 64-char
client-supplied `client_msg_id → NULL`. Wiping these matters because they are
unstructured PII ("I'm Nick, call me on 0400…"); a `*_id` column whose value is
attacker-controlled input is no less sensitive for being named an id. Leaving
either would contradict the live privacy policy (imagineering.cc/aiko/privacy §5:
"your account and associated message data are deleted"). The account-identifying
rows — the user and its federated identities — are hard-deleted. Net: the
conversation slot endures, the content and every link back to a person do not.

**Sole-admin guard.** Deletion is refused (`CannotDeleteSoleAdmin`) if the user
is the only admin of any channel, so a channel is never left admin-less; the
caller surfaces which channels to hand over or leave first. The guard is enforced
ATOMICALLY with the removal — the user's admin memberships are dropped by a
conditional ``admins_remaining > 1`` DELETE under a FOR UPDATE lock (the same
dual-engine mechanism as ``memberships_service._delete_membership_unless_last_admin``),
so two co-admins deleting concurrently can never both slip past a stale read and
orphan the channel (#1583). Admin-transfer on delete is a deliberate follow-up,
not part of this MVP.

Commit convention follows `users_service` (service-owns-commit), not
`channels_service` (caller-owns-commit): the auth/account routes stay uniformly
commit-free.
"""
from __future__ import annotations

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import (
    devices_service, moderation_service, passkey_service, recovery_service,
    signing_keys_service)
from .memberships_service import ROLE_ADMIN
from .models import (
    Community, CommunityMembership, Membership, Message, SocialIdentity, User)

# What a tombstoned message's author label becomes once the account is gone.
DELETED_USER_LABEL = "[deleted user]"
# What a tombstoned message's body becomes — the free-text PII is destroyed while
# the row/slot survives. `Message.body` is Text NOT NULL, so this is a non-empty
# sentinel, never NULL. Makes the live privacy policy (§5 "message data are
# deleted") literally true without gapping co-participants' timelines.
DELETED_BODY = "[deleted]"


class CannotDeleteSoleAdmin(Exception):
    """The user is the sole admin of one or more channels; deleting them would
    orphan those channels. Carries the channel ids so the caller can tell the
    user exactly which channels to transfer or leave first."""

    def __init__(self, channel_ids: list[str]) -> None:
        self.channel_ids = channel_ids
        super().__init__(f"sole admin of channels: {channel_ids}")


async def _remove_admin_memberships_or_refuse(
    session: AsyncSession, user_id: str
) -> None:
    """Delete every ADMIN membership of `user_id`, REFUSING the whole account
    deletion (`CannotDeleteSoleAdmin`) if removing any would leave a channel with
    no admin.

    Authoritative and correct on BOTH engines, by the SAME dual mechanism as
    ``memberships_service._delete_membership_unless_last_admin`` — a mechanism a
    pure read-then-check guard could not achieve on SQLite, which is why the old
    `_sole_admin_channel_ids` read had a TOCTOU (two co-admins deleting
    concurrently each saw the other and both proceeded, orphaning the channel — #1583):

      * ``SELECT … FOR UPDATE`` locks a channel's admin set — serialises a
        concurrent co-admin's account deletion on Postgres (the second blocks, then
        re-reads the post-commit count). Inert on SQLite (no row locks).
      * a conditional ``admins_remaining > 1`` DELETE, the count re-evaluated
        INSIDE the atomic statement under SQLite's single-writer lock — the loser
        sees count == 1 and removes 0 rows. FOR UPDATE alone is inert on SQLite (the
        prod engine, #1281); the conditional DELETE alone is inert on Postgres READ
        COMMITTED (two DELETEs of different rows don't conflict). Neither suffices
        without the other.

    Channels are processed in sorted id order so two concurrent deletions sharing
    several channels acquire the per-channel locks in the SAME order — no
    CROSS-channel lock inversion (AB/BA). Intra-channel row-lock ordering is left
    to Postgres' scan order, as in memberships_service. Refused channels are
    collected so the 409 can name all of them. (Postgres FOR UPDATE behaviour is
    reasoned from the mirrored, SQLite-tested memberships_service pattern; the tests
    here exercise the prod engine, SQLite.)
    """
    admin_channels = sorted((await session.execute(
        select(Membership.channel_id).where(
            Membership.user_id == user_id, Membership.role == ROLE_ADMIN)
    )).scalars().all())
    refused: list[str] = []
    for cid in admin_channels:
        # Lock this channel's admin set (Postgres); no-op on SQLite.
        await session.execute(
            select(Membership.user_id).where(
                Membership.channel_id == cid, Membership.role == ROLE_ADMIN)
            .with_for_update())
        admins_remaining = (
            select(func.count()).select_from(Membership).where(
                Membership.channel_id == cid, Membership.role == ROLE_ADMIN)
            .scalar_subquery())
        # Remove this user's admin row only while MORE THAN ONE admin remains — the
        # count re-evaluated atomically with the delete (closes the SQLite TOCTOU).
        result = await session.execute(
            delete(Membership).where(
                Membership.channel_id == cid,
                Membership.user_id == user_id,
                Membership.role == ROLE_ADMIN,
                admins_remaining > 1))
        if result.rowcount == 0:
            # Disambiguate a 0-row delete: a row STILL PRESENT is a genuine
            # sole-admin refusal; one already GONE (a concurrent removal) is fine
            # (mirrors _delete_membership_unless_last_admin, PR#29).
            still_present = (await session.execute(
                select(Membership.user_id).where(
                    Membership.channel_id == cid,
                    Membership.user_id == user_id,
                    Membership.role == ROLE_ADMIN))).scalar_one_or_none()
            if still_present is not None:
                refused.append(cid)
    if refused:
        raise CannotDeleteSoleAdmin(refused)


async def delete_user_account(session: AsyncSession, user_id: str) -> None:
    """IRREVERSIBLE: tombstone the user's messages (wipe body + client_msg_id,
    sever the account link), tear down every other child of `users` —
    moderation rows (blocks deleted, reports anonymized), device tokens, passkey
    credentials, social identities, memberships — then delete the user row, and
    commit — all in one transaction (children-before-parent; no ON DELETE CASCADE).

    Raises `CannotDeleteSoleAdmin` if removing the user's admin memberships would
    leave any channel admin-less. On refusal the account row is left intact; any
    admin memberships already dropped for OTHER channels in the same call are
    uncommitted and rolled back by the caller. Enforced atomically — see the
    module docstring.
    """
    # Drop the user's ADMIN memberships FIRST, refusing the whole deletion if any
    # channel would be orphaned — atomic on both engines (closes the #1583 TOCTOU).
    # A refusal raises here, before the irreversible tombstoning below.
    await _remove_admin_memberships_or_refuse(session, user_id)

    # Tombstone authored messages: keep the conversation slot, but destroy every
    # part that carries the person. Two free-text vectors, not one: the `body`,
    # and `client_msg_id` — a 64-char client-supplied string (validated only as
    # "a string", envelopes.py) that can hold an email/phone/handle. Naming a
    # column `*_id` does not make attacker-controlled input non-PII. Nulling it is
    # safe: the channel/client_msg_id idempotency it enables is moot for a
    # now-gone user, and the UNIQUE(channel_id, client_msg_id) treats NULLs as
    # distinct (SQLite + Postgres), so many tombstones can share NULL.
    # Scoped to this user's messages only — co-participants' rows are untouched.
    await session.execute(
        update(Message)
        .where(Message.sender_user_id == user_id)
        .values(sender_user_id=None, sender_label=DELETED_USER_LABEL,
                body=DELETED_BODY, client_msg_id=None))
    # Moderation footprint (#7): delete this user's blocks (either direction) and
    # anonymize their reports (reporter → NULL, audit trail kept). Both are FK
    # children of `users`, so they must go before the user row — the SAME
    # children-before-parent discipline as memberships/identities. Without this
    # the final User delete would FK-violate on the user_blocks / message_reports
    # rows (verify-the-neighbor: the just-shipped deletion cascade must learn
    # about every new table that references users).
    await moderation_service.purge_user_moderation_rows(session, user_id)
    # Push-notification tokens (#16) are another FK child of `users` — they must
    # go before the user row too (verify-the-neighbor: every new users-referencing
    # table must join this cascade, exactly as the moderation rows above did).
    await devices_service.purge_user_devices(session, user_id)
    # Passkey credentials (#27) are the same shape — a non-null FK child of `users`.
    # The passkey ship added this table and the cascade has to learn about it, or a
    # passkey holder's deletion leaves an orphaned credential row (FK off) / FK-
    # violates the final User delete (FK on / Postgres). Same verify-the-neighbor.
    await passkey_service.purge_user_credentials(session, user_id)
    # Signing-key bindings (#1816 PR B) are another FK child of `users` — a personal
    # pubkey->account observation, hard-deleted with the account (verify-the-neighbor:
    # every new users-referencing table must join this cascade, exactly as the
    # passkey/device/moderation rows above did). The cascade guard now requires it.
    await signing_keys_service.purge_user_keys(session, user_id)
    # Social-recovery footprint (Design 05) — THREE FK children of `users`:
    # pending_recovery, recovery_approvers, recovery_policies. All hard-deleted with
    # the account (verify-the-neighbor: every new users-referencing table must join
    # this cascade, exactly as the passkey/signing/device/moderation rows above did).
    # The cascade guard now requires all three. NOTE: finalize (the one flow that
    # mints a session for a not-previously-authed party) works on a separate row that
    # is torn down here too, so a deleted account can never be recovered afterward.
    await recovery_service.purge_user_recovery(session, user_id)
    # Community footprint (#32). TWO FK children of `users`, handled differently —
    # the same verify-the-neighbor discipline, and BOTH are now required by the
    # cascade guard:
    #   * community_memberships.user_id — DELETE the user's community memberships
    #     (a child row, like channel memberships below).
    #   * communities.owner_id — ANONYMIZE (owner_id -> NULL), do NOT delete the
    #     community. A community is shared infrastructure like a channel; deleting
    #     it on the owner's departure would strip every other member (the same
    #     tombstone-not-shred reasoning as the message body above). The seeded Aiko
    #     community is already system-owned (NULL) and is untouched.
    # Decrement the denormalized member_count of every community the user belongs
    # to BEFORE removing their membership rows — otherwise the count drifts on
    # account deletion (invisible in B1 since nothing reads it yet, but account
    # deletion is live, so keep the projection honest rather than ship a knowingly
    # stale counter; B2's join/leave own the increment side).
    await session.execute(
        update(Community)
        .where(Community.id.in_(
            select(CommunityMembership.community_id)
            .where(CommunityMembership.user_id == user_id)))
        .values(member_count=Community.member_count - 1))
    await session.execute(
        delete(CommunityMembership).where(CommunityMembership.user_id == user_id))
    await session.execute(
        update(Community).where(Community.owner_id == user_id)
        .values(owner_id=None))
    # Remove federated-identity links and the REMAINING (non-admin) channel
    # memberships — the admin ones were already dropped by the sole-admin guard
    # above (children first).
    await session.execute(
        delete(SocialIdentity).where(SocialIdentity.user_id == user_id))
    await session.execute(
        delete(Membership).where(Membership.user_id == user_id))
    # Finally the account row itself.
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()
