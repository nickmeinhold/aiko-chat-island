"""Community discovery / detail / join / my-communities — the READ + JOIN trust
surface (#32, Phase B2).

Option B's directory layer. B1 shipped the model invisibly; this is where the
real trust boundary lives. Three NEW read paths into the community visible-set
(discover, detail, join) plus "my communities". Like ``acl`` (channel reads) and
``memberships_service`` (channel-membership writes), this module is the SINGLE
enforcement source the REST router delegates to, so the visibility rule cannot
drift between the paired reads.

THE TRUST SURFACE — within-instant visibility consistency (global lesson b2d9).
``discover`` lists a SNAPSHOT; ``join`` is the AUTHORITATIVE gate. A community can
flip public->private, or be taken down, BETWEEN a viewer's ``discover`` listing
and their ``join`` — even DURING the join call. So ``join`` does not observe-then-
write: it folds the visibility check INTO the membership INSERT (a conditional
``INSERT ... SELECT ... WHERE EXISTS(joinable)``), so the check and the mutation are
ATOMIC and the gate is authoritative at WRITE time, not observe time (Carnot
cage-match, PR#48). A community no longer joinable inserts nothing and FAILS CLOSED
(404) — it never trusts "it was in the list".

Two read predicates + one write predicate, kept DISTINCT (conflating them
overstates the gate — Carnot cage-match, PR#48):
  * ``discoverable_predicate()`` — what the PUBLIC directory lists: public + not
    taken down. No viewer term: the directory is identical for everyone.
  * ``accessible_predicate(viewer_id)`` — who may SEE a community (``community_detail``
    + the idempotent existing-member branch of ``join``): (public, anyone) OR
    (already a member — so a community you're in stays reachable even if it later
    goes unlisted/private), AND not taken down. Fail-closed: anything else is 404.
  * the NEW-join write predicate (in ``_new_join_insert``) — who may newly JOIN:
    strictly PUBLIC and not taken down. Stricter than "may see": you can SEE a
    private community you're in, but you cannot newly self-join a non-public one.

DELIBERATE v1 SCOPE (named, not overlooked — these are the cage-match's questions):
  * Only PUBLIC communities are self-joinable. ``accessible_predicate`` admits
    unlisted/private ONLY via the member branch, so a NEW join to a non-public
    community fails closed. Direct-link join of an unlisted community and invites
    to a private one are B3 — and no creation path exists yet (everything is the
    seeded public "Aiko"), so deferring them is zero-impact AND strictly safer.
  * A user-block does NOT remove a community from the directory. A public community
    is shared infrastructure (Aiko's owner is even NULL); conflating user-block with
    community visibility would let one member's block hide shared infra from them.
    Block stays a per-message / per-user visibility dimension (moderation_service).
  * NO leave-community endpoint (not in the B2 handoff). So ``member_count`` only
    INCREMENTS here; the decrement path is account-deletion (wired in B1).
  * ``sort=active`` (last_activity_at) is deferred — no per-channel last-ULID
    rollup populates it yet; v1 sorts by ``members`` (default) or ``name``.

Commit convention: service-owns-commit (mirrors ``memberships_service`` /
``accounts_service``) so the REST routes stay commit-free.
"""
from __future__ import annotations

import base64

from sqlalchemy import and_, exists, insert, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import acl
from .models import Channel, Community, CommunityMembership, Role, Visibility

# Fixed directory page size — NOT client-controlled (a caller can't request an
# arbitrarily large page as a cheap amplification). The cursor walks the rest.
PAGE_SIZE = 20
_SORTS = ("members", "name")
# Cursor field delimiter: ASCII unit-separator, which never appears in a ULID id
# and is vanishingly unlikely in a community name. We still split from the RIGHT
# (the id is the fixed-width last field) so a name containing it can't corrupt the
# decode.
_CURSOR_SEP = "\x1f"


