"""Membership management — the WRITE half of the I2 trust boundary (#46).

`acl.py` ENFORCES membership on reads/posts but has no way to CREATE memberships
(before this, the only path was seeding DB rows, so private channels were inert).
This module is the single source for membership *mutations*, mirroring how
``acl`` is the single source for membership *reads* — the REST router delegates
here so the rules can never drift between call sites.

The trust boundary, enumerated as the bypass cases each function must reject:

  (a) non-admin adding/removing others        -> NotChannelAdmin
  (b) self-join to an invite_only channel     -> ChannelNotFound (existence-hiding)
  (c) non-member enumerating a private members -> ChannelNotFound (existence-hiding)
  (d) removing the last admin of a channel     -> LastAdmin (forbidden — never orphan)
  (e) double-join / double-add                 -> idempotent no-op (returns existing)
  (f) leaving a channel you are not in         -> NotAMember

EXISTENCE-HIDING (inherited from #36, see acl.py): every operation a non-member
must not be able to probe (self-join, members list) collapses "private channel
you can't see" into the SAME ``ChannelNotFound`` the caller maps to 404 — so the
boundary never confirms a private channel exists to someone outside it. We reuse
``acl.readable_channel`` for that single-query existence+access resolution so the
hiding can't drift from the read path.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import acl
from .ids import new_ulid
from .models import Channel, Membership

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"

JOIN_OPEN = "open"
JOIN_INVITE_ONLY = "invite_only"


class MembershipError(Exception):
    """Base for membership-mutation rejections. The REST layer maps each
    subtype to an HTTP status (existence-hiding errors -> 404)."""


class ChannelNotFound(MembershipError):
    """The channel does not exist OR is private and the caller may not see it.
    The two are deliberately indistinguishable (existence-hiding)."""


class NotChannelAdmin(MembershipError):
    """The caller is not an admin of this channel (cannot add/remove others)."""


class NotAMember(MembershipError):
    """The target user is not a member (e.g. leaving/removing a non-member)."""


class SelfJoinNotAllowed(MembershipError):
    """Self-join attempted on a channel whose policy forbids it. NOTE: callers
    must NOT surface this for a channel the user cannot see — that path raises
    ChannelNotFound instead, so invite_only is hidden behind a 404."""


class LastAdmin(MembershipError):
    """Refused: the operation would remove the channel's last admin, orphaning
    it (no one could ever manage membership again)."""


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


async def _admin_count(session: AsyncSession, channel_id: str) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.channel_id == channel_id,
                Membership.role == ROLE_ADMIN,
            )
        )
    ).scalar_one()


async def _require_admin(
    session: AsyncSession, channel_id: str, actor_id: str
) -> Channel:
    """Resolve the channel for an ADMIN-only operation, or raise.

    Existence-hiding: an actor who cannot even READ the channel gets
    ``ChannelNotFound`` (same as nonexistent) — never a signal it exists. A
    member who is not an admin gets ``NotChannelAdmin`` (they already know it
    exists, so that's an honest forbidden, no leak)."""
    channel = await acl.readable_channel(session, actor_id, channel_id)
    if channel is None:
        raise ChannelNotFound(channel_id)
    actor = await _membership(session, channel_id, actor_id)
    # A public channel readable to all still has no admins unless seeded; admin
    # ops require an explicit admin membership row regardless of privacy.
    if actor is None or actor.role != ROLE_ADMIN:
        raise NotChannelAdmin(channel_id)
    return channel


async def add_member(
    session: AsyncSession,
    *,
    channel_id: str,
    actor_id: str,
    target_user_id: str,
    role: str = ROLE_MEMBER,
    can_post: bool = True,
) -> Membership:
    """Admin adds ``target_user_id`` to a channel. Idempotent: re-adding an
    existing member returns the existing row unchanged (case (e)).

    Rejects: non-admin actor (a, via _require_admin), unseeable channel (404)."""
    await _require_admin(session, channel_id, actor_id)
    existing = await _membership(session, channel_id, target_user_id)
    if existing is not None:
        return existing  # idempotent — do not silently flip role/can_post
    m = Membership(
        channel_id=channel_id,
        user_id=target_user_id,
        role=role if role in (ROLE_ADMIN, ROLE_MEMBER) else ROLE_MEMBER,
        can_post=can_post,
    )
    session.add(m)
    await session.commit()
    return m


async def self_join(
    session: AsyncSession, *, channel_id: str, actor_id: str
) -> Membership:
    """A user self-joins a channel. Allowed only when the channel is visible to
    them AND its policy permits self-join.

    EXISTENCE-HIDING (case (b)): a non-member of an invite_only PRIVATE channel
    cannot tell "exists but invite_only" from "doesn't exist" — both raise
    ``ChannelNotFound``. We must therefore resolve visibility-as-a-non-member
    BEFORE consulting the policy: if the channel is private and they have no
    membership, it is invisible, so 404 regardless of policy.

    Idempotent (case (e)): already a member -> returns the existing row."""
    # Load the raw channel directly (not via readable_channel) because for a
    # private channel a non-member's readable_channel is None — but we need to
    # branch on the policy. We reconstruct the hiding explicitly below.
    channel = await session.get(Channel, channel_id)
    existing = await _membership(session, channel_id, actor_id)
    if existing is not None:
        return existing  # already in — idempotent self-join

    if channel is None:
        raise ChannelNotFound(channel_id)

    if channel.is_private:
        # A non-member of a private channel must not learn it exists. An OPEN
        # private channel is the one case a non-member is *allowed* to join, so
        # only that policy escapes the 404; invite_only collapses to not-found.
        if channel.join_policy != JOIN_OPEN:
            raise ChannelNotFound(channel_id)
    # Public channels: joining is harmless (reads are open anyway) and lets a
    # user opt into an explicit membership row; allowed for any policy.

    m = Membership(
        channel_id=channel_id,
        user_id=actor_id,
        role=ROLE_MEMBER,
        can_post=True,
    )
    session.add(m)
    await session.commit()
    return m


async def remove_member(
    session: AsyncSession, *, channel_id: str, actor_id: str, target_user_id: str
) -> None:
    """Admin removes ``target_user_id``. Rejects: non-admin (a), unseeable
    channel (404), removing a non-member (f), and removing the last admin (d)."""
    await _require_admin(session, channel_id, actor_id)
    target = await _membership(session, channel_id, target_user_id)
    if target is None:
        raise NotAMember(target_user_id)
    if target.role == ROLE_ADMIN and await _admin_count(session, channel_id) <= 1:
        raise LastAdmin(channel_id)
    await session.delete(target)
    await session.commit()


async def leave(
    session: AsyncSession, *, channel_id: str, actor_id: str
) -> None:
    """A user removes their own membership.

    Rejects: not a member (f). Refuses to orphan the channel — leaving as the
    last admin raises ``LastAdmin`` (d), same invariant as an admin removing
    the last admin. Note this does not leak existence: a non-member who was
    never in the channel gets NotAMember, which the REST layer also maps to 404
    (so a private channel they can't see still answers 404)."""
    mine = await _membership(session, channel_id, actor_id)
    if mine is None:
        raise NotAMember(actor_id)
    if mine.role == ROLE_ADMIN and await _admin_count(session, channel_id) <= 1:
        raise LastAdmin(channel_id)
    await session.delete(mine)
    await session.commit()


async def list_members(
    session: AsyncSession, *, channel_id: str, actor_id: str
) -> list[Membership]:
    """Members of a channel, for a caller who may see it.

    EXISTENCE-HIDING (case (c)): a non-member of a private channel gets
    ``ChannelNotFound`` (404) — the members list of a private channel is
    invisible to outsiders, indistinguishable from a nonexistent channel. We
    gate on ``acl.readable_channel`` (the same single-query existence+access
    check the read path uses) so the hiding can't drift."""
    channel = await acl.readable_channel(session, actor_id, channel_id)
    if channel is None:
        raise ChannelNotFound(channel_id)
    rows = (
        await session.execute(
            select(Membership)
            .where(Membership.channel_id == channel_id)
            .order_by(Membership.user_id)
        )
    ).scalars()
    return list(rows)


async def create_channel(
    session: AsyncSession,
    *,
    creator_id: str,
    name: str,
    is_private: bool,
    join_policy: str = JOIN_INVITE_ONLY,
    kind: str = "standard",
    aiko_channel: str | None = None,
) -> Channel:
    """Create a channel and auto-add the creator as an admin member (#46).

    The creator becomes ``role=admin`` so there is always at least one admin who
    can manage membership — this seeds the admin invariant the remove/leave
    paths protect (no channel is born adminless). ``aiko_channel`` defaults to a
    unique generated token so two channels with the same display name don't
    collide on the unique aiko_channel constraint."""
    policy = join_policy if join_policy in (JOIN_OPEN, JOIN_INVITE_ONLY) else JOIN_INVITE_ONLY
    channel = Channel(
        id=new_ulid(),
        name=name,
        kind=kind,
        # aiko_channel is wire-unique; for a user-created channel default to a
        # namespaced token derived from its id rather than the display name.
        aiko_channel=aiko_channel or f"ch_{new_ulid()}",
        is_private=is_private,
        join_policy=policy if is_private else JOIN_OPEN,
    )
    session.add(channel)
    session.add(
        Membership(
            channel_id=channel.id,
            user_id=creator_id,
            role=ROLE_ADMIN,
            can_post=True,
        )
    )
    await session.commit()
    return channel
