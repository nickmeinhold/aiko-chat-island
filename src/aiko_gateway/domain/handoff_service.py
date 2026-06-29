"""One-time handoff store for the OAuth broker flow (#21).

The broker callback completes the authorization-code exchange server-side and
must hand the result back to the app WITHOUT putting minted tokens in a redirect
URL. So it stores a MINIMAL outcome payload here under a fresh random code and
redirects the browser with only the opaque code; the app redeems it once via
POST /v1/auth/oauth/exchange.

Two operations:
  * create_handoff — stash a minimal JSON payload under a cryptographically random
    code with a short TTL. NEVER stores minted tokens.
  * consume_handoff — atomically redeem a code exactly once. Returns the payload
    (and clears it) or None if missing / expired / already consumed. The atomic
    guard closes the double-spend race (two concurrent /exchange calls for the
    same code → at most one wins).
"""
from __future__ import annotations

import datetime as dt
import json
import secrets

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from .models import OAuthHandoff


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def create_handoff(session: AsyncSession, payload: dict) -> str:
    """Store the minimal outcome payload and return its single-use code. The code
    is secrets.token_urlsafe(32) — 256 bits of entropy, unguessable."""
    code = secrets.token_urlsafe(32)
    row = OAuthHandoff(
        code=code,
        payload=json.dumps(payload),
        expires_at=_utcnow() + dt.timedelta(
            seconds=settings.oauth_handoff_ttl_seconds),
        consumed=False,
    )
    session.add(row)
    await session.commit()
    return code


async def consume_handoff(session: AsyncSession, code: str) -> dict | None:
    """Atomically consume a handoff code exactly once.

    Returns the decoded payload if the code exists, is not expired, and was not
    already consumed; otherwise None. The single-use guarantee is the WHERE
    clause on the UPDATE: it flips consumed=False -> True only for an unexpired,
    unconsumed row, and rowcount tells us whether THIS call won. Two concurrent
    redemptions race on that conditional UPDATE; at most one gets rowcount==1.
    This is the double-spend guard — we never read-then-write (which would TOCTOU),
    we let the DB arbitrate via the conditional update.
    """
    now = _utcnow()
    result = await session.execute(
        update(OAuthHandoff)
        .where(
            OAuthHandoff.code == code,
            OAuthHandoff.consumed == False,  # noqa: E712 (SQL boolean, not Python)
            OAuthHandoff.expires_at > now,
        )
        .values(consumed=True)
    )
    if result.rowcount != 1:
        # Missing, expired, or already consumed — fail closed (caller -> 401).
        await session.commit()
        return None
    # We won the race. Read the row's payload (now safely ours).
    row = await session.get(OAuthHandoff, code)
    await session.commit()
    if row is None:  # pragma: no cover — defensive; the UPDATE just matched it
        return None
    return json.loads(row.payload)
