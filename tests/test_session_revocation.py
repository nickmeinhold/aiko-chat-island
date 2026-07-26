"""Session revocation via per-user token_generation (#1914).

The LIFT CONDITION for Design 05 social recovery: bumping a user's
`token_generation` must invalidate EVERY previously-issued JWT (access + refresh)
at EVERY authentication ingress, while a token minted at the current generation
still works. Stateless HS256 tokens have no revocation on their own; the `gen`
claim + a live equality check against the DB row is the revocation mechanism.

These are the ingress-completeness twin of the ban tests
(test_moderation_actions): a ban rejects a suspended ROW, token_generation
rejects a stale SESSION. Both must gate all three ingresses — REST
get_current_user, token refresh, and the WS handshake — or the revocation leaks
(the auth-ingress fragmentation risk, #1927). The WS handshake in particular is
the ingress the task's prose fix-list omitted; it authenticates outside
get_current_user via decode_token directly, so it gets its own test here.

Migration safety is asserted too: a token minted BEFORE this feature carries no
`gen` claim and must read as generation 0, so a deploy doesn't mass-logout every
live session (the default column value is also 0).
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from starlette.websockets import WebSocketDisconnect

from aiko_gateway.config import settings
from aiko_gateway.domain import security, users_service
from aiko_gateway.realtime import ws as ws_module
from aiko_gateway.realtime.hub import Hub
from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest.deps import get_current_user

pytestmark = pytest.mark.asyncio


async def _user(session, name="alice"):
    return await users_service.create_user(
        session, username=name, display_name=name.title(), password="pw")


async def _bump_generation(session, user, to: int) -> None:
    """Simulate a revocation: advance the user's token_generation and commit so a
    fresh SessionLocal (the WS ingress opens its own) sees the new value."""
    user.token_generation = to
    await session.commit()


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


# -- 1. the claim round-trips + migration safety ----------------------------- #

async def test_issue_embeds_generation_and_decode_returns_it():
    token = security.issue_access("u1", gen=7)
    sub, gen = security.decode_token(token, expected_type="access")
    assert sub == "u1"
    assert gen == 7


async def test_absent_gen_claim_reads_as_zero():
    """A token minted before this feature has no `gen` claim. It must decode to
    generation 0 so it still validates against the default token_generation=0 —
    no mass-logout on deploy. (An attacker can't strip the claim: HS256 signs it.)"""
    now = dt.datetime.now(dt.timezone.utc)
    legacy = jwt.encode(
        {"sub": "u1", "type": "access", "iat": int(now.timestamp()),
         "exp": int((now + dt.timedelta(hours=1)).timestamp())},
        settings.jwt_secret, algorithm=settings.jwt_algorithm)
    sub, gen = security.decode_token(legacy, expected_type="access")
    assert sub == "u1"
    assert gen == 0


@pytest.mark.parametrize("bad_gen", ["not-int", None, -1, True, 1.5])
async def test_malformed_gen_claim_fails_closed(bad_gen):
    """decode_token is the trust-boundary parser: a signed-but-malformed `gen`
    (string / JSON-null / negative / bool / float) must raise InvalidTokenError, not
    a raw ValueError/TypeError that escapes the ingress try/except as a 500. Only we
    mint these (HS256), so this is defense-in-depth — same fail-closed posture as
    validate_origin. Cage-match Carnot+Tesla, PR#94."""
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode(
        {"sub": "u1", "type": "access", "gen": bad_gen, "iat": int(now.timestamp()),
         "exp": int((now + dt.timedelta(hours=1)).timestamp())},
        settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(jwt.InvalidTokenError):
        security.decode_token(tok, expected_type="access")


# -- 2. REST ingress: get_current_user --------------------------------------- #

async def test_get_current_user_rejects_stale_generation(session):
    user = await _user(session)
    stale = security.issue_access(user.id, gen=0)   # gen at issue time
    await _bump_generation(session, user, to=1)     # revoke everything at gen 0

    with pytest.raises(HTTPException) as ei:
        await get_current_user(_creds(stale), session)
    assert ei.value.status_code == status.HTTP_401_UNAUTHORIZED

    fresh = security.issue_access(user.id, gen=1)
    got = await get_current_user(_creds(fresh), session)
    assert got.id == user.id


# -- 3. Refresh ingress ------------------------------------------------------ #

async def test_refresh_rejects_stale_generation_and_remints_current(session):
    user = await _user(session)
    stale_refresh = security.issue_refresh(user.id, gen=0)
    await _bump_generation(session, user, to=1)

    with pytest.raises(HTTPException) as ei:
        await auth_routes.refresh(
            auth_routes.RefreshReq(refresh_token=stale_refresh), session)
    assert ei.value.status_code == status.HTTP_401_UNAUTHORIZED

    fresh_refresh = security.issue_refresh(user.id, gen=1)
    out = await auth_routes.refresh(
        auth_routes.RefreshReq(refresh_token=fresh_refresh), session)
    # The re-minted access token must carry the CURRENT generation, so it is
    # accepted downstream (not a token that will itself be rejected).
    _, gen = security.decode_token(out["access_token"], expected_type="access")
    assert gen == 1


# -- 4. WS handshake ingress (the one the prose fix-list omitted) ------------- #

async def _run_handshake(session, monkeypatch, token):
    """Drive ws_endpoint against a stub socket, returning (accepted, close_code).
    Mirrors test_moderation_actions._run_handshake; the WS opens its OWN
    SessionLocal, so we monkeypatch it to the test session."""
    class _CM:
        async def __aenter__(self): return session
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(ws_module, "SessionLocal", lambda: _CM())

    class _StubWS:
        def __init__(self, tok):
            self.query_params = {"token": tok}
            self.app = SimpleNamespace(
                state=SimpleNamespace(gw=SimpleNamespace(hub=Hub())))
            self.closed_code = None
            self.accepted = False
        async def close(self, code=1000): self.closed_code = code
        async def accept(self): self.accepted = True
        async def receive_json(self): raise WebSocketDisconnect()

    stub = _StubWS(token)
    await ws_module.ws_endpoint(stub)
    return stub.accepted, stub.closed_code


async def test_ws_handshake_rejects_stale_generation(session, monkeypatch):
    user = await _user(session)
    stale = security.issue_access(user.id, gen=0)
    await _bump_generation(session, user, to=1)

    accepted, code = await _run_handshake(session, monkeypatch, stale)
    assert accepted is False and code == status.WS_1008_POLICY_VIOLATION

    fresh = security.issue_access(user.id, gen=1)
    accepted, code = await _run_handshake(session, monkeypatch, fresh)
    assert accepted is True and code is None
