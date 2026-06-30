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
caller surfaces which channels to hand over or leave first. Admin-transfer on
delete is a deliberate follow-up, not part of this MVP.

Commit convention follows `users_service` (service-owns-commit), not
`channels_service` (caller-owns-commit): the auth/account routes stay uniformly
commit-free.
"""
from __future__ import annotations

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import devices_service, moderation_service
from .memberships_service import ROLE_ADMIN
from .models import Membership, Message, SocialIdentity, User

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


async def _sole_admin_channel_ids(session: AsyncSession, user_id: str) -> list[str]:
    """Channel ids where `user_id` is an admin AND the only admin.

    CONCURRENCY (flagged by all three cage-match reviewers): this is a
    read-then-write business invariant with NO row lock, so it is not atomic.
    Under the current single-writer SQLite deployment the race window is small,
    but two co-admins of the same channel deleting concurrently can each observe
    the other as the "second admin" and both proceed — orphaning the channel.
    Moving to Postgres (planned on the public-scale / external-testers path)
    widens the window. The proper fix is `SELECT … FOR UPDATE` on the channel's
    admin memberships before the guard (or a DB-level invariant); tracked
    separately. Accepted for the MVP because the precondition (two simultaneous
    sole-co-admin deletes) is rare and the blast radius is a recoverable
    admin-less channel, not data loss.
    """
    admin_channels = (await session.execute(
        select(Membership.channel_id).where(
            Membership.user_id == user_id, Membership.role == ROLE_ADMIN)
    )).scalars().all()
    sole: list[str] = []
    for cid in admin_channels:
        admin_count = (await session.execute(
            select(func.count()).select_from(Membership).where(
                Membership.channel_id == cid, Membership.role == ROLE_ADMIN)
        )).scalar_one()
        if admin_count <= 1:
            sole.append(cid)
    return sole


async def delete_user_account(session: AsyncSession, user_id: str) -> None:
    """IRREVERSIBLE: anonymize the user's messages, delete their social
    identities + memberships + user row, and commit — all in one transaction.

    Raises `CannotDeleteSoleAdmin` (before performing ANY write) if the user is
    the only admin of any channel.
    """
    sole = await _sole_admin_channel_ids(session, user_id)
    if sole:
        raise CannotDeleteSoleAdmin(sole)

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
    # Remove federated-identity links and channel memberships (children first).
    await session.execute(
        delete(SocialIdentity).where(SocialIdentity.user_id == user_id))
    await session.execute(
        delete(Membership).where(Membership.user_id == user_id))
    # Finally the account row itself.
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()
