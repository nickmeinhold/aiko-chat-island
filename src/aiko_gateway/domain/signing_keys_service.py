"""Sovereign-signing key binding (#1816 PR B) — the pubkey->account roster.

The persistence half of "whose key is this". ``record_signing_key`` is the SINGLE
door both writers go through — the implicit one inside ``create_outbound`` (a
signed message observes its sender's key at send time) and the explicit
``POST /v1/keys``. One door means one idempotent upsert and no second insert path
to race.

This module is persistence ONLY — it does NOT validate the pubkey. Shape
validation is the job of the boundary that admits the value:
``signing.validate_origin`` for the implicit path (already run in ws.py before
``create_outbound``) and ``signing.decode_multikey`` in ``rest/keys.py`` for the
explicit path. Same split as ``devices_service`` (pydantic/enum validates at the
route; the service just persists).

COMMIT CONVENTION: the mutators here do NOT commit — the caller owns the
transaction. This is what keeps the implicit binding ATOMIC with the message
insert: ``create_outbound`` records the key, adds the ``Message``, and commits
ONCE, so a signed message can never persist without its binding (and a failed
message insert rolls the binding back with it). The explicit route commits after
its single ``record_signing_key`` call.
"""
from __future__ import annotations

from sqlalchemy import delete, func, insert, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .ids import new_ulid
from .models import SigningKey, _utcnow


