"""OAuth broker (#21, increment 2) — the server-side authorization-code flow.

Two layers, mirroring the social split:
  * UNIT (this file, lower half): exchange_code / build_authorize_url against a
    faked httpx layer (the convention from test_oauth_verify
    .test_real_refresh_rejects_malformed_shapes — stub httpx.AsyncClient).
  * REST (upper half): the full /start /callback /exchange /providers path over an
    ASGI client (the convention from test_social_signin), with exchange_code
    mocked to inject a VerifiedIdentity or a typed error.

Every bypass case from the security acceptance list is a test here, named so the
PR can map case -> test.
"""
from __future__ import annotations

import datetime as dt
import json

import httpx
import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI

import asyncio

from aiko_gateway.config import settings
from aiko_gateway.domain import (
    handoff_service, oauth_broker, security, state_service, users_service,
)
from aiko_gateway.domain.models import OAuthHandoff, OAuthState
from aiko_gateway.domain.oauth import VerifiedIdentity
from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest.deps import get_session
from httpx import ASGITransport, AsyncClient


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def client(session, monkeypatch):
    """Router-only ASGI app with github configured + the test DB session."""
    # Broker endpoints share the social-signin kill-switch (cage-match #30 r2):
    # they 403 / list nothing when social_signin_enabled is False. The success
    # paths below all require it ON.
    monkeypatch.setattr(settings, "social_signin_enabled", True)
    monkeypatch.setattr(settings, "github_client_id", "gh-client-id")
    monkeypatch.setattr(settings, "github_client_secret", "gh-secret")
    monkeypatch.setattr(settings, "app_oauth_callback_url", "aikochat://auth")
    monkeypatch.setattr(settings, "gateway_base_url",
                        "https://chat.imagineering.cc")

    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()


_IDENTITY = VerifiedIdentity(
    provider="github", sub="gh-123", email="dev@example.com",
    suggested_name="Dev Eloper")


def _mock_exchange(monkeypatch, identity=None, exc=None, capture=None):
    async def _fake(provider, *, code, code_verifier=None):
        if capture is not None:
            capture["code_verifier"] = code_verifier
        if exc is not None:
            raise exc
        return identity
    monkeypatch.setattr(oauth_broker, "exchange_code", _fake)


async def _mint_state(session, provider="github", code_verifier=None) -> str:
    """Create a SERVER-SIDE single-use state nonce (replaces the old signed-JWT
    state). Returns the opaque nonce to send as the callback `state` param."""
    return await state_service.create_state(
        session, provider=provider, code_verifier=code_verifier)


