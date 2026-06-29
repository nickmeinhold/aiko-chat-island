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

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import acl
from .ids import new_ulid
# Role / JoinPolicy are defined in models.py (the persistence layer) so the closed
# set is the single source of truth for the column default AND the DB CHECK
# constraint (#11). Re-exported here so the long-standing `memberships_service.Role`
# / `.JoinPolicy` call sites (rest/members.py, tests) are unchanged.
from .models import Channel, JoinPolicy, Membership, Role, User  # noqa: F401


# Back-compat string aliases — the model defaults and existing call sites use
# these. StrEnum members compare equal to their string value, so == checks keep
# working whether a Role/JoinPolicy or a bare string is passed.
ROLE_ADMIN = Role.ADMIN
ROLE_MEMBER = Role.MEMBER
JOIN_OPEN = JoinPolicy.OPEN
JOIN_INVITE_ONLY = JoinPolicy.INVITE_ONLY


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


async def _lock_admin_count(session: AsyncSession, channel_id: str) -> int:
    """Count the channel's admins, ROW-LOCKING those admin rows for the rest of
    the transaction (``SELECT ... FOR UPDATE``).

    This closes the last-admin TOCTOU race (cage-match PR#10: Maxwell + Kelvin +
    Carnot consensus). Without the lock, two concurrent admin-removals on a
    2-admin channel both read count==2, both pass the ``<= 1`` guard, and both
    delete — orphaning the channel with zero admins. Holding a write lock on the
    admin rows serializes the second transaction behind the first, so it re-reads
    count==1 and is correctly refused.

    SQLite (the test engine) has no row locks and silently ignores
    ``with_for_update`` — harmless there because the test session is serial
    anyway; the lock matters only on Postgres, where the race is real."""
    rows = (
        await session.execute(
            select(Membership.user_id)
            .where(
                Membership.channel_id == channel_id,
                Membership.role == Role.ADMIN,
            )
            .with_for_update()
        )
    ).scalars().all()
    return len(rows)