async def record_signing_key(
    session: AsyncSession, *, user_id: str, pubkey: str, key_version: int = 1
) -> SigningKey:
    """Observe (or re-observe) that ``user_id`` used ``pubkey``. Idempotent and
    race-safe, keyed on ``(user_id, pubkey)``. Does NOT commit — the caller owns
    the transaction (see the module docstring on atomicity).

    First observation inserts (``first_seen_at == last_seen_at``). A repeat — the
    same account re-sending with the same key — BUMPS ``last_seen_at`` on the
    existing row rather than duplicating. UNIQUE(user_id, pubkey) is the authority,
    not a pre-check (which would have a TOCTOU window under concurrent sends).

    Insert inside a SAVEPOINT so a unique violation rolls back ONLY the failed
    insert, leaving the outer txn/greenlet intact — the same aiosqlite
    MissingGreenlet hazard handled in ``devices_service.register_device`` /
    ``memberships_service._insert_idempotent``.

    ``key_version`` is stored on first observation and NOT overwritten on a repeat:
    a fixed pubkey keeps its first-seen version (a differing version for the same
    key is anomalous, not a legitimate update — only ``last_seen_at`` moves).

    RACED-REVOKE ROBUSTNESS (cage-match Tesla): on a unique conflict we re-fetch the
    existing row and bump it. But a concurrent ``DELETE /v1/keys/{pubkey}`` (or an
    account purge) can delete the conflicting row between our failed insert and the
    re-fetch, so the re-fetch returns None. The ONLY unique constraint here is
    ``(user_id, pubkey)`` and ``user_id`` is always a valid authed user, so a
    None re-fetch is ALWAYS that benign raced-delete — never a genuine bad-FK
    conflict. We therefore RETRY the insert once (the conflicting row is now gone,
    so it succeeds). This matters because the implicit caller folds this into
    ``create_outbound``: without the retry, a mid-flight revoke on a second device
    would abort an otherwise-fine SIGNED MESSAGE with a 500. Observation is
    best-effort; it must not sink message carriage. Only a pathological double
    conflict-then-vanish (or a truly unexpected IntegrityError) re-raises."""
    # ONE timestamp for both columns at birth, so first_seen == last_seen is a real
    # invariant on a first observation (the model's per-column `default=_utcnow`
    # would fire twice, microseconds apart, and never be exactly equal).
    now = _utcnow()
    last_exc: IntegrityError | None = None
    for _ in range(2):
        row = SigningKey(user_id=user_id, pubkey=pubkey, key_version=key_version,
                         first_seen_at=now, last_seen_at=now)
        try:
            async with session.begin_nested():
                session.add(row)
            return row
        except IntegrityError as e:
            last_exc = e
            # Already recorded — the SAVEPOINT is rolled back, the outer txn is
            # still live. Bump last_seen_at on the existing row (eagerly populated
            # so a later sync attribute access can't trigger a lazy refresh).
            existing = (
                await session.execute(
                    select(SigningKey)
                    .where(
                        SigningKey.user_id == user_id,
                        SigningKey.pubkey == pubkey,
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.last_seen_at = _utcnow()
                return existing
            # existing is None: the conflicting row was revoked/purged out from
            # under us. Loop to retry the insert (now unobstructed).
    # Two conflicts with the row vanishing each time is pathological — surface the
    # real error rather than silently no-op (mirrors the spirit of
    # devices_service.register_device's re-raise).
    assert last_exc is not None  # only reachable via the except branch
    raise last_exc


async def count_keys(session: AsyncSession, user_id: str) -> int:
    """How many signing keys ``user_id`` currently has on file — the per-user cap
    check for the explicit ``POST /v1/keys`` route (the implicit send path is never
    capped; a real message must not fail on a key count)."""
    return (await session.execute(
        select(func.count()).select_from(SigningKey)
        .where(SigningKey.user_id == user_id))).scalar_one()


async def get_key(
    session: AsyncSession, *, user_id: str, pubkey: str
) -> SigningKey | None:
    """The caller's binding for ``pubkey``, or None. Lets the explicit route tell a
    re-registration (idempotent, allowed even at the cap) from a genuinely new key."""
    return (await session.execute(
        select(SigningKey).where(
            SigningKey.user_id == user_id, SigningKey.pubkey == pubkey))
    ).scalar_one_or_none()


def _capped_insert(*, key_id: str, user_id: str, pubkey: str,
                   key_version: int, now, max_keys: int):
    """An ``INSERT ... SELECT ... WHERE (count < max_keys)`` statement: it inserts the
    new binding IFF the user is under the per-user cap, in ONE atomic statement.

    The heart of the cap being EXACT rather than best-effort (cage-match Carnot,
    PR#67 round 2): a plain count-then-insert proves "under cap" at READ time,
    leaving a TOCTOU in which concurrent POSTs each observe count < cap and all
    insert. Folding the count INTO the insert makes the check and the mutation
    atomic — the count subquery is re-evaluated at WRITE time under SQLite's
    single-writer lock (a concurrent writer blocks until the first commits, then
    sees the committed row), so a burst can no longer overshoot. Mirrors
    ``communities_service._new_join_insert`` — the codebase's established pattern
    for folding a predicate into a write."""
    under_cap = select(
        literal(key_id), literal(user_id), literal(pubkey),
        literal(key_version), literal(now), literal(now),
    ).where(
        (select(func.count()).select_from(SigningKey)
         .where(SigningKey.user_id == user_id).scalar_subquery()) < max_keys
    )
    return insert(SigningKey).from_select(
        ["id", "user_id", "pubkey", "key_version", "first_seen_at", "last_seen_at"],
        under_cap)


async def register_explicit_key(
    session: AsyncSession, *, user_id: str, pubkey: str,
    key_version: int, max_keys: int,
) -> tuple[SigningKey | None, bool]:
    """Explicit ``POST /v1/keys`` registration with an ATOMIC per-user cap. Returns
    ``(row, True)`` on success (new insert OR idempotent re-register) and
    ``(None, False)`` when a genuinely NEW key would exceed ``max_keys``. Does NOT
    commit — the route owns the transaction.

    Distinct from ``record_signing_key`` (the shared, UNcapped door the implicit
    send path uses): a real signed message must never fail on a key count, so the
    cap lives ONLY here, on the arbitrary-mint API surface. Re-registering an
    existing key is idempotent and always allowed, even at the cap (bumps
    last_seen_at)."""
    # Idempotent re-register: an existing key bumps last_seen_at, always allowed.
    existing = await get_key(session, user_id=user_id, pubkey=pubkey)
    if existing is not None:
        existing.last_seen_at = _utcnow()
        return existing, True
    # Genuinely new key: atomic capped insert (count folded into the write).
    now = _utcnow()
    key_id = new_ulid()
    try:
        async with session.begin_nested():
            result = await session.execute(_capped_insert(
                key_id=key_id, user_id=user_id, pubkey=pubkey,
                key_version=key_version, now=now, max_keys=max_keys))
    except IntegrityError:
        # A concurrent POST of the SAME new pubkey won the (user_id, pubkey) race.
        # Treat as idempotent success (re-fetch + bump), mirroring
        # record_signing_key's conflict handling.
        existing = await get_key(session, user_id=user_id, pubkey=pubkey)
        if existing is None:
            raise
        existing.last_seen_at = _utcnow()
        return existing, True
    if result.rowcount == 0:
        # rowcount 0 has TWO causes (cage-match Carnot, PR#67 round 3):
        #   (a) genuinely at the cap, OR
        #   (b) a CONCURRENT request registered this SAME pubkey and filled the final
        #       slot between our top idempotency check and this insert. The cap
        #       predicate (count < max_keys) then suppresses OUR SELECT row, so SQLite
        #       never attempts the insert and the UNIQUE conflict never fires — the
        #       IntegrityError branch above is bypassed. Returning (None, False) here
        #       would wrongly 429 a registration that should be idempotent success.
        # Disambiguate by re-fetching: if the key now exists it is OURS (idempotent
        # success, bump last_seen_at); only a still-absent key is a real cap rejection.
        existing = await get_key(session, user_id=user_id, pubkey=pubkey)
        if existing is not None:
            existing.last_seen_at = _utcnow()
            return existing, True
        return None, False
    # from_select doesn't return an ORM object; re-fetch the inserted row.
    row = await get_key(session, user_id=user_id, pubkey=pubkey)
    return row, True


async def list_keys(session: AsyncSession, user_id: str) -> list[SigningKey]:
    """Every signing key observed for ``user_id`` — the caller's own roster
    (``GET /v1/keys``). Ordered by first_seen_at then id for a deterministic list."""
    rows = (
        await session.execute(
            select(SigningKey)
            .where(SigningKey.user_id == user_id)
            .order_by(SigningKey.first_seen_at, SigningKey.id)
        )
    ).scalars()
    return list(rows)


async def revoke_key(
    session: AsyncSession, *, user_id: str, pubkey: str
) -> bool:
    """Remove the caller's binding for ``pubkey`` (``DELETE /v1/keys/{pubkey}``).
    Returns True if a row was deleted, False otherwise. Commits (the explicit
    route owns no larger transaction).

    Scoped to (user_id, pubkey), NOT pubkey alone — revoking is purely "forget MY
    key", it never legitimately crosses users, so scoping to the authenticated
    user closes a cross-user delete vector at zero cost (a caller who learned
    another account's pubkey — it is public — cannot delete that account's binding).
    Mirrors ``devices_service.unregister_device``.

    HONEST-SCOPE NOTE (cage-match Tesla): this is a HARD delete — user-revoke means
    forget (and re-register mints a virgin row). It therefore does NOT retain a
    revoked key's observation history, so the cross-user collision SIGNAL the model
    keeps is durable only for LIVE keys: a caller can erase their own
    ``(caller, pubkey)`` row. That is acceptable pre-trust-root because nothing
    ADJUDICATES collisions yet. A retained-evidence soft-revoke (``revoked_at``
    tombstone) belongs with the revocation/rotation lifecycle that is explicitly
    deferred to federation #1760 — see the SigningKey model docstring."""
    result = await session.execute(
        delete(SigningKey).where(
            SigningKey.user_id == user_id, SigningKey.pubkey == pubkey
        )
    )
    await session.commit()
    return result.rowcount > 0


async def purge_user_keys(session: AsyncSession, user_id: str) -> None:
    """Delete all signing-key bindings for a user — called from account deletion
    (children-before-parent, no ON DELETE CASCADE in this codebase). Does NOT
    commit: the caller owns the deletion transaction (mirrors
    ``devices_service.purge_user_devices``)."""
    await session.execute(
        delete(SigningKey).where(SigningKey.user_id == user_id)
    )