class CommunityNotFound(Exception):
    """The community does not exist, OR is not visible/joinable to this viewer.
    The two are deliberately indistinguishable (existence-hiding, mirroring
    ``memberships_service.ChannelNotFound``)."""


class InvalidCursor(Exception):
    """The pagination cursor is malformed (tampered or stale-format)."""


# --- visibility predicates (the single source both paired reads consult) ----


def discoverable_predicate():
    """SQL predicate: this Community is LISTED in the public directory — public and
    not taken down. No viewer term; the directory is the same for everyone."""
    return and_(
        Community.visibility == Visibility.PUBLIC,
        Community.taken_down_at.is_(None),
    )


def accessible_predicate(viewer_id: str):
    """SQL predicate: this Community is VISIBLE to ``viewer_id`` — public (anyone)
    or one they already belong to — AND not taken down. The authoritative gate for
    BOTH ``community_detail`` and ``join`` so the read and the join-time re-check
    can't diverge (b2d9). A correlated EXISTS so it composes into one statement."""
    member = exists().where(
        (CommunityMembership.community_id == Community.id)
        & (CommunityMembership.user_id == viewer_id)
    )
    return and_(
        Community.taken_down_at.is_(None),
        or_(Community.visibility == Visibility.PUBLIC, member),
    )


# --- cursor pagination helpers ----------------------------------------------


def _encode_cursor(sort: str, sort_key, community_id: str) -> str:
    # The cursor carries the SORT MODE it was minted for, so a cursor from one sort
    # (e.g. name) can't be silently misinterpreted against another (e.g. members),
    # which would yield arbitrary results (Carnot cage-match, PR#48).
    raw = f"{sort}{_CURSOR_SEP}{sort_key}{_CURSOR_SEP}{community_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(token: str, expected_sort: str) -> tuple[str, str]:
    """Decode a cursor minted for ``expected_sort``. Fails CLOSED (InvalidCursor) on
    malformed base64, a missing field, OR a sort-mode mismatch — never a silent
    fallback to a degraded page (Carnot cage-match, PR#48)."""
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        sort, rest = raw.split(_CURSOR_SEP, 1)
        # rsplit: the id is the fixed last field, so a name containing the separator
        # can't corrupt the decode.
        sort_key, community_id = rest.rsplit(_CURSOR_SEP, 1)
    except Exception as exc:  # malformed base64 / missing separator
        raise InvalidCursor() from exc
    if sort != expected_sort:
        raise InvalidCursor()  # cursor minted for a different sort mode
    return sort_key, community_id


# --- discover (the public directory) ----------------------------------------


