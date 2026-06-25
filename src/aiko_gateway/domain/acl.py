"""Membership ACL — invariant I2 (plan §A3).

The read/subscribe/post trust boundary. Access semantics (Phase 2):

  * PUBLIC channels (``is_private=False``): open to every authenticated user for
    reading/subscribing. Posting is also open by default, BUT an explicit
    ``Membership`` row with ``can_post=False`` mutes that user on the channel
    (absence of a row = open; presence with ``can_post=True`` = open;
    presence with ``can_post=False`` = muted).
  * PRIVATE channels (``is_private=True``): require an explicit ``Membership``
    row to read/subscribe; posting additionally needs ``Membership.can_post``.

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

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Channel, Membership


def _readable_predicate(user_id: str):
    """SQL predicate for "this Channel is readable by user_id": public, OR a
    membership row for this user exists. A correlated EXISTS so it composes into
    a single statement (no per-channel membership round-trip)."""
    member = exists().where(
        (Membership.channel_id == Channel.id) & (Membership.user_id == user_id)
    )
    return Channel.is_private.is_(False) | member


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


async def readable_channel(
    session: AsyncSession, user_id: str, channel_id: str
) -> Channel | None:
    """The channel iff the user may read/subscribe to it, else None — in ONE query.

    Existence AND access are resolved in a single statement, so "no such channel"
    and "private channel you're not a member of" do IDENTICAL database work and
    both collapse to ``None``. That closes the timing/query-shape oracle a
    two-step (lookup-then-membership) check leaks: an attacker probing ids cannot
    distinguish "missing" from "exists but private" by latency. Callers map the
    single ``None`` to their existence-hiding response (REST 404 / WS no_channel).
    """
    return (
        await session.execute(
            select(Channel).where(
                Channel.id == channel_id, _readable_predicate(user_id)
            )
        )
    ).scalar_one_or_none()


async def can_post(session: AsyncSession, user_id: str, channel: Channel) -> bool:
    """Post access: honouring ``Membership.can_post`` on both public and private channels.

    * PUBLIC: open by default; an explicit ``Membership`` row with ``can_post=False``
      mutes the user.  Absence of a row means open (public default preserved).
    * PRIVATE: requires a ``Membership`` row with ``can_post=True``.

    Only called AFTER ``readable_channel`` has confirmed access, so it never
    leaks existence (the caller already knows the channel is real)."""
    m = await _membership(session, channel.id, user_id)
    if not channel.is_private:
        return m is None or m.can_post  # public: open by default, explicit row can mute
    return m is not None and m.can_post  # private: needs a row with can_post


async def visible_channels(session: AsyncSession, user_id: str) -> list[Channel]:
    """Every public channel + every private channel the user belongs to, id asc."""
    rows = (
        await session.execute(
            select(Channel).where(_readable_predicate(user_id)).order_by(Channel.id)
        )
    ).scalars()
    return list(rows)


async def filter_readable_ids(
    session: AsyncSession, user_id: str, channel_ids: list[str]
) -> list[str]:
    """The subset of ``channel_ids`` the user may read/subscribe to, order preserved.

    Unknown ids and private channels the user is not a member of are silently
    dropped (existence-hiding: the WS suback simply omits them). Resolved in ONE
    query — subscribe is attacker-controlled input, so a per-channel membership
    loop would be avoidable DB amplification (and another timing oracle).
    """
    if not channel_ids:
        return []
    readable = set(
        (
            await session.execute(
                select(Channel.id).where(
                    Channel.id.in_(channel_ids), _readable_predicate(user_id)
                )
            )
        ).scalars()
    )
    return [cid for cid in channel_ids if cid in readable]  # preserve request order
