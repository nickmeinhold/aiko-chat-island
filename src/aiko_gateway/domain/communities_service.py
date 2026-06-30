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
and their ``join``. So ``join`` RE-CHECKS visibility at join time and FAILS CLOSED
(404) when the community is no longer joinable — it never trusts "it was in the
list". Both detail and join consult ONE predicate (``accessible_predicate``) so the
contract can't drift between the read and the gate.

Two predicates, one per projection:
  * ``discoverable_predicate()`` — what the PUBLIC directory lists: public + not
    taken down. No viewer term: the directory is identical for everyone.
  * ``accessible_predicate(viewer_id)`` — the AUTHORITATIVE gate for detail + join:
    (public, anyone) OR (already a member — so a community you're in stays
    reachable even if it later goes unlisted/private), AND not taken down.
    Fail-closed: anything else is invisible (404), never a confirmation it exists.

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

from sqlalchemy import and_, exists, or_, select, update
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


def _encode_cursor(sort_key, community_id: str) -> str:
    raw = f"{sort_key}{_CURSOR_SEP}{community_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(token: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        sort_key, community_id = raw.rsplit(_CURSOR_SEP, 1)
    except Exception as exc:  # malformed base64 / missing separator
        raise InvalidCursor() from exc
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

    Keyset (not offset) pagination: stable under concurrent inserts and O(page),
    not O(offset). ``sort`` is clamped to the closed set (an unknown value falls
    back to ``members``, never an error). ``q`` is a case-insensitive name
    substring; ``category`` is an exact match. Both AND with the discoverable
    predicate. ``viewer_id`` is accepted for signature symmetry with the other read
    paths (the directory itself is viewer-independent in v1)."""
    if sort not in _SORTS:
        sort = "members"
    conds = [discoverable_predicate()]
    if q:
        conds.append(Community.name.ilike(f"%{q}%"))
    if category:
        conds.append(Community.category == category)

    if cursor:
        sort_key, last_id = _decode_cursor(cursor)
        if sort == "members":
            # ORDER BY member_count DESC, id ASC -> "after" is a strictly smaller
            # count, or the same count with a strictly greater id.
            mc = int(sort_key) if sort_key.lstrip("-").isdigit() else 0
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
        next_cursor = _encode_cursor(sort_key, last.id)
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


async def _insert_membership_idempotent(
    session: AsyncSession, community_id: str, user_id: str
) -> bool:
    """Insert the (community, user) membership; return True iff THIS call inserted
    it, False if a concurrent caller already did. The composite PK is the authority
    (a pre-check has a TOCTOU window), mirroring
    ``memberships_service._insert_idempotent``. Returning whether we actually
    inserted is what lets ``join`` bump ``member_count`` EXACTLY ONCE per real join.

    begin_nested (a SAVEPOINT) so a unique-violation rolls back ONLY this insert,
    leaving the outer transaction (and the aiosqlite greenlet) intact — a plain
    commit-then-rollback would break the connection state on aiosqlite."""
    try:
        async with session.begin_nested():
            session.add(CommunityMembership(
                community_id=community_id, user_id=user_id, role=Role.MEMBER))
        return True
    except IntegrityError:
        return False  # a concurrent caller already joined — idempotent no-op


async def join(
    session: AsyncSession, *, viewer_id: str, community_id: str
) -> tuple[Community, list[Channel], bool]:
    """Join ``viewer_id`` to a community. Returns (community, channels-to-subscribe,
    joined) where ``joined`` is False for an idempotent re-join. ``channels`` is the
    set the app should WS-subscribe (the same shape ``GET /v1/channels`` returns) —
    public channels need no per-channel membership (acl: public is open to all), so
    join records only the COMMUNITY-grain membership; per-channel opt-in is B3.

    FAIL CLOSED on concurrent shrink (b2d9): visibility is re-checked HERE via
    ``accessible_predicate`` at join time, so a community that was public at
    ``discover`` but is now private/taken-down raises ``CommunityNotFound`` (404).
    Only PUBLIC communities are joinable as a NEW member — the member branch of the
    predicate exists solely to keep an existing membership idempotent."""
    community = (
        await session.execute(
            select(Community).where(
                Community.id == community_id, accessible_predicate(viewer_id)
            )
        )
    ).scalar_one_or_none()
    if community is None:
        # Not found, OR not visible/joinable to this viewer — indistinguishable.
        raise CommunityNotFound(community_id)

    inserted = await _insert_membership_idempotent(session, community_id, viewer_id)
    if inserted:
        # Atomic read-modify-write in ONE statement (correct on both engines: a
        # single UPDATE ... = member_count + 1 holds the row lock, so two joins by
        # DIFFERENT users each bump once). Only on a genuinely new row, so an
        # idempotent re-join never double-counts.
        await session.execute(
            update(Community)
            .where(Community.id == community_id)
            .values(member_count=Community.member_count + 1)
        )
    await session.commit()
    # Re-read so the returned member_count reflects the increment we just committed.
    await session.refresh(community)
    channels = await acl.visible_channels_in_community(
        session, viewer_id, community_id)
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