async def discover(
    session: AsyncSession,
    *,
    viewer_id: str,
    q: str | None = None,
    category: str | None = None,
    sort: str = "members",
    cursor: str | None = None,
) -> tuple[list[Community], str | None]:
    """One page of the public directory + a ``next_cursor`` (None at the end).

    Keyset (not offset) pagination: O(page), not O(offset). ``sort`` is clamped to
    the closed set (an unknown value falls back to ``members``, never an error).
    ``q`` is a case-insensitive name substring (LIKE metacharacters are escaped, so
    a literal ``%`` in the query matches a literal ``%``); ``category`` is an exact
    match. Both AND with the discoverable predicate. ``viewer_id`` is accepted for
    signature symmetry with the other read paths (the directory is
    viewer-independent in v1).

    PAGINATION SEMANTICS — this is a LIVE feed, not a frozen snapshot (named
    tradeoff, Carnot cage-match PR#48). The ``members`` sort keys on the MUTABLE
    ``member_count``, so a community whose count changes between two page fetches
    can shift across a page boundary and be seen twice or skipped. That is the
    standard weak guarantee of an activity-ranked directory; a strict no-gap/no-dupe
    contract would need a snapshot/version key, deferred until the directory needs
    it. The ``name`` sort keys on an effectively-immutable field and does not have
    this property."""
    if sort not in _SORTS:
        sort = "members"
    conds = [discoverable_predicate()]
    if q:
        # Escape LIKE metacharacters so user %/_ are matched literally, not as
        # wildcards (Carnot/Maxwell cage-match, PR#48).
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conds.append(Community.name.ilike(f"%{escaped}%", escape="\\"))
    if category:
        conds.append(Community.category == category)

    if cursor:
        sort_key, last_id = _decode_cursor(cursor, sort)
        if sort == "members":
            # ORDER BY member_count DESC, id ASC -> "after" is a strictly smaller
            # count, or the same count with a strictly greater id. A non-integer key
            # is a tampered/corrupt cursor -> fail closed (not a silent 0 fallback).
            if not sort_key.lstrip("-").isdigit():
                raise InvalidCursor()
            mc = int(sort_key)
            conds.append(or_(
                Community.member_count < mc,
                and_(Community.member_count == mc, Community.id > last_id),
            ))
        else:
            # ORDER BY name ASC, id ASC.
            conds.append(or_(
                Community.name > sort_key,
                and_(Community.name == sort_key, Community.id > last_id),
            ))

    stmt = select(Community).where(and_(*conds))
    if sort == "members":
        stmt = stmt.order_by(Community.member_count.desc(), Community.id.asc())
    else:
        stmt = stmt.order_by(Community.name.asc(), Community.id.asc())
    # Fetch one extra row to learn whether a further page exists without a count(*).
    stmt = stmt.limit(PAGE_SIZE + 1)

    rows = list((await session.execute(stmt)).scalars())
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        sort_key = last.member_count if sort == "members" else last.name
        next_cursor = _encode_cursor(sort, sort_key, last.id)
    return rows, next_cursor


# --- detail -----------------------------------------------------------------


async def community_detail(
    session: AsyncSession, *, viewer_id: str, community_id: str
) -> tuple[Community, list[Channel]]:
    """The community + the channels in it the viewer may see, or raise
    ``CommunityNotFound`` (mapped to 404 — fail closed, existence-hiding).

    Existence AND access resolve in ONE statement via ``accessible_predicate``: a
    nonexistent id and a private community the viewer is not in both come back as
    None, so neither latency nor query shape distinguishes them."""
    community = (
        await session.execute(
            select(Community).where(
                Community.id == community_id, accessible_predicate(viewer_id)
            )
        )
    ).scalar_one_or_none()
    if community is None:
        raise CommunityNotFound(community_id)
    channels = await acl.visible_channels_in_community(
        session, viewer_id, community_id)
    return community, channels


# --- join (the authoritative gate) ------------------------------------------


def _new_join_insert(community_id: str, user_id: str):
    """An ``INSERT ... SELECT ... WHERE EXISTS(joinable)`` statement: it inserts the
    (community, user) membership IFF the community is a NEW-joinable target — PUBLIC
    and not taken down — in ONE atomic statement.

    This is the heart of the b2d9 fix (Carnot cage-match, PR#48): a plain
    observe-then-insert proves visibility at READ time, leaving a TOCTOU window in
    which the community can be taken down / flipped private before the write commits.
    Folding the visibility predicate INTO the insert makes the check and the
    mutation atomic — the gate is authoritative at WRITE time, not observe time.
    Mirrors ``memberships_service._delete_membership_unless_last_admin``'s conditional
    write (the codebase's established pattern for this exact race class).

    The NEW-join predicate is strictly ``PUBLIC and not taken down`` — NOT
    ``accessible_predicate`` (which also admits existing members). An existing member
    never reaches this statement (the idempotent fast-path returns first), so the two
    concepts — "who may SEE" (accessible_predicate, detail) and "who may newly JOIN"
    (this, stricter) — stay distinct rather than conflated."""
    joinable = select(
        literal(community_id), literal(user_id), literal(Role.MEMBER.value)
    ).where(
        exists().where(
            (Community.id == community_id)
            & (Community.visibility == Visibility.PUBLIC)
            & (Community.taken_down_at.is_(None))
        )
    )
    return insert(CommunityMembership).from_select(
        ["community_id", "user_id", "role"], joinable)


