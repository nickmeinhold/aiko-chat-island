"""Account deletion — irreversible application-level cascade (Apple 5.1.1(v)).

Tears down the authenticated user's account in ONE transaction. Children are
removed before the parent so the result is correct whether or not SQLite FK
enforcement is on — this codebase never relies on `ON DELETE CASCADE` (cf.
`channels_service.hard_delete_channel`).

**Message handling is ANONYMIZE, not delete.** A chat message lives in a shared
conversation; hard-deleting a departing user's messages would shred *other*
participants' history. So a deleted user's messages stay in place but are
unlinked from any account: `sender_user_id → NULL` (already a first-class state,
used for non-gateway bus actors) and `sender_label → "[deleted user]"`. The
account-identifying PII — the user row and its federated identities — is
hard-deleted, so nothing links the surviving message bodies back to a person.

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

from .models import Membership, Message, SocialIdentity, User

# Wire value of an admin membership role (models.py: role is `member|admin`).
_ROLE_ADMIN = "admin"

# What an anonymized message's author label becomes once the account is gone.
DELETED_USER_LABEL = "[deleted user]"


class CannotDeleteSoleAdmin(Exception):
    """The user is the sole admin of one or more channels; deleting them would
    orphan those channels. Carries the channel ids so the caller can tell the
    user exactly which channels to transfer or leave first."""

    def __init__(self, channel_ids: list[str]) -> None:
        self.channel_ids = channel_ids
        super().__init__(f"sole admin of channels: {channel_ids}")


async def _sole_admin_channel_ids(session: AsyncSession, user_id: str) -> list[str]:
    """Channel ids where `user_id` is an admin AND the only admin.

    Note (MVP tradeoff): the check-then-delete is not wrapped in row locks, so a
    concurrent admin change could in principle race it. SQLite serialises writers
    so the window is closed in practice; revisit with `SELECT … FOR UPDATE` when
    the store moves to Postgres.
    """
    admin_channels = (await session.execute(
        select(Membership.channel_id).where(
            Membership.user_id == user_id, Membership.role == _ROLE_ADMIN)
    )).scalars().all()
    sole: list[str] = []
    for cid in admin_channels:
        admin_count = (await session.execute(
            select(func.count()).select_from(Membership).where(
                Membership.channel_id == cid, Membership.role == _ROLE_ADMIN)
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

    # Anonymize authored messages: keep the conversation, drop the account link
    # and the human's name.
    await session.execute(
        update(Message)
        .where(Message.sender_user_id == user_id)
        .values(sender_user_id=None, sender_label=DELETED_USER_LABEL))
    # Remove federated-identity links and channel memberships (children first).
    await session.execute(
        delete(SocialIdentity).where(SocialIdentity.user_id == user_id))
    await session.execute(
        delete(Membership).where(Membership.user_id == user_id))
    # Finally the account row itself.
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()