# --------------------------------------------------------------------------- #
# /start
# --------------------------------------------------------------------------- #
async def test_start_redirects_to_provider_with_stored_state_nonce(client, session):
    r = await client.get("/v1/auth/oauth/github/start")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://github.com/login/oauth/authorize?")
    q = httpx.QueryParams(loc.split("?", 1)[1])
    assert q["client_id"] == "gh-client-id"
    assert q["redirect_uri"] == (
        "https://chat.imagineering.cc/v1/auth/oauth/github/callback")
    assert q["scope"] == "read:user user:email"
    assert q["response_type"] == "code"
    # GitHub: PKCE OFF — no challenge in the URL.
    assert "code_challenge" not in q
    # `state` is now an OPAQUE server-side nonce (NOT a signed JWT) — it must NOT
    # decode as a JWT, and a matching row must exist in the state store carrying
    # the provider.
    nonce = q["state"]
    with pytest.raises(jwt.InvalidTokenError):
        jwt.decode(nonce, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    row = await session.get(OAuthState, nonce)
    assert row is not None and row.provider == "github" and row.consumed is False


async def test_start_unknown_provider_404(client):
    r = await client.get("/v1/auth/oauth/facebook/start")
    assert r.status_code == 404


async def test_start_unconfigured_provider_404(client, monkeypatch):
    # github with no secret = not configured -> indistinguishable from unknown.
    monkeypatch.setattr(settings, "github_client_secret", "")
    r = await client.get("/v1/auth/oauth/github/start")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# /callback — happy paths
# --------------------------------------------------------------------------- #
async def test_callback_new_identity_stores_provisioning_handoff(
        client, monkeypatch, session):
    _mock_exchange(monkeypatch, identity=_IDENTITY)
    state = await _mint_state(session)
    r = await client.get(
        f"/v1/auth/oauth/github/callback?code=authcode&state={state}")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("aikochat://auth?code=")
    handoff_code = httpx.QueryParams(loc.split("?", 1)[1])["code"]
    # The stored payload is MINIMAL provisioning data — NO minted tokens.
    row = await session.get(OAuthHandoff, handoff_code)
    payload = json.loads(row.payload)
    assert payload["kind"] == "provisioning"
    assert payload["provider"] == "github" and payload["provider_sub"] == "gh-123"
    assert "access_token" not in row.payload


async def test_callback_known_identity_stores_authenticated_handoff(
        client, monkeypatch, session):
    user = await users_service.create_social_user(
        session, provider="github", provider_sub="gh-123",
        handle="dev", display_name="Dev", email=None)
    _mock_exchange(monkeypatch, identity=_IDENTITY)
    state = await _mint_state(session)
    r = await client.get(
        f"/v1/auth/oauth/github/callback?code=authcode&state={state}")
    assert r.status_code == 302
    handoff_code = httpx.QueryParams(
        r.headers["location"].split("?", 1)[1])["code"]
    row = await session.get(OAuthHandoff, handoff_code)
    payload = json.loads(row.payload)
    assert payload == {"kind": "authenticated", "user_id": user.id}
    # No minted tokens at rest.
    assert "access_token" not in row.payload


# --------------------------------------------------------------------------- #
# /callback — failure → graceful redirect-with-error (never a 500)
# --------------------------------------------------------------------------- #
async def test_callback_provider_error_param_redirects_with_error(client, session):
    state = await _mint_state(session)
    r = await client.get(
        f"/v1/auth/oauth/github/callback?error=access_denied&state={state}")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("aikochat://auth?error=")
    assert "code=" not in loc


async def test_callback_bad_state_redirects_with_error(client, monkeypatch):
    _mock_exchange(monkeypatch, identity=_IDENTITY)
    r = await client.get(
        "/v1/auth/oauth/github/callback?code=authcode&state=not-a-token")
    assert r.status_code == 302
    assert "error=bad_state" in r.headers["location"]


async def test_callback_expired_state_redirects_with_error(
        client, monkeypatch, session):
    """An EXPIRED state row (expires_at in the past) is not consumable -> bad_state
    (the store's expires_at predicate)."""
    _mock_exchange(monkeypatch, identity=_IDENTITY)
    expired = OAuthState(
        nonce="expired-state-nonce", provider="github", code_verifier=None,
        expires_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5),
        consumed=False)
    session.add(expired)
    await session.commit()
    r = await client.get(
        "/v1/auth/oauth/github/callback?code=authcode&state=expired-state-nonce")
    assert r.status_code == 302
    assert "error=bad_state" in r.headers["location"]


async def test_callback_unknown_state_nonce_redirects_with_error(
        client, monkeypatch):
    """An opaque nonce that was NEVER issued (forged / guessed) has no row -> the
    store can't consume it -> bad_state. Replaces the old 'tampered JWT signature'
    case: state is no longer a signed token, so unguessable-nonce-not-in-store IS
    the forgery defense."""
    _mock_exchange(monkeypatch, identity=_IDENTITY)
    r = await client.get(
        "/v1/auth/oauth/github/callback?code=authcode&state=never-issued-nonce")
    assert r.status_code == 302
    assert "error=bad_state" in r.headers["location"]


async def test_callback_state_single_use_replay_rejected(
        client, monkeypatch, session):
    """REPLAY / single-use (cage-match #30, Finding 1): a captured callback URL
    replayed with the SAME state nonce fails the second time — the nonce is
    consumed on first use. This is the property the old signed-stateless state
    LACKED (its NAMED TRADEOFF, now retired)."""
    _mock_exchange(monkeypatch, identity=_IDENTITY)
    state = await _mint_state(session)
    r1 = await client.get(
        f"/v1/auth/oauth/github/callback?code=authcode&state={state}")
    assert r1.status_code == 302
    assert "code=" in r1.headers["location"]  # first use succeeds
    # Replay the EXACT same nonce -> rejected (already consumed).
    r2 = await client.get(
        f"/v1/auth/oauth/github/callback?code=authcode&state={state}")
    assert r2.status_code == 302
    assert "error=bad_state" in r2.headers["location"]


