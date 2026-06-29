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


async def test_consume_does_not_commit_so_rollback_un_burns(session):
    """#24 atomic-with-outcome: consume_nonce CLAIMS the nonce but does NOT commit,
    so if the caller's request rolls back (a downstream sign-in failure) the burn is
    undone and the SAME nonce works again. Pre-#24 consume committed eagerly, so a
    rollback could not un-burn it and the user was stranded with a spent nonce.

    RED-proof: re-add `await session.commit()` to consume_nonce and the second
    consume returns False (the burn was made durable before the rollback)."""
    nonce = await nonce_service.issue_nonce(session)            # commits the issued row
    assert await nonce_service.consume_nonce(session, nonce) is True   # claim (uncommitted)
    await session.rollback()                                   # models per-request session
    # The claim was never made durable → the nonce is consumable again.
    assert await nonce_service.consume_nonce(session, nonce) is True


async def test_caller_commit_makes_burn_durable(session):
    """Mirror image: once the caller COMMITS the claim, a later rollback cannot
    revive the nonce — the burn is durable (cross-request replay stays closed)."""
    nonce = await nonce_service.issue_nonce(session)
    assert await nonce_service.consume_nonce(session, nonce) is True
    await session.commit()                                     # the handler's success commit
    await session.rollback()                                   # a later rollback must not revive it
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
