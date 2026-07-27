"""The shared session resolver (auth_session) — the single per-user session gate
that REST, refresh, and the WS handshake all funnel through (#1927 /
concept_auth_ingress_fragmentation).

These pin the resolver's CONTRACT directly (four rejection paths + the happy
paths for both token types). The HTTP/WS *rendering* of these neutral outcomes is
covered by the per-ingress enforcement suites (test_moderation_actions ban-at-
every-ingress, test_session_revocation) — this file proves the policy they now
share is correct in one place, so a future per-user gate added here is trusted to
be live everywhere.
"""
from __future__ import annotations

import datetime as dt

import pytest

from aiko_gateway.domain import auth_session, security, users_service


async def _user(session, username: str, *, banned: bool = False):
    u = await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")
    if banned:
        u.banned_at = dt.datetime.now(dt.timezone.utc)
        await session.commit()
    return u


async def test_resolves_valid_access_token(session):
    u = await _user(session, "alice")
    got = await auth_session.resolve_session_user(
        session, security.issue_access(u.id, gen=u.token_generation),
        expected_type="access")
    assert got.id == u.id


async def test_resolves_valid_refresh_token(session):
    """The refresh ingress funnels through the same resolver with expected_type=refresh."""
    u = await _user(session, "bob")
    got = await auth_session.resolve_session_user(
        session, security.issue_refresh(u.id, gen=u.token_generation),
        expected_type="refresh")
    assert got.id == u.id


async def test_garbage_token_is_invalid_session(session):
    with pytest.raises(auth_session.InvalidSession):
        await auth_session.resolve_session_user(
            session, "not.a.jwt", expected_type="access")


async def test_wrong_token_type_is_invalid_session(session):
    """A refresh token presented where an access token is expected is rejected —
    the type is a signed claim (security.decode_token enforces it)."""
    u = await _user(session, "carol")
    with pytest.raises(auth_session.InvalidSession):
        await auth_session.resolve_session_user(
            session, security.issue_refresh(u.id, gen=u.token_generation),
            expected_type="access")


async def test_unknown_user_is_invalid_session(session):
    """Validly-signed token for a user row that doesn't exist → neutral rejection
    (existence collapsed into InvalidSession, no separate 'user not found')."""
    with pytest.raises(auth_session.InvalidSession):
        await auth_session.resolve_session_user(
            session, security.issue_access("00000000000000000000000000", gen=0),
            expected_type="access")


async def test_stale_generation_is_invalid_session(session):
    """Revocation (#1914): a token minted at an older generation is dead."""
    u = await _user(session, "dave")
    stale = security.issue_access(u.id, gen=u.token_generation)  # gen 0
    u.token_generation += 1  # a recovery re-key bumps it
    await session.commit()
    with pytest.raises(auth_session.InvalidSession):
        await auth_session.resolve_session_user(session, stale, expected_type="access")


async def test_banned_user_is_session_banned(session):
    """Ban (Piece B): a suspended account is a DISTINCT signal from InvalidSession —
    the REST adapters render it as the structured 403, WS as 1008."""
    u = await _user(session, "erin", banned=True)
    tok = security.issue_access(u.id, gen=u.token_generation)
    with pytest.raises(auth_session.SessionBanned):
        await auth_session.resolve_session_user(session, tok, expected_type="access")


async def test_banned_and_stale_is_invalid_session_not_banned(session):
    """Precedence / non-leak ordering: a banned user presenting a REVOKED-generation
    token gets the opaque InvalidSession, NOT SessionBanned. The revocation/existence
    gate is checked before the ban gate, so a dead token never reveals suspension —
    the ban signal (structured 403) is only exposed to a token that is otherwise
    valid. Locks the check order against a future reshuffle."""
    u = await _user(session, "frank", banned=True)
    stale = security.issue_access(u.id, gen=u.token_generation)  # gen 0
    u.token_generation += 1
    await session.commit()
    with pytest.raises(auth_session.InvalidSession):
        await auth_session.resolve_session_user(session, stale, expected_type="access")