async def test_callback_state_provider_mismatch_rejected(
        client, monkeypatch, session):
    """A state minted for a DIFFERENT provider than the path provider is rejected
    (provider stored in the state row must equal the path provider)."""
    _mock_exchange(monkeypatch, identity=_IDENTITY)
    # Stored for provider=apple while the path is /github/.
    state = await _mint_state(session, provider="apple")
    r = await client.get(
        f"/v1/auth/oauth/github/callback?code=authcode&state={state}")
    assert r.status_code == 302
    assert "error=state_provider_mismatch" in r.headers["location"]


async def test_callback_exchange_invalid_redirects_with_error(
        client, monkeypatch, session):
    _mock_exchange(monkeypatch, exc=oauth_broker.BrokerInvalidExchange("bad code"))
    state = await _mint_state(session)
    r = await client.get(
        f"/v1/auth/oauth/github/callback?code=authcode&state={state}")
    assert r.status_code == 302
    assert "error=exchange_failed" in r.headers["location"]


async def test_callback_exchange_unavailable_redirects_with_error(
        client, monkeypatch, session):
    _mock_exchange(monkeypatch, exc=oauth_broker.BrokerUnavailable("down"))
    state = await _mint_state(session)
    r = await client.get(
        f"/v1/auth/oauth/github/callback?code=authcode&state={state}")
    assert r.status_code == 302
    assert "error=provider_unavailable" in r.headers["location"]


async def test_callback_open_redirect_target_is_fixed(
        client, monkeypatch, session):
    """OPEN-REDIRECT defense: no request parameter can change WHERE we redirect.
    Even with an attacker-supplied redirect_uri/next param, the target stays the
    configured app callback host."""
    _mock_exchange(monkeypatch, identity=_IDENTITY)
    state = await _mint_state(session)
    r = await client.get(
        f"/v1/auth/oauth/github/callback?code=authcode&state={state}"
        "&redirect_uri=https://evil.example.com"
        "&next=https://evil.example.com")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("aikochat://auth?")
    assert "evil.example.com" not in loc