async def _insert_idempotent(
    session: AsyncSession, membership: Membership, channel_id: str, user_id: str
) -> Membership:
    """Insert a membership, treating a concurrent duplicate as a no-op (case (e)).

    The composite PK (channel_id, user_id) is the real idempotency guarantee: a
    pre-insert existence check has a TOCTOU window where two concurrent joins
    both see "not a member" and both insert. Rather than letting the loser raise
    an IntegrityError (500), we catch it, roll back, and return the row the
    winner committed — so a racing double-join/double-add is genuinely idempotent
    (cage-match PR#10: Carnot). The DB constraint, not the check, is the
    authority."""
    # Insert inside a SAVEPOINT so a unique-constraint violation rolls back ONLY
    # the failed insert, leaving the outer transaction (and its async/greenlet
    # context) intact. A plain commit-then-rollback would unwind the connection
    # state and, on aiosqlite, break the subsequent re-fetch with MissingGreenlet.
    try:
        async with session.begin_nested():
            session.add(membership)
        await session.commit()
        return membership
    except IntegrityError:
        # The duplicate's SAVEPOINT is already rolled back; the outer txn is
        # still live. Re-fetch the winner's row, eagerly populating it so a
        # later sync attribute access can't trigger a lazy refresh.
        existing = (
            await session.execute(
                select(Membership)
                .where(
                    Membership.channel_id == channel_id,
                    Membership.user_id == user_id,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        raise  # a different integrity violation (e.g. bad FK) — surface it


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
    role: str = Role.MEMBER,
    can_post: bool = True,
) -> Membership:
    """Admin adds ``target_user_id`` to a channel. Idempotent: re-adding an
    existing member returns the existing row unchanged (case (e)), including
    under a concurrent double-add (the composite PK is the authority).

    Rejects: non-admin actor (a, via _require_admin), unseeable channel (404),
    and an unknown target user (controlled NotAMember rather than an FK
    IntegrityError at commit — cage-match PR#10: Carnot)."""
    await _require_admin(session, channel_id, actor_id)
    existing = await _membership(session, channel_id, target_user_id)
    if existing is not None:
        return existing  # idempotent — do not silently flip role/can_post
    # Validate the target user up front so a nonexistent user is a controlled
    # rejection, not an FK violation surfacing as a 500 at commit time.
    if await session.get(User, target_user_id) is None:
        raise NotAMember(target_user_id)
    role = role if role in (Role.ADMIN, Role.MEMBER) else Role.MEMBER
    m = Membership(
        channel_id=channel_id,
        user_id=target_user_id,
        role=role,
        can_post=can_post,
    )
    return await _insert_idempotent(session, m, channel_id, target_user_id)


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

    Idempotent (case (e)): already a member -> returns the existing row,
    including under a concurrent double-join (the composite PK is the authority).

    EXISTENCE-HIDING — single-query resolution (cage-match PR#10: Carnot). The
    earlier version did a bare ``session.get(Channel, id)`` PK lookup, which is a
    query-SHAPE / timing oracle: a nonexistent id is a PK miss, an invite_only
    private channel is a PK HIT then a policy read — distinguishable by latency
    even though both return 404. That reintroduces exactly the oracle #36's
    single-query design closed. We instead resolve "joinable-or-already-visible"
    in ONE statement: the channel comes back iff it is PUBLIC, or PRIVATE+OPEN,
    or the actor is already a member. A nonexistent channel and an invite_only
    private one both yield None with identical DB work — no oracle."""
    joinable = exists().where(
        (Membership.channel_id == Channel.id) & (Membership.user_id == actor_id)
    )
    channel = (
        await session.execute(
            select(Channel).where(
                Channel.id == channel_id,
                Channel.is_private.is_(False)
                | (Channel.join_policy == JoinPolicy.OPEN)
                | joinable,
            )
        )
    ).scalar_one_or_none()
    if channel is None:
        # Not found, OR private+invite_only and the actor is not already in it —
        # indistinguishable by design (same single-None as the read path).
        raise ChannelNotFound(channel_id)

    existing = await _membership(session, channel_id, actor_id)
    if existing is not None:
        return existing  # already in — idempotent self-join

    m = Membership(
        channel_id=channel_id,
        user_id=actor_id,
        role=Role.MEMBER,
        can_post=True,
    )
    return await _insert_idempotent(session, m, channel_id, actor_id)


async def remove_member(
    session: AsyncSession, *, channel_id: str, actor_id: str, target_user_id: str
) -> None:
    """Admin removes ``target_user_id``. Rejects: non-admin (a), unseeable
    channel (404), removing a non-member (f), and removing the last admin (d)."""
    await _require_admin(session, channel_id, actor_id)
    target = await _membership(session, channel_id, target_user_id)
    if target is None:
        raise NotAMember(target_user_id)
    # Row-lock the admin set BEFORE the count, so a concurrent admin-removal is
    # serialized behind us and the last-admin guard can't be raced (PR#10).
    if target.role == Role.ADMIN and await _lock_admin_count(session, channel_id) <= 1:
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
    # Same row-locked last-admin guard as remove_member — serializes a racing
    # concurrent leave so the channel can't be orphaned (PR#10).
    if mine.role == Role.ADMIN and await _lock_admin_count(session, channel_id) <= 1:
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
    policy = (
        join_policy if join_policy in (JoinPolicy.OPEN, JoinPolicy.INVITE_ONLY)
        else JoinPolicy.INVITE_ONLY
    )
    channel_id = new_ulid()
    channel = Channel(
        id=channel_id,
        name=name,
        kind=kind,
        # aiko_channel is wire-unique; for a user-created channel default to a
        # namespaced token DERIVED FROM the channel id (not the display name, and
        # not a second independent ULID — Kelvin, PR#10), so the wire name maps
        # 1:1 to the row and is reproducible from the id.
        aiko_channel=aiko_channel or f"ch_{channel_id}",
        is_private=is_private,
        join_policy=policy if is_private else JoinPolicy.OPEN,
    )
    session.add(channel)
    # Channel + creator-admin are added before a SINGLE commit, so the auto-admin
    # seed is atomic with channel creation (no window where a channel exists with
    # zero admins). Carnot confirmed this atomicity in PR#10.
    session.add(
        Membership(
            channel_id=channel.id,
            user_id=creator_id,
            role=Role.ADMIN,
            can_post=True,
        )
    )
    await session.commit()
    return channel
