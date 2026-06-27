"""Social sign-in endpoints (#13) — full request path with the verifier mocked.

The OAuth verify boundary itself is exercised in test_oauth_verify.py with real
tokens. Here we mock `oauth.verify_id_token` (inject a VerifiedIdentity or an
error) and drive the REST flow: new-user → provisioning → claim → tokens;
returning user → tokens; the disabled gate; and each verify failure mapped to its
honest status code. The app is built from just the auth router (no
aiko_gateway.main → no aiko_services), mirroring test_register_gate.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiko_gateway.config import settings
from aiko_gateway.domain import oauth, security, users_service
from aiko_gateway.domain.oauth import VerifiedIdentity
from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest.deps import get_session


@pytest_asyncio.fixture
async def client(session, monkeypatch):
    monkeypatch.setattr(settings, "social_signin_enabled", True)

    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _mock_verify(monkeypatch, identity=None, exc=None):
    async def _fake(provider, id_token, *, expected_nonce=None):
        if exc is not None:
            raise exc
        return identity

    monkeypatch.setattr(oauth, "verify_id_token", _fake)


_IDENTITY = VerifiedIdentity(
    provider="google", sub="g-sub-1", email="nick@example.com",
    suggested_name="Nick M")


async def test_new_user_provisioning_then_claim(client, monkeypatch, session):
    _mock_verify(monkeypatch, identity=_IDENTITY)
    # First contact: unknown identity → provisioning token, no tokens yet.
    r1 = await client.post("/v1/auth/social",
                           json={"provider": "google", "id_token": "x"})
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body["status"] == "provisioning"
    assert body["suggested_name"] == "Nick M"
    assert "access_token" not in body
    prov = body["provisioning_token"]

    # Claim a handle → real account + tokens.
    r2 = await client.post("/v1/auth/social/claim", json={
        "provisioning_token": prov, "handle": "nick", "display_name": "Nick"})
    assert r2.status_code == 200, r2.text
    claimed = r2.json()
    assert "access_token" in claimed and "refresh_token" in claimed
    assert claimed["user"]["username"] == "nick"

    # The identity is now linked: a second /social returns tokens directly.
    r3 = await client.post("/v1/auth/social",
                           json={"provider": "google", "id_token": "x"})
    assert r3.status_code == 200
    assert "access_token" in r3.json()
    assert r3.json().get("status") != "provisioning"


async def test_returning_user_skips_provisioning(client, monkeypatch, session):
    # Pre-create the linked user.
    await users_service.create_social_user(
        session, provider="google", provider_sub="g-sub-1",
        handle="existing", display_name="Existing", email=None)
    _mock_verify(monkeypatch, identity=_IDENTITY)
    r = await client.post("/v1/auth/social",
                          json={"provider": "google", "id_token": "x"})
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "existing"
    assert "access_token" in r.json()


async def test_handle_conflict_returns_409(client, monkeypatch, session):
    await users_service.create_social_user(
        session, provider="apple", provider_sub="a-sub-9",
        handle="taken", display_name="T", email=None)
    _mock_verify(monkeypatch, identity=_IDENTITY)
    r1 = await client.post("/v1/auth/social",
                           json={"provider": "google", "id_token": "x"})
    prov = r1.json()["provisioning_token"]
    r2 = await client.post("/v1/auth/social/claim", json={
        "provisioning_token": prov, "handle": "taken", "display_name": "Nope"})
    assert r2.status_code == 409


async def test_disabled_returns_403(client, monkeypatch):
    monkeypatch.setattr(settings, "social_signin_enabled", False)
    _mock_verify(monkeypatch, identity=_IDENTITY)
    r = await client.post("/v1/auth/social",
                          json={"provider": "google", "id_token": "x"})
    assert r.status_code == 403
    r2 = await client.post("/v1/auth/social/claim", json={
        "provisioning_token": "y", "handle": "h"})
    assert r2.status_code == 403


async def test_invalid_provider_token_returns_401(client, monkeypatch):
    _mock_verify(monkeypatch, exc=oauth.InvalidProviderToken("bad sig"))
    r = await client.post("/v1/auth/social",
                          json={"provider": "google", "id_token": "x"})
    assert r.status_code == 401


async def test_provider_outage_returns_503(client, monkeypatch):
    _mock_verify(monkeypatch, exc=oauth.ProviderUnavailable("down"))
    r = await client.post("/v1/auth/social",
                          json={"provider": "google", "id_token": "x"})
    assert r.status_code == 503


async def test_unknown_provider_rejected_at_boundary_422(client, monkeypatch):
    # provider is a Provider StrEnum on the request model now, so an unsupported
    # provider is a 422 at validation — it never reaches verify_id_token. (The
    # UnknownProvider -> 400 path remains as defense-in-depth, unit-tested in
    # test_oauth_verify.test_unknown_provider_rejected.)
    _mock_verify(monkeypatch, identity=_IDENTITY)
    r = await client.post("/v1/auth/social",
                          json={"provider": "facebook", "id_token": "x"})
    assert r.status_code == 422


@pytest.mark.parametrize("handle", ["", "   ", "x" * 65])
async def test_claim_rejects_bad_handle_422(client, monkeypatch, handle):
    # Empty, whitespace-only, and overlong handles are rejected at the boundary
    # (422), not deferred to DB behaviour. Validation precedes the endpoint, so a
    # bogus provisioning_token is irrelevant here.
    _mock_verify(monkeypatch, identity=_IDENTITY)
    r = await client.post("/v1/auth/social/claim", json={
        "provisioning_token": "irrelevant", "handle": handle})
    assert r.status_code == 422


async def test_claim_strips_handle_whitespace(client, monkeypatch):
    # A handle with surrounding whitespace is stripped before becoming the
    # username (not stored verbatim).
    _mock_verify(monkeypatch, identity=_IDENTITY)
    r1 = await client.post("/v1/auth/social",
                           json={"provider": "google", "id_token": "x"})
    prov = r1.json()["provisioning_token"]
    r2 = await client.post("/v1/auth/social/claim", json={
        "provisioning_token": prov, "handle": "  nick  ", "display_name": "  Nick  "})
    assert r2.status_code == 200, r2.text
    assert r2.json()["user"]["username"] == "nick"


async def test_claim_rejects_forged_provisioning_token(client, monkeypatch):
    _mock_verify(monkeypatch, identity=_IDENTITY)
    r = await client.post("/v1/auth/social/claim", json={
        "provisioning_token": "not-a-real-token", "handle": "nick"})
    assert r.status_code == 401


async def test_claim_rejects_an_access_token_as_provisioning(client, monkeypatch):
    """A non-provisioning token (e.g. a stolen access token) must not be usable at
    /claim — the type discriminator rejects it."""
    _mock_verify(monkeypatch, identity=_IDENTITY)
    access = security.issue_access("some-user-id")
    r = await client.post("/v1/auth/social/claim", json={
        "provisioning_token": access, "handle": "nick"})
    assert r.status_code == 401


async def test_social_only_user_cannot_password_login(session):
    """The social bypass guard: a password-less account (password_hash=None) must
    never authenticate via username/password, whatever password is supplied."""
    await users_service.create_social_user(
        session, provider="google", provider_sub="g-sub-2",
        handle="socialonly", display_name="S", email=None)
    assert await users_service.authenticate(session, "socialonly", "") is None
    assert await users_service.authenticate(session, "socialonly", "anything") is None
