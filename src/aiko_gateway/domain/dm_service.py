"""Direct messages as 1:1 (member-set) channels (#2633).

The SINGLE mutator door for DM find-or-create + the DM list read, mirroring how
``memberships_service`` owns membership mutations and ``acl`` owns membership reads —
the REST router delegates here so the pairing/idempotency rules can never drift between
call sites. Design of record: ``docs/design/11-direct-messages.md``.

A DM is an ORDINARY channel — ``kind="dm"``, ``is_private=True``, ``community_id=None``,
one ``Membership`` per participant — so it inherits the whole channel/message machinery
(auth I1, membership visibility I2 via ``acl``, existence-hiding, signed ``origin``,
mentions, reply-to integrity, takedown retractions, reactions, the block content-filter)
through the same enforcement doors. There is NO new table and NO migration.

THREE gateway decisions live here (the reviewer-facing reasoning is in the design note):

  * MEMBER-SET, NOT 2-CAPPED (app-tab shaping request). The membership relation stays
    N-able; 2-ness is confined to ``get_or_create_dm`` (this endpoint), never baked into
    the channel/membership model. A future ``kind="group"`` is an additive kind, not a
    schema retype. That is why this module NEVER writes a member_a/member_b or a
    ``CHECK(count=2)`` — it just inserts one ``Membership`` per id in the pair.

  * THE PAIR IS THE IDEMPOTENCY KEY (no side table). ``channels.aiko_channel`` is already
    ``UNIQUE NOT NULL``; a deterministic ``dm:{lo}:{hi}`` minted from the canonically
    SORTED member ids makes find-or-create atomic on that existing constraint — INSERT,
    and on ``IntegrityError`` (a concurrent double-tap raced us) roll back and re-fetch.
    Remove-the-coupling: no ``dm_pairs`` table to keep in sync. The ``dm:`` prefix keeps
    2-ness out of the model (a group would mint a ULID ``aiko_channel``, not a pair).

  * DMs DO NOT FEDERATE ON THE BUS. Enforced in ``realtime/ws.py`` (the publish is gated
    on ``kind != "dm"``), not here — this module only marks the channel ``kind="dm"``;
    the send path reads that kind to keep private content off the shared ChatServer. A DM
    is island-local by construction (both members on one island). Documented here so the
    invariant is visible from the creation door too.
"""
from __future__ import annotations

from sqlalchemy import null, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import users_service
from .ids import new_ulid
from .models import Channel, ChannelKind, Membership, User

# The deterministic aiko_channel prefix for a DM. A DM never rides the bus (ws.py gates
# the publish on kind), so this string is a LOCAL idempotency key, not a real aiko
# recipient — but it must still be UNIQUE + non-null (the channels schema), which is
# exactly what makes it the find-or-create key. `dm:` + two 26-char ULIDs + one ':' = 56
# chars, comfortably under channels.aiko_channel's String(64). PUBLIC + single-source:
# channels_service imports this to RESERVE the prefix at the bus-reconcile boundary
# (a bus channel_list must never mint/route/delete a dm: channel — cage-match PR#124).
DM_CHANNEL_PREFIX = "dm:"


def is_dm_channel_name(aiko_channel: str) -> bool:
    """True iff ``aiko_channel`` is in the reserved local DM namespace. Used by
    ``channels_service`` to fail-closed on any bus attempt to reconcile a ``dm:`` name,
    so the DM privacy invariant does not rest on the bus operator never naming one."""
    return aiko_channel.startswith(DM_CHANNEL_PREFIX)


def _canonical_aiko_channel(a_id: str, b_id: str) -> str:
    """The deterministic DM channel key for the UNORDERED pair {a, b}. Sort so
    ``{me, target}`` and ``{target, me}`` map to the SAME string (idempotency), and a
    self-DM (a == b) collapses to a single-id key (notes-to-self). ULIDs sort
    lexicographically, so ``sorted`` gives a stable canonical order."""
    lo, hi = sorted({a_id, b_id}) if a_id != b_id else (a_id, a_id)
    return f"{DM_CHANNEL_PREFIX}{lo}:{hi}"


def _member_ids(me_id: str, target_id: str) -> list[str]:
    """The DM's member set — a SET (self-DM has one member, notes-to-self). Ordering is
    canonical (sorted) so ``members`` on the wire is stable across both participants."""
    return sorted({me_id, target_id})


