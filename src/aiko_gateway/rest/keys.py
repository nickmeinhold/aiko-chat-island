"""Signing-key registration endpoints (#1816 PR B) — the EXPLICIT half of the
pubkey->account binding.

The app MAY register its Ed25519 signing public key here directly (rather than
only having it observed implicitly when it sends a signed message). All routes
take ``CurrentUser``: an unauthenticated caller is rejected before any row is
touched, and the key is always bound to the AUTHENTICATED user, never a user_id
from the client body (the same server-derives-identity discipline as
messages.sender_user_id / devices — invariant I5).

The app does not call these yet. They are shipped fully hardened + tested anyway:
an unused endpoint on a trust boundary is LIVE ATTACK SURFACE, not a stub. Both
writers (this route and the implicit create_outbound path) go through the single
door signing_keys_service.record_signing_key.

Validation reuses signing.decode_multikey — the exact ed25519-Multikey shape gate
the WS trust boundary applies — so a malformed pubkey is a clean 400 here, not a
stored value the carrier would later echo.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..domain import signing, signing_keys_service as svc
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["keys"])

# Cap the accepted pubkey string at the boundary (defense in depth before the
# base58 bigint decode); matches signing._MAX_PUBKEY_STR and the column width.
_MAX_PUBKEY_STR = 128

# Per-user cap on EXPLICIT key registration (cage-match Tesla). Unlike device
# tokens (issued by APNs/FCM, so naturally bounded), a signing pubkey is any
# client-minted shape-valid Multikey — an authed principal could otherwise POST
# unlimited rows to grief storage and poison the cross-account collision well. A
# real principal needs a handful (one live key, a few across rotations); 32 is
# generous headroom while bounding abuse. The IMPLICIT send path is deliberately
# NOT capped — a real signed message must never fail on a key count; this guards
# only the arbitrary-mint API surface. Re-registering an existing key (idempotent)
# is always allowed, even at the cap.
#
# This is a SOFT abuse-mitigation limit, NOT a correctness invariant — the SAME
# class as the per-IP fixed-window `rate_limit.py`, and deliberately enforced the
# same approximate way. NAMED TRADEOFF (cage-match Carnot): the count()+get_key
# read below is not atomic with the insert, so concurrent POSTs of distinct keys
# can each observe count < cap before any commits and overshoot. The overshoot is
# BOUNDED, not unbounded: once the first burst commits, count >= cap rejects every
# later new key, so the worst case is cap + one concurrent burst — a few extra tiny
# rows, never runaway growth. Since the stake is storage slop (not an orphaned
# channel or a minted duplicate — the classes that DO get the atomic
# fold-into-write guard here), an exact conditional-insert guard would be
# gold-plating a soft limit the codebase elsewhere rate-limits approximately. If a
# future consumer makes the roster load-bearing (federation adjudication, #1760),
# revisit with a real quota counter or guarded write.
_MAX_KEYS_PER_USER = 32


class RegisterKeyReq(BaseModel):
    # The multibase-base58btc ed25519 Multikey string (`z…`). Shape-validated in the
    # handler via signing.decode_multikey; the length cap here is a cheap first gate.
    pubkey: str = Field(min_length=1, max_length=_MAX_PUBKEY_STR)
    # The app's announced key version (>= 1). ge=1 rejects 0/negative at the boundary.
    key_version: int = Field(default=1, ge=1)


class KeyView(BaseModel):
    pubkey: str
    key_version: int
    first_seen_at: str
    last_seen_at: str


@router.post("/keys", status_code=status.HTTP_201_CREATED)
async def register_key(
    req: RegisterKeyReq, user: CurrentUser, session: DbSession
) -> dict:
    """Register (or re-observe) the current user's signing public key. Idempotent:
    re-registering the same key is a no-op bump of last_seen_at, still 201.

    Bound to the authenticated user — a body ``user_id`` is neither accepted nor
    consulted. A malformed pubkey (not a well-formed ed25519 Multikey) is a 400,
    rejected before any row is written."""
    try:
        signing.decode_multikey(req.pubkey)  # shape gate — raises OriginError if invalid
    except signing.OriginError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid signing pubkey: {e}") from e
    # Per-user cap — but only for a genuinely NEW key. A re-registration of an
    # already-recorded key is an idempotent bump and must stay allowed even at the
    # cap (else revoking to make room would be forced). This read is NOT atomic with
    # the insert (see the _MAX_KEYS_PER_USER note): concurrent POSTs of distinct keys
    # can overshoot by up to one burst, bounded because a committed burst then blocks
    # further new keys. Accepted for a soft storage-abuse limit.
    if await svc.get_key(session, user_id=user.id, pubkey=req.pubkey) is None:
        if await svc.count_keys(session, user.id) >= _MAX_KEYS_PER_USER:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"signing-key limit reached ({_MAX_KEYS_PER_USER}); "
                       "revoke an unused key before registering another")
    row = await svc.record_signing_key(
        session, user_id=user.id, pubkey=req.pubkey, key_version=req.key_version)
    # record_signing_key does not commit (caller owns the txn) — commit the
    # single-row explicit write here.
    await session.commit()
    return {"pubkey": row.pubkey, "key_version": row.key_version}


@router.get("/keys")
async def list_keys(user: CurrentUser, session: DbSession) -> list[KeyView]:
    """The current user's registered/observed signing keys. Scoped to the
    authenticated user — never another account's roster."""
    rows = await svc.list_keys(session, user.id)
    return [
        KeyView(
            pubkey=r.pubkey, key_version=r.key_version,
            first_seen_at=r.first_seen_at.isoformat(),
            last_seen_at=r.last_seen_at.isoformat(),
        )
        for r in rows
    ]


@router.delete("/keys/{pubkey}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    pubkey: str, user: CurrentUser, session: DbSession
) -> None:
    """Revoke (forget) one of the caller's signing keys. 204 whether or not the key
    was present — revoking an unknown key is not an error (idempotent), and a 404
    would leak whether a given key is on file. Scoped to the authenticated user, so
    a caller can never revoke another account's binding (the pubkey is public).

    No shape validation: a malformed pubkey simply matches no row and 204s — the
    same leak-free idempotent behavior as devices' unregister."""
    await svc.revoke_key(session, user_id=user.id, pubkey=pubkey)
