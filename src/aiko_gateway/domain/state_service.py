"""One-time CSRF/PKCE state nonce store for the OAuth broker flow (#21).

Replaces the earlier self-contained signed-JWT ``state`` (cage-match #30,
Finding 1). The broker /start mints an opaque nonce, stashes the provider (and,
for PKCE providers, the ``code_verifier``) here, and sends ONLY the nonce to the
provider as ``state``. The /callback redeems the nonce ATOMICALLY exactly once.

Why a store and not a signed token:
  * PKCE — the verifier stays SERVER-SIDE; only the code_challenge ever leaves us.
    A stateless JWT had to carry the verifier through the browser/provider
    (base64-readable), defeating PKCE.
  * REPLAY / login-CSRF — single-use ``consumed`` + ``expires_at`` mean a captured
    callback URL can't be replayed at the state layer. A signed-stateless state is
    replayable within its exp window.

Two operations (same shape as handoff_service):
  * create_state — stash provider + optional code_verifier under a random nonce
    with a short TTL.
  * consume_state — atomically redeem a nonce exactly once. Returns
    {"provider", "code_verifier"} or None if missing / expired / already consumed.
    The atomic guard closes the double-spend / replay race (a conditional UPDATE
    arbitrated by rowcount — NEVER a read-then-write TOCTOU).
"""
from __future__ import annotations

import datetime as dt
import secrets

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from .models import OAuthState


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def create_state(
    session: AsyncSession, *, provider: str, code_verifier: str | None,
) -> str:
    """Store the provider (+ optional PKCE code_verifier) and return its single-use
    nonce. The nonce is secrets.token_urlsafe(32) — 256 bits of entropy,
    unguessable. The code_verifier (if any) NEVER leaves the server."""
    nonce = secrets.token_urlsafe(32)
    row = OAuthState(
        nonce=nonce,
        provider=provider,
        code_verifier=code_verifier,
        expires_at=_utcnow() + dt.timedelta(
            seconds=settings.oauth_state_ttl_seconds),
        consumed=False,
    )
    session.add(row)
    await session.commit()
    return nonce


async def consume_state(session: AsyncSession, nonce: str) -> dict | None:
    """Atomically consume a state nonce exactly once.

    Returns {"provider", "code_verifier"} if the nonce exists, is not expired, and
    was not already consumed; otherwise None. The single-use guarantee is the
    WHERE clause on the UPDATE: it flips consumed=False -> True only for an
    unexpired, unconsumed row, and rowcount tells us whether THIS call won. Two
    concurrent redemptions race on that conditional UPDATE; at most one gets
    rowcount==1. This is the single-use + double-spend + replay guard — we never
    read-then-write (which would TOCTOU), we let the DB arbitrate via the
    conditional update.
    """
    now = _utcnow()
    result = await session.execute(
        update(OAuthState)
        .where(
            OAuthState.nonce == nonce,
            OAuthState.consumed == False,  # noqa: E712 (SQL boolean, not Python)
            OAuthState.expires_at > now,
        )
        .values(consumed=True)
    )
    if result.rowcount != 1:
        # Missing, expired, or already consumed — fail closed (caller -> bad_state).
        await session.commit()
        return None
    # We won the race. Read the row (now safely ours).
    row = await session.get(OAuthState, nonce)
    await session.commit()
    if row is None:  # pragma: no cover — defensive; the UPDATE just matched it
        return None
    return {"provider": row.provider, "code_verifier": row.code_verifier}
