"""Server-issued single-use nonce store (#13 option a) — exercised, not asserted.

The atomic single-use guarantee IS the security property: a captured /social
request must not replay, because the nonce is already burned. These drive the real
nonce_service against a throwaway SQLite session (the same harness as the rest).
"""
from __future__ import annotations

from aiko_gateway.config import settings
from aiko_gateway.domain import nonce_service


async def test_issue_returns_high_entropy_nonce(session):
    nonce = await nonce_service.issue_nonce(session)
    assert isinstance(nonce, str)
    assert len(nonce) >= 32  # token_urlsafe(32) -> 43 chars, 256 bits


async def test_issue_then_consume_once(session):
    nonce = await nonce_service.issue_nonce(session)
    assert await nonce_service.consume_nonce(session, nonce) is True


async def test_single_use_second_consume_fails(session):
    """The replay guard: a nonce redeems exactly once; the second attempt (a
    replayed request) fails closed."""
    nonce = await nonce_service.issue_nonce(session)
    assert await nonce_service.consume_nonce(session, nonce) is True
    assert await nonce_service.consume_nonce(session, nonce) is False


async def test_unissued_nonce_rejected(session):
    assert await nonce_service.consume_nonce(session, "never-issued") is False


async def test_expired_nonce_rejected(session, monkeypatch):
    """The expires_at guard in the conditional UPDATE: an expired nonce can't be
    consumed even though the row exists."""
    monkeypatch.setattr(settings, "social_nonce_ttl_seconds", -1)  # born expired
    nonce = await nonce_service.issue_nonce(session)
    assert await nonce_service.consume_nonce(session, nonce) is False


async def test_issue_prunes_expired_rows(session, monkeypatch):
    """Opportunistic cleanup (Carnot PR#33): issuing prunes already-expired rows so
    an unauthenticated flood can't grow the table without bound."""
    from sqlalchemy import func, select

    from aiko_gateway.domain.models import SocialNonce

    monkeypatch.setattr(settings, "social_nonce_ttl_seconds", -1)
    await nonce_service.issue_nonce(session)               # born expired
    monkeypatch.setattr(settings, "social_nonce_ttl_seconds", 600)
    await nonce_service.issue_nonce(session)               # prunes the expired one
    count = (await session.execute(
        select(func.count()).select_from(SocialNonce))).scalar()
    assert count == 1                                      # only the fresh row remains