# --------------------------------------------------------------------------- #
# /exchange — single-use redemption
# --------------------------------------------------------------------------- #
async def test_exchange_authenticated_returns_tokens(client, monkeypatch, session):
    user = await users_service.create_social_user(
        session, provider="github", provider_sub="gh-123",
        handle="dev", display_name="Dev", email=None)
    code = await handoff_service.create_handoff(
        session, {"kind": "authenticated", "user_id": user.id})
    r = await client.post("/v1/auth/oauth/exchange", json={"code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["username"] == "dev"
    assert "access_token" in body and "refresh_token" in body


async def test_exchange_provisioning_returns_provisioning_token(
        client, monkeypatch, session):
    code = await handoff_service.create_handoff(session, {
        "kind": "provisioning", "provider": "github", "provider_sub": "gh-999",
        "suggested_name": "New Dev", "email": "new@example.com"})
    r = await client.post("/v1/auth/oauth/exchange", json={"code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "provisioning"
    assert body["suggested_name"] == "New Dev"
    assert "access_token" not in body
    # The provisioning token carries the verified (provider, sub) and is claimable.
    decoded = security.decode_provisioning(body["provisioning_token"])
    assert decoded["provider"] == "github" and decoded["provider_sub"] == "gh-999"


async def test_exchange_single_use_second_call_401(client, session):
    """SINGLE-USE: a handoff code redeems exactly once; a 2nd exchange -> 401."""
    code = await handoff_service.create_handoff(session, {
        "kind": "provisioning", "provider": "github", "provider_sub": "gh-1",
        "suggested_name": None, "email": None})
    r1 = await client.post("/v1/auth/oauth/exchange", json={"code": code})
    assert r1.status_code == 200
    r2 = await client.post("/v1/auth/oauth/exchange", json={"code": code})
    assert r2.status_code == 401


async def test_exchange_unknown_code_401(client):
    r = await client.post("/v1/auth/oauth/exchange",
                          json={"code": "never-issued"})
    assert r.status_code == 401


async def test_exchange_expired_code_401(client, session):
    """An expired handoff -> 401 (the consume guard's expires_at predicate)."""
    # Write a handoff row directly with an expiry in the past.
    expired = OAuthHandoff(
        code="expired-code",
        payload=json.dumps({"kind": "provisioning", "provider": "github",
                            "provider_sub": "x", "suggested_name": None,
                             "email": None}),
        expires_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5),
        consumed=False)
    session.add(expired)
    await session.commit()
    r = await client.post("/v1/auth/oauth/exchange",
                          json={"code": "expired-code"})
    assert r.status_code == 401


async def test_exchange_user_deleted_between_callback_and_exchange_401(
        client, session):
    code = await handoff_service.create_handoff(
        session, {"kind": "authenticated", "user_id": "01ABCNOTAREALUSER000000000"})
    r = await client.post("/v1/auth/oauth/exchange", json={"code": code})
    assert r.status_code == 401


async def test_exchange_bogus_kind_fails_closed_401(client, session):
    """KIND-GUARD (cage-match #30, Finding 4): a handoff row whose `kind` is not
    one we write ('authenticated'/'provisioning') is rejected with 401 — it must
    NOT fall through to a KeyError/500."""
    code = await handoff_service.create_handoff(
        session, {"kind": "totally-bogus", "user_id": "x"})
    r = await client.post("/v1/auth/oauth/exchange", json={"code": code})
    assert r.status_code == 401


async def test_exchange_missing_kind_fails_closed_401(client, session):
    """A payload with NO `kind` at all is also rejected (fail-closed)."""
    code = await handoff_service.create_handoff(session, {"user_id": "x"})
    r = await client.post("/v1/auth/oauth/exchange", json={"code": code})
    assert r.status_code == 401


async def test_exchange_authenticated_missing_user_id_fails_closed_401(
        client, session):
    """COMPLETE KIND-GUARD (cage-match #30 r2): an 'authenticated' payload with no
    user_id must 401, NOT KeyError/500 when indexing payload['user_id']."""
    code = await handoff_service.create_handoff(session, {"kind": "authenticated"})
    r = await client.post("/v1/auth/oauth/exchange", json={"code": code})
    assert r.status_code == 401


async def test_exchange_provisioning_missing_fields_fails_closed_401(
        client, session):
    """COMPLETE KIND-GUARD (cage-match #30 r2): a 'provisioning' payload missing
    provider / provider_sub must 401, NOT fall through to indexing them."""
    code = await handoff_service.create_handoff(session, {"kind": "provisioning"})
    r = await client.post("/v1/auth/oauth/exchange", json={"code": code})
    assert r.status_code == 401


async def test_concurrent_handoff_redemption_exactly_one_wins(session):
    """SINGLE-USE under concurrency (cage-match #30, Finding 5): two SIMULTANEOUS
    redemptions of the same handoff code yield exactly one non-None payload.

    NOTE: the aiosqlite test backend serializes writes on a single connection, so
    this may not exercise true wall-clock concurrency. But the assertion holds
    regardless: the rowcount-arbitrated conditional UPDATE in consume_handoff means
    at most one call can flip consumed False->True, so exactly one of the two
    gather'd calls returns the payload and the other returns None. (On Postgres
    with row-level locking the same invariant holds under real concurrency.)"""
    code = await handoff_service.create_handoff(session, {
        "kind": "provisioning", "provider": "github", "provider_sub": "gh-conc",
        "suggested_name": None, "email": None})
    r1, r2 = await asyncio.gather(
        handoff_service.consume_handoff(session, code),
        handoff_service.consume_handoff(session, code),
    )
    winners = [r for r in (r1, r2) if r is not None]
    assert len(winners) == 1


async def test_concurrent_state_consume_exactly_one_wins(session):
    """SINGLE-USE under concurrency for the state nonce (cage-match #30, Finding 5,
    applied to consume_state): two SIMULTANEOUS consumes of the same nonce yield
    exactly one non-None result. Same rowcount-arbitration invariant as the handoff
    store; same aiosqlite-serialization caveat."""
    nonce = await state_service.create_state(
        session, provider="github", code_verifier=None)
    r1, r2 = await asyncio.gather(
        state_service.consume_state(session, nonce),
        state_service.consume_state(session, nonce),
    )
    winners = [r for r in (r1, r2) if r is not None]
    assert len(winners) == 1


# --------------------------------------------------------------------------- #
# /providers
# --------------------------------------------------------------------------- #
async def test_providers_lists_configured_broker_and_native(client, monkeypatch):
    monkeypatch.setattr(settings, "apple_client_ids", ["apple-id"])
    monkeypatch.setattr(settings, "google_client_ids", [])
    r = await client.get("/v1/auth/providers")
    assert r.status_code == 200
    slugs = {p["slug"]: p for p in r.json()["providers"]}
    assert slugs["apple"]["kind"] == "native"
    assert "google" not in slugs  # not configured
    assert slugs["github"]["kind"] == "broker"
    assert slugs["github"]["display_name"] == "GitHub"


async def test_providers_omits_unconfigured_broker(client, monkeypatch):
    monkeypatch.setattr(settings, "github_client_secret", "")  # half-config
    monkeypatch.setattr(settings, "apple_client_ids", [])
    monkeypatch.setattr(settings, "google_client_ids", [])
    r = await client.get("/v1/auth/providers")
    assert r.json()["providers"] == []


# --------------------------------------------------------------------------- #
# Kill-switch: broker endpoints honour settings.social_signin_enabled
# (cage-match #30 r2 — uniform with the native /social gate)
# --------------------------------------------------------------------------- #
async def test_start_disabled_social_signin_403(client, monkeypatch):
    monkeypatch.setattr(settings, "social_signin_enabled", False)
    r = await client.get("/v1/auth/oauth/github/start")
    assert r.status_code == 403


async def test_callback_disabled_social_signin_403(client, monkeypatch, session):
    """A disabled-flag callback is not a normal user path → a plain 403 (NOT a
    redirect)."""
    _mock_exchange(monkeypatch, identity=_IDENTITY)
    state = await _mint_state(session)
    monkeypatch.setattr(settings, "social_signin_enabled", False)
    r = await client.get(
        f"/v1/auth/oauth/github/callback?code=authcode&state={state}")
    assert r.status_code == 403


async def test_exchange_disabled_social_signin_403(client, monkeypatch, session):
    code = await handoff_service.create_handoff(session, {
        "kind": "provisioning", "provider": "github", "provider_sub": "gh-1",
        "suggested_name": None, "email": None})
    monkeypatch.setattr(settings, "social_signin_enabled", False)
    r = await client.post("/v1/auth/oauth/exchange", json={"code": code})
    assert r.status_code == 403


async def test_providers_disabled_social_signin_empty(client, monkeypatch):
    monkeypatch.setattr(settings, "apple_client_ids", ["apple-id"])
    monkeypatch.setattr(settings, "social_signin_enabled", False)
    r = await client.get("/v1/auth/providers")
    assert r.status_code == 200
    assert r.json()["providers"] == []


# --------------------------------------------------------------------------- #
# UNIT: exchange_code against a faked httpx layer (no network)
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, payload, status_code=200, raw=None, headers=None):
        self._payload = payload
        self.status_code = status_code
        self._raw = raw
        self.headers = headers or {}

    def json(self):
        if self._raw is not None:
            raise ValueError("not json")
        return self._payload


def _fake_client_factory(monkeypatch, *, token_resp, user_resp=None,
                         emails_resp=None, raise_on=None):
    """Install a fake httpx.AsyncClient that returns canned responses for the
    token POST and the GitHub profile GETs. raise_on={'token'|'user'} raises an
    httpx.HTTPError for that call (network failure)."""
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, headers=None):
            if raise_on == "token":
                raise httpx.ConnectError("token endpoint down")
            return token_resp

        async def get(self, url, headers=None):
            if raise_on == "user" and url.endswith("/user"):
                raise httpx.ConnectError("profile down")
            if url.endswith("/user/emails"):
                return emails_resp
            return user_resp

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def _gh_provider(monkeypatch):
    monkeypatch.setattr(settings, "github_client_id", "id")
    monkeypatch.setattr(settings, "github_client_secret", "secret")
    return oauth_broker.get_provider("github")


async def test_exchange_code_success_builds_identity(monkeypatch):
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch,
        token_resp=_FakeResp({"access_token": "gho_abc"}),
        user_resp=_FakeResp({"id": 42, "login": "octo", "name": "Octo Cat"}),
        emails_resp=_FakeResp([
            {"email": "secondary@x.com", "primary": False, "verified": True},
            {"email": "octo@example.com", "primary": True, "verified": True},
        ]),
    )
    identity = await oauth_broker.exchange_code(prov, code="abc")
    assert identity.provider == "github"
    assert identity.sub == "42"
    assert identity.suggested_name == "Octo Cat"
    assert identity.email == "octo@example.com"