def dm_channel_view(channel: Channel, member_ids: list[str]) -> dict:
    """The channel view the contract returns from ``POST /v1/dm`` and per item in
    ``GET /v1/dm`` (before ``last_message`` is attached). ``members`` is always an ARRAY
    (never a peer scalar) so an N-member group serializes through the identical shape."""
    return {
        "channel_id": channel.id,
        "kind": channel.kind,
        "members": member_ids,
        "created_at": channel.created_at.isoformat(),
    }


async def members_of(session: AsyncSession, channel_id: str) -> list[str]:
    """The member ids of a DM channel, canonical (sorted) order."""
    rows = (await session.execute(
        select(Membership.user_id).where(Membership.channel_id == channel_id)
    )).scalars()
    return sorted(rows)


async def members_of_many(
    session: AsyncSession, channel_ids: list[str]
) -> dict[str, list[str]]:
    """Batched ``members_of`` — ``{channel_id: [member ids, sorted]}`` in ONE query.
    Collapses ``GET /v1/dm``'s per-channel membership N+1 (cage-match PR#124)."""
    if not channel_ids:
        return {}
    result: dict[str, list[str]] = {cid: [] for cid in channel_ids}
    rows = (await session.execute(
        select(Membership.channel_id, Membership.user_id).where(
            Membership.channel_id.in_(channel_ids))
    )).all()
    for cid, uid in rows:
        result[cid].append(uid)
    return {cid: sorted(uids) for cid, uids in result.items()}


class TargetNotFound(Exception):
    """``target_user_id`` does not resolve to a real user. The route maps this to the
    contract's 404 (same code the app expects for a bad target)."""


class DmKeyCollision(Exception):
    """The deterministic ``dm:<lo>:<hi>`` key is already held by a channel that is NOT
    a DM (kind != 'dm'). FAIL CLOSED (cage-match PR#124 Carnot+Tesla): ``aiko_channel``
    is a namespace shared with bus-reconciled channels, so — however unlikely, since the
    key embeds two unguessable 26-char ULIDs — a non-DM channel squatting the key must
    NOT be adopted as a DM. Adopting it would (a) return a non-DM channel from
    POST /v1/dm whose sends then FEDERATE on the bus (ws.py gates on kind), leaking
    "DM" content, and (b) grant the pair membership on an unrelated channel. Refusing is
    a DoS on that one pair (deniable, requires squatting their exact ULIDs) — never a
    privacy leak. The route maps this to a 409/500-class error, not a silent success."""


