"""Server-issued single-use nonce store for the NATIVE social sign-in flow (#13,
option (a)).

PR#32 wired an APP-supplied nonce (option (b)): the app generated it and sent it
beside the id_token, so the 'expected' value was NOT independent server state — a
captured request body replays (and for Google the raw nonce is readable straight
out of the token). This module closes that window: the GATEWAY issues the nonce,
stores it, and redeems it ATOMICALLY exactly once at /v1/auth/social. A replayed
request fails because the nonce is already burned.

Two operations, the same shape (and the same single-use guarantee) as
state_service — but with no provider/PKCE payload, since a native nonce is
provider-agnostic at issue time:
  * issue_nonce  — mint a 256-bit nonce, store it with a short TTL, return it.
  * consume_nonce — atomically redeem a nonce exactly once. Returns True iff the
    nonce existed, was unexpired, and was not already consumed. The single-use
    guarantee is the WHERE clause on the UPDATE (consumed=False AND not expired),
    arbitrated by rowcount — NEVER a read-then-write TOCTOU.
"""
from __future__ import annotations

import datetime as dt
import secrets

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from .models import SocialNonce


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def issue_nonce(session: AsyncSession) -> str:
    """Mint and store a single-use nonce, returning it. secrets.token_urlsafe(32)
    is 256 bits — unguessable, so an attacker can't forge an 'issued' nonce; they
    must capture a real one, and capture-then-replay is exactly what the single-use
    consume defeats."""
    # Opportunistic cleanup: drop already-expired rows so an UNAUTHENTICATED flood
    # of /nonce can't grow the table without bound (cage-match PR#33, Carnot HIGH —
    # a short TTL makes a row UNUSABLE but not GONE). Bounded work (only rows past
    # expiry), piggy-backed on issue so there's no separate sweeper at this scale;
    # a periodic sweep / rate-limit is the follow-up if volume ever warrants it.
    await session.execute(
        delete(SocialNonce).where(SocialNonce.expires_at <= _utcnow()))
    nonce = secrets.token_urlsafe(32)
    session.add(SocialNonce(
        nonce=nonce,
        expires_at=_utcnow() + dt.timedelta(
            seconds=settings.social_nonce_ttl_seconds),
        consumed=False,
    ))
    await session.commit()
    return nonce


async def consume_nonce(session: AsyncSession, nonce: str) -> bool:
    """Atomically claim a nonce exactly once, WITHOUT committing. True iff the
    nonce exists, is unexpired, and was not already consumed; otherwise False
    (missing / expired / already-consumed / forged → caller fails closed).

    The single-use guarantee is the conditional UPDATE: it flips consumed False ->
    True only for an unexpired, unconsumed row, and rowcount tells us whether THIS
    call won. Two concurrent redemptions race on that UPDATE; at most one gets
    rowcount==1. No read-then-write (which would TOCTOU) — the DB arbitrates. This
    is the property that makes a captured-and-replayed sign-in request fail.

    ATOMIC-WITH-OUTCOME (#24): this does NOT commit. The conditional UPDATE leaves
    the row locked for the rest of the request, so concurrent replays collapse to
    at most one COMMITTED winner (a claim whose request rolls back leaves the nonce
    usable — the intended retry path), but the burn is not made DURABLE until the
    caller commits — which it does only after the sign-in OUTCOME succeeds. So a transient
    failure AFTER the claim (e.g. _resolve_identity) rolls back the burn and the
    app may retry the SAME nonce. The caller MUST commit on success (and let the
    request session roll back on failure). Safe to defer the commit here ONLY
    because the native /social path does no network IO between this claim and the
    commit; the broker /callback path is DELIBERATELY not symmetric (it has the
    provider code-exchange in that gap) and commits its state burn eagerly — see
    state_service.consume_state.
    """
    now = _utcnow()
    result = await session.execute(
        update(SocialNonce)
        .where(
            SocialNonce.nonce == nonce,
            SocialNonce.consumed == False,  # noqa: E712 (SQL boolean, not Python)
            SocialNonce.expires_at > now,
        )
        .values(consumed=True)
    )
    return result.rowcount == 1