async def test_exchange_code_no_verified_email_yields_none(monkeypatch):
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch,
        token_resp=_FakeResp({"access_token": "gho_abc"}),
        user_resp=_FakeResp({"id": 7, "login": "noemail", "name": None}),
        emails_resp=_FakeResp([
            {"email": "unverified@x.com", "primary": True, "verified": False},
        ]),
    )
    identity = await oauth_broker.exchange_code(prov, code="abc")
    assert identity.sub == "7"
    assert identity.suggested_name == "noemail"  # falls back to login
    assert identity.email is None


async def test_exchange_code_github_error_body_is_invalid(monkeypatch):
    """GitHub returns 200 with {"error": ...} on a bad/expired code -> 401-class."""
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch,
        token_resp=_FakeResp({"error": "bad_verification_code"}),
    )
    with pytest.raises(oauth_broker.BrokerInvalidExchange):
        await oauth_broker.exchange_code(prov, code="abc")


async def test_exchange_code_missing_access_token_is_invalid(monkeypatch):
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(monkeypatch, token_resp=_FakeResp({"scope": "read:user"}))
    with pytest.raises(oauth_broker.BrokerInvalidExchange):
        await oauth_broker.exchange_code(prov, code="abc")


async def test_exchange_code_token_4xx_is_invalid(monkeypatch):
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch, token_resp=_FakeResp({}, status_code=400))
    with pytest.raises(oauth_broker.BrokerInvalidExchange):
        await oauth_broker.exchange_code(prov, code="abc")