async def join(
    session: AsyncSession, *, viewer_id: str, community_id: str
) -> tuple[Community, list[Channel], bool]:
    """Join ``viewer_id`` to a community. Returns (community, channels-to-subscribe,
    joined) where ``joined`` is False for an idempotent re-join. ``channels`` is the
    set the app should WS-subscribe (the same shape ``GET /v1/channels`` returns) —
    public channels need no per-channel membership (acl: public is open to all), so
    join records only the COMMUNITY-grain membership; per-channel opt-in is B3.

    FAIL CLOSED on concurrent shrink (b2d9): the visibility check is folded INTO the
    membership INSERT (``_new_join_insert``), so a community taken down / flipped
    private between ``discover`` and ``join`` — even within this call — inserts
    nothing and raises ``CommunityNotFound`` (404). The gate is authoritative at
    WRITE time, not observe time (Carnot cage-match, PR#48). Only PUBLIC communities
    are joinable as a NEW member; unlisted direct-link join + private invites are B3.

    The whole join (conditional insert + member_count bump + the channel + community
    reads for the response) runs in ONE transaction committed at the end, so the
    response is a consistent snapshot — a takedown landing after commit can't make
    the returned channel set inconsistent with the membership we wrote."""
    # Idempotent fast-path: already a member -> no second insert / no count bump.
    # Still hide a taken-down community even from an existing member (consistent with
    # accessible_predicate / list_mine — a taken-down community is gone for members
    # too).
    existing = await session.get(CommunityMembership, (community_id, viewer_id))
    if existing is not None:
        community = await session.get(Community, community_id)
        if community is None or community.taken_down_at is not None:
            raise CommunityNotFound(community_id)
        channels = await acl.visible_channels_in_community(
            session, viewer_id, community_id)
        return community, channels, False

    # New join: conditional INSERT (atomic visibility check). begin_nested so a
    # concurrent same-user insert (lost the existence pre-check race) rolls back ONLY
    # this insert, leaving the outer txn / aiosqlite greenlet intact.
    try:
        async with session.begin_nested():
            result = await session.execute(_new_join_insert(community_id, viewer_id))
        rowcount = result.rowcount
    except IntegrityError:
        rowcount = None  # concurrent same-user join already created the row

    if rowcount == 0:
        # The WHERE EXISTS(joinable) was false: nonexistent, private, or taken down.
        # Fail closed, existence-hidden behind the same 404 — at WRITE time.
        raise CommunityNotFound(community_id)

    inserted = rowcount == 1
    if inserted:
        # Atomic read-modify-write in ONE statement (correct on both engines: a
        # single UPDATE = member_count + 1 holds the row lock, so two joins by
        # DIFFERENT users each bump once). Only on a genuinely new row, so an
        # idempotent re-join never double-counts.
        await session.execute(
            update(Community)
            .where(Community.id == community_id)
            .values(member_count=Community.member_count + 1)
        )

    # Read the community (fresh — reflects the bump) and channels INSIDE the same
    # transaction, before commit, so the response is internally consistent.
    community = (await session.execute(
        select(Community).where(Community.id == community_id))).scalar_one()
    channels = await acl.visible_channels_in_community(
        session, viewer_id, community_id)
    await session.commit()
    return community, channels, inserted


# --- my communities ---------------------------------------------------------


async def list_mine(
    session: AsyncSession, *, viewer_id: str
) -> list[Community]:
    """The communities ``viewer_id`` belongs to (id asc), excluding any that have
    been taken down — a taken-down community is removed as a unit for members too,
    consistent with ``accessible_predicate``."""
    rows = (
        await session.execute(
            select(Community)
            .join(CommunityMembership,
                  CommunityMembership.community_id == Community.id)
            .where(
                CommunityMembership.user_id == viewer_id,
                Community.taken_down_at.is_(None),
            )
            .order_by(Community.id)
        )
    ).scalars()
    return list(rows)
