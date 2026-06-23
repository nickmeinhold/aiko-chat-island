"""Membership ACL — invariant I2 (plan §A3).

The read/subscribe/post trust boundary. Access semantics (Phase 2):

  * PUBLIC channels (``is_private=False``): open to every authenticated user.
  * PRIVATE channels (``is_private=True``): require an explicit ``Membership`` row.

Posting additionally honours ``Membership.can_post`` on private channels.

EXISTENCE-HIDING (explicit design call, #36): callers collapse "private channel
you are not a member of" into the SAME response as "no such channel" (REST 404 /
WS ``no_channel``) so the boundary never confirms a private channel exists to a
non-member. A member who merely lacks ``can_post`` already knows the channel
exists, so that path returns an honest ``forbidden`` (no leak).

Membership is read LIVE from the DB on every request — never trusted from the
JWT — so a revoked membership takes effect immediately, not at next token
refresh (plan §A3). This module is the single enforcement source; the REST and
WS call sites delegate here so the rule can never drift between them.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Channel, Membership


async def _membership(
    session: AsyncSession, channel_id: str, user_id: str
) -> Membership | None:
    return (
        await session.execute(
            select(Membership).where(
                Membership.channel_id == channel_id,
                Membership.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def can_read(session: AsyncSession, user_id: str, channel: Channel) -> bool:
    """Read/subscribe access: public channels open to all; private need membership."""
    if not channel.is_private:
        return True
    return await _membership(session, channel.id, user_id) is not None


async def can_post(session: AsyncSession, user_id: str, channel: Channel) -> bool:
    """Post access: public open to all; private needs a membership with can_post."""
    if not channel.is_private:
        return True
    m = await _membership(session, channel.id, user_id)
    return m is not None and m.can_post


async def visible_channels(session: AsyncSession, user_id: str) -> list[Channel]:
    """Every public channel + every private channel the user belongs to, id asc."""
    member_cids = select(Membership.channel_id).where(Membership.user_id == user_id)
    rows = (
        await session.execute(
            select(Channel)
            .where(Channel.is_private.is_(False) | Channel.id.in_(member_cids))
            .order_by(Channel.id)
        )
    ).scalars()
    return list(rows)


async def filter_readable_ids(
    session: AsyncSession, user_id: str, channel_ids: list[str]
) -> list[str]:
    """The subset of ``channel_ids`` the user may read/subscribe to, order preserved.

    Unknown ids and private channels the user is not a member of are silently
    dropped (existence-hiding: the WS suback simply omits them). Public channels
    short-circuit without a membership query.
    """
    if not channel_ids:
        return []
    found = {
        c.id: c
        for c in (
            await session.execute(select(Channel).where(Channel.id.in_(channel_ids)))
        ).scalars()
    }
    out: list[str] = []
    for cid in channel_ids:
        channel = found.get(cid)
        if channel is not None and await can_read(session, user_id, channel):
            out.append(cid)
    return out