async def test_exchange_code_token_5xx_is_unavailable(monkeypatch):
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch, token_resp=_FakeResp({}, status_code=503))
    with pytest.raises(oauth_broker.BrokerUnavailable):
        await oauth_broker.exchange_code(prov, code="abc")


async def test_exchange_code_token_network_failure_is_unavailable(monkeypatch):
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch, token_resp=_FakeResp({}), raise_on="token")
    with pytest.raises(oauth_broker.BrokerUnavailable):
        await oauth_broker.exchange_code(prov, code="abc")


async def test_exchange_code_profile_network_failure_is_unavailable(monkeypatch):
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch,
        token_resp=_FakeResp({"access_token": "gho_abc"}),
        raise_on="user")
    with pytest.raises(oauth_broker.BrokerUnavailable):
        await oauth_broker.exchange_code(prov, code="abc")


# --- Finding 2: secret-bearing exception chain is BROKEN (`from None`) ------- #
async def test_token_network_failure_breaks_exception_chain(monkeypatch):
    """The token POST's httpx exception holds the request body (client_secret) on
    its `.request`. Raising `from None` must break the chain so __cause__ is None —
    otherwise the secret is reachable through the exception (cage-match #30,
    Finding 2)."""
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch, token_resp=_FakeResp({}), raise_on="token")
    with pytest.raises(oauth_broker.BrokerUnavailable) as ei:
        await oauth_broker.exchange_code(prov, code="abc")
    assert ei.value.__cause__ is None
    # And belt-and-braces: __context__ is suppressed too.
    assert ei.value.__suppress_context__ is True