async def get_or_create_dm(
    session: AsyncSession, *, me: User, target_user_id: str
) -> tuple[Channel, list[str]]:
    """Find-or-create the 1:1 DM channel for the unordered pair ``{me, target}`` and
    return ``(channel, member_ids)``. Idempotent: the same pair always resolves to the
    same channel (canonical ``aiko_channel``). Raises ``TargetNotFound`` if the target
    is not a real user.

    Self-DM (``target_user_id == me.id``) is ALLOWED (notes-to-self, app-tab decision) —
    it resolves to a single-member channel.

    COMMITS its own transaction (a self-contained REST mutation, unlike the flush-only
    reconcile services whose caller owns the boundary). The channel + its memberships
    land in ONE commit so a channel can never persist without its member rows.

    CONCURRENCY (double-tap → one channel, not two): the INSERT races on the existing
    ``UNIQUE(aiko_channel)`` constraint. The loser catches ``IntegrityError``, rolls
    back, and re-fetches the winner's channel — then ensures ITS OWN memberships exist
    (idempotent on the ``Membership`` composite PK), so a partial interleave still
    converges on the full member set. Mirrors the INSERT-or-refetch pattern
    ``users_service.create_passkey_account`` / ``devices_service`` use on their UNIQUE
    columns."""
    target = await users_service.get_by_id(session, target_user_id)
    if target is None:
        raise TargetNotFound(target_user_id)

    aiko_channel = _canonical_aiko_channel(me.id, target_user_id)
    member_ids = _member_ids(me.id, target_user_id)

    existing = (await session.execute(
        select(Channel).where(Channel.aiko_channel == aiko_channel)
    )).scalar_one_or_none()
    if existing is not None:
        # Already created by a prior call (or a concurrent winner). FAIL CLOSED if the
        # key is somehow held by a non-DM channel (see DmKeyCollision) — never adopt or
        # federate a foreign channel as a DM. Otherwise ensure my own membership(s) exist
        # and return — never a second channel for the same pair.
        if existing.kind != ChannelKind.DM:
            raise DmKeyCollision(aiko_channel)
        await _ensure_memberships(session, existing.id, member_ids)
        await session.commit()
        return existing, member_ids

    channel = Channel(
        id=new_ulid(),
        name=aiko_channel,          # cosmetic for a DM (client renders member handles)
        kind=ChannelKind.DM,        # ws.py reads this to keep the message off the bus
        aiko_channel=aiko_channel,
        is_private=True,            # → acl.readable_predicate requires a membership row
        # SQL NULL, not None: a DM is community-less. The Channel.community_id model
        # DEFAULT is the seeded Aiko community, and SQLAlchemy applies a Python-side
        # scalar `default=` whenever the bound value is None — so `community_id=None`
        # would SILENTLY store Aiko's id (verified: the model comment claiming "pass
        # None EXPLICITLY" is wrong; None does NOT bypass the default). A DM in Aiko
        # would leak into `visible_channels_in_community`. `null()` is the only thing
        # that overrides a column default with a real SQL NULL. The partial CHECK
        # ck_channels_community_required exempts kind='dm' from the community rule.
        community_id=null(),
    )
    session.add(channel)
    for uid in member_ids:
        session.add(Membership(channel_id=channel.id, user_id=uid,
                               role="member", can_post=True))
    try:
        await session.commit()
        # Reload from the row we just wrote so the returned object reflects the stored
        # NULL community_id (in-memory it currently holds the null() construct, not
        # None) — callers get a faithful object, not a SQL expression.
        await session.refresh(channel)
    except IntegrityError:
        # A concurrent POST for the same pair won the UNIQUE(aiko_channel) race. Roll
        # back our half-built channel, adopt the winner's, and make sure our memberships
        # are present on it (idempotent) — converge on the same channel, never dupe.
        await session.rollback()
        winner = (await session.execute(
            select(Channel).where(Channel.aiko_channel == aiko_channel)
        )).scalar_one_or_none()
        # NARROW the catch (cage-match PR#124 Tesla): only a UNIQUE(aiko_channel) race
        # leaves a winner behind. If NO row holds the key, the IntegrityError was some
        # OTHER integrity failure (an FK to a target deleted mid-flight, a CHECK) — NOT
        # our race — so re-raise it rather than masking it as a phantom race (which would
        # surface as an opaque NoResultFound/500 on scalar_one()).
        if winner is None:
            raise
        # Same fail-closed guard as the adopt path: the winner of the UNIQUE race must
        # itself be a DM (it is, when the race is two POST /v1/dm) — never adopt a
        # non-DM channel that squats the key.
        if winner.kind != ChannelKind.DM:
            raise DmKeyCollision(aiko_channel)
        await _ensure_memberships(session, winner.id, member_ids)
        await session.commit()
        return winner, member_ids
    return channel, member_ids


async def _ensure_memberships(
    session: AsyncSession, channel_id: str, member_ids: list[str]
) -> None:
    """Add any missing ``Membership`` rows for ``member_ids`` on ``channel_id``,
    idempotently and RACE-SAFELY. FLUSH only; the caller owns the commit.

    Each missing row is inserted inside its OWN SAVEPOINT so a concurrent path that
    inserts the same (channel_id, user_id) first raises ``IntegrityError`` on the
    composite PK — which we swallow (the row now exists, which is the goal) without
    poisoning the caller's outer transaction. (Correcting an earlier comment that
    claimed a plain ORM re-insert is a silent no-op — it is NOT; a duplicate composite
    PK raises, exactly the concurrent-repair 500 cage-match PR#124 Carnot+Tesla flagged.
    The savepoint converts that raise into the intended idempotent no-op.)"""
    present = set((await session.execute(
        select(Membership.user_id).where(
            Membership.channel_id == channel_id,
            Membership.user_id.in_(member_ids),
        )
    )).scalars())
    for uid in member_ids:
        if uid in present:
            continue
        try:
            async with session.begin_nested():   # SAVEPOINT — rolls back just this row
                session.add(Membership(channel_id=channel_id, user_id=uid,
                                       role="member", can_post=True))
        except IntegrityError:
            pass  # a concurrent inserter won the PK — the row exists; idempotent success
    await session.flush()


async def list_dms(session: AsyncSession, viewer_id: str) -> list[Channel]:
    """The viewer's DM channels (``kind="dm"`` channels they belong to), newest-active
    first is NOT promised — ordered by channel id desc (creation order) for a stable
    switcher. ``last_message`` is attached by the route (it is viewer-dependent — see
    ``last_visible_message``)."""
    rows = (await session.execute(
        select(Channel)
        .join(Membership, Membership.channel_id == Channel.id)
        .where(Channel.kind == "dm", Membership.user_id == viewer_id)
        .order_by(Channel.id.desc())
    )).scalars()
    return list(rows)