async def test_profile_network_failure_breaks_exception_chain(monkeypatch):
    """The profile GET's httpx exception holds the Authorization header (the access
    token). `from None` must break the chain so __cause__ is None (Finding 2)."""
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch,
        token_resp=_FakeResp({"access_token": "gho_abc"}),
        raise_on="user")
    with pytest.raises(oauth_broker.BrokerUnavailable) as ei:
        await oauth_broker.exchange_code(prov, code="abc")
    assert ei.value.__cause__ is None
    assert ei.value.__suppress_context__ is True


# --- Finding 3: rate-limit (429 / 403+remaining:0) is an OUTAGE (503) -------- #
async def test_token_429_is_unavailable(monkeypatch):
    """A 429 from the token endpoint is rate-limiting -> BrokerUnavailable (503),
    NOT a bad credential (cage-match #30, Finding 3)."""
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch, token_resp=_FakeResp({}, status_code=429))
    with pytest.raises(oauth_broker.BrokerUnavailable):
        await oauth_broker.exchange_code(prov, code="abc")


async def test_token_403_ratelimited_is_unavailable(monkeypatch):
    """A 403 with X-RateLimit-Remaining: 0 from the token endpoint is rate-limiting
    -> BrokerUnavailable (Finding 3)."""
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch,
        token_resp=_FakeResp({}, status_code=403,
                             headers={"X-RateLimit-Remaining": "0"}))
    with pytest.raises(oauth_broker.BrokerUnavailable):
        await oauth_broker.exchange_code(prov, code="abc")


async def test_token_403_not_ratelimited_is_invalid(monkeypatch):
    """A PLAIN 403 (no X-RateLimit-Remaining: 0) from the token endpoint is a real
    authorization failure -> BrokerInvalidExchange (Finding 3 — only rate-limit
    403s become outages)."""
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch, token_resp=_FakeResp({}, status_code=403))
    with pytest.raises(oauth_broker.BrokerInvalidExchange):
        await oauth_broker.exchange_code(prov, code="abc")


async def test_profile_429_is_unavailable(monkeypatch):
    """A 429 from the profile endpoint is rate-limiting -> BrokerUnavailable
    (Finding 3)."""
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch,
        token_resp=_FakeResp({"access_token": "gho_abc"}),
        user_resp=_FakeResp({}, status_code=429))
    with pytest.raises(oauth_broker.BrokerUnavailable):
        await oauth_broker.exchange_code(prov, code="abc")


async def test_profile_403_ratelimited_is_unavailable(monkeypatch):
    """A 403 with X-RateLimit-Remaining: 0 from the profile endpoint is
    rate-limiting -> BrokerUnavailable (Finding 3)."""
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch,
        token_resp=_FakeResp({"access_token": "gho_abc"}),
        user_resp=_FakeResp({}, status_code=403,
                            headers={"X-RateLimit-Remaining": "0"}))
    with pytest.raises(oauth_broker.BrokerUnavailable):
        await oauth_broker.exchange_code(prov, code="abc")


async def test_profile_403_not_ratelimited_is_invalid(monkeypatch):
    """A PLAIN 403 from the profile endpoint stays a bad credential
    (BrokerInvalidExchange) — only rate-limit 403s become outages (Finding 3)."""
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch,
        token_resp=_FakeResp({"access_token": "gho_abc"}),
        user_resp=_FakeResp({}, status_code=403))
    with pytest.raises(oauth_broker.BrokerInvalidExchange):
        await oauth_broker.exchange_code(prov, code="abc")


async def test_exchange_code_profile_5xx_is_unavailable(monkeypatch):
    prov = _gh_provider(monkeypatch)
    _fake_client_factory(
        monkeypatch,
        token_resp=_FakeResp({"access_token": "gho_abc"}),
        user_resp=_FakeResp({}, status_code=502))
    with pytest.raises(oauth_broker.BrokerUnavailable):
        await oauth_broker.exchange_code(prov, code="abc")


async def test_secret_and_token_never_in_exception(monkeypatch):
    """SECRET HYGIENE: a failed exchange's exception must not contain the
    client_secret or any access token."""
    monkeypatch.setattr(settings, "github_client_id", "id")
    monkeypatch.setattr(settings, "github_client_secret", "SUPER-SECRET-VALUE")
    prov = oauth_broker.get_provider("github")
    _fake_client_factory(
        monkeypatch,
        token_resp=_FakeResp({"access_token": "gho_LEAK_ME"}, status_code=400))
    with pytest.raises(oauth_broker.BrokerError) as ei:
        await oauth_broker.exchange_code(prov, code="abc")
    assert "SUPER-SECRET-VALUE" not in str(ei.value)
    assert "gho_LEAK_ME" not in str(ei.value)


# --------------------------------------------------------------------------- #
# build_authorize_url — PKCE inclusion is provider-gated
# --------------------------------------------------------------------------- #
def test_build_authorize_url_omits_pkce_for_github(monkeypatch):
    prov = _gh_provider(monkeypatch)
    url = oauth_broker.build_authorize_url(
        prov, state="st", code_challenge="ignored-because-no-pkce")
    assert "code_challenge" not in url


def test_build_authorize_url_includes_pkce_when_supported(monkeypatch):
    """A PKCE-capable provider (synthetic) emits the challenge + S256 method."""
    import dataclasses
    prov = dataclasses.replace(_gh_provider(monkeypatch), supports_pkce=True)
    url = oauth_broker.build_authorize_url(
        prov, state="st", code_challenge="CHAL")
    q = httpx.QueryParams(url.split("?", 1)[1])
    assert q["code_challenge"] == "CHAL"
    assert q["code_challenge_method"] == "S256"


# --------------------------------------------------------------------------- #
# Finding 1 RED-PROOF (ii): PKCE code_verifier NEVER leaves the server.
# --------------------------------------------------------------------------- #
async def test_pkce_verifier_never_in_authorize_url_or_state(
        client, monkeypatch, session):
    """For a SYNTHETIC PKCE-enabled provider, /start must store the code_verifier
    in the SERVER-SIDE state row and put NEITHER the verifier NOR a base64-readable
    token into the authorize URL or the `state` param. Only the code_challenge
    crosses the wire (cage-match #30, Finding 1).

    Under the OLD signed-JWT state this FAILED: the verifier rode inside the
    base64-decodable state token. Now the verifier lives only in the DB."""
    import dataclasses

    pkce_prov = dataclasses.replace(
        oauth_broker._GITHUB, slug="pkcetest", supports_pkce=True)
    # Make slug 'pkcetest' resolve via the github creds so it is_configured().
    monkeypatch.setitem(oauth_broker._REGISTRY, "pkcetest", pkce_prov)
    monkeypatch.setattr(
        oauth_broker, "_provider_client_id", lambda s: "pkce-client-id")
    monkeypatch.setattr(
        oauth_broker, "_provider_client_secret", lambda s: "pkce-secret")

    r = await client.get("/v1/auth/oauth/pkcetest/start")
    assert r.status_code == 302
    loc = r.headers["location"]
    q = httpx.QueryParams(loc.split("?", 1)[1])

    # The challenge IS present (PKCE provider)...
    assert "code_challenge" in q
    nonce = q["state"]
    # ...but the verifier is stored server-side and is NOT the state, NOT in the
    # URL, and the state is NOT a decodable JWT carrying it.
    row = await session.get(OAuthState, nonce)
    assert row is not None and row.code_verifier  # stored server-side
    verifier = row.code_verifier
    assert verifier != nonce
    assert verifier not in loc            # verifier nowhere in the redirect URL
    assert verifier != q["code_challenge"]  # challenge is the S256 hash, not the verifier
    with pytest.raises(jwt.InvalidTokenError):
        # state is an opaque nonce, not a JWT we can crack open for the verifier.
        jwt.decode(nonce, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
