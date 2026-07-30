"""The OIDC agent-ingress end-to-end (Citizenship for the Dreaming, H1 — #2403).

Layers, mirroring test_moderation_actions:
  * service tests call agents_service directly (binding create, kind='agent', the
    OIDC door's bound / unbound / wrong-aud / banned outcomes).
  * HTTP tests drive the real contract: admin-gated binding create, the public token
    exchange, and that neither door re-opens registration or lets a human self-promote.
  * the SEND + REVOKE tests prove an agent posts to a PRIVATE channel via the WS send
    frame, and that banning a live-connected agent drops its socket.

App-under-test is built from the auth + agents (+ moderation for ban) routers, never
`main`, keeping the "never import aiko_services" isolation invariant.
"""
from __future__ import annotations

import datetime as dt
import time
from types import SimpleNamespace

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from aiko_gateway.config import settings
from aiko_gateway.domain import agents_service, github_oidc, security, users_service
from aiko_gateway.domain.models import Channel, Kind, Membership, Message, User
from aiko_gateway.realtime.hub import Connection, Hub
from aiko_gateway.realtime.ws import _handle_send
from aiko_gateway.rest import agents as agent_routes
from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest import moderation as moderation_routes
from aiko_gateway.rest.deps import get_session
from aiko_gateway.rest.errors import register_error_handlers

# --- GitHub OIDC test-token machinery (mirrors test_agent_oidc) --------------

_KID = "gh-test-key-1"
_ISS = "https://token.actions.githubusercontent.com"
_AUD = "aiko-island"
_REPO = "nickmeinhold/aiko-chat-app"
_REPO_ID = "638902173"
_REPO_OWNER_ID = "9919"
_REF = "refs/heads/main"
_WORKFLOW_REF = "nickmeinhold/aiko-chat-app/.github/workflows/agent.yml@refs/heads/main"


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def wired(rsa_key, monkeypatch):
    cache = github_oidc._jwks
    monkeypatch.setattr(cache, "_keys", {_KID: rsa_key.public_key()})
    monkeypatch.setattr(cache, "_fetched_at", time.monotonic())
    return rsa_key


def _now() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def _oidc(rsa_key, **overrides) -> str:
    claims = {
        "iss": _ISS, "aud": _AUD, "sub": f"repo:{_REPO}:ref:{_REF}",
        "repository": _REPO, "repository_id": _REPO_ID,
        "repository_owner_id": _REPO_OWNER_ID, "ref": _REF,
        "workflow_ref": _WORKFLOW_REF, "iat": _now(), "exp": _now() + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": _KID})


# --- shared helpers ----------------------------------------------------------

async def _user(session, username: str, *, banned: bool = False) -> User:
    u = await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")
    if banned:
        u.banned_at = dt.datetime.now(dt.timezone.utc)
        await session.commit()
    return u


async def _bind(session, *, aud=_AUD, repository=_REPO, repository_id=_REPO_ID,
                repository_owner_id=_REPO_OWNER_ID, ref=_REF,
                workflow_ref=_WORKFLOW_REF) -> tuple[User, object]:
    return await agents_service.create_agent_binding(
        session, repository=repository, repository_id=repository_id,
        repository_owner_id=repository_owner_id, ref=ref,
        workflow_ref=workflow_ref, aud=aud)


def _auth(user: User) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


@pytest_asyncio.fixture
async def client(session):
    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(agent_routes.router)
    app.include_router(moderation_routes.router)
    register_error_handlers(app)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ============================================================================
# service: binding create + kind
# ============================================================================

async def test_create_binding_mints_agent_identity(session):
    user, binding = await _bind(session)
    assert user.kind == Kind.AGENT == "agent"
    assert user.password_hash is None
    assert binding.repository == _REPO and binding.aud == _AUD
    # the binding resolves back on the exact triple
    got = await agents_service.get_binding(
        session, repository=_REPO, ref=_REF, workflow_ref=_WORKFLOW_REF)
    assert got is not None and got.user_id == user.id


async def test_duplicate_binding_conflicts(session):
    await _bind(session)
    with pytest.raises(agents_service.BindingConflict):
        await _bind(session)


@pytest.mark.parametrize("bad", [
    {"repository": " "}, {"repository_id": ""}, {"repository_owner_id": "  "},
    {"ref": ""}, {"workflow_ref": "  "}, {"aud": ""},
])
async def test_blank_binding_field_rejected(session, bad):
    kwargs = dict(repository=_REPO, repository_id=_REPO_ID,
                  repository_owner_id=_REPO_OWNER_ID, ref=_REF,
                  workflow_ref=_WORKFLOW_REF, aud=_AUD)
    kwargs.update(bad)
    with pytest.raises(agents_service.InvalidBinding):
        await agents_service.create_agent_binding(session, **kwargs)


# ============================================================================
# service: the OIDC door
# ============================================================================

async def test_bound_token_resolves_agent(session, wired):
    agent, _ = await _bind(session)
    resolved = await agents_service.authenticate_agent_oidc(session, _oidc(wired))
    assert resolved.id == agent.id and resolved.kind == "agent"


async def test_unbound_repo_rejected(session, wired):
    # No binding created at all → a signature-valid token has no home.
    with pytest.raises(agents_service.AgentNotBound):
        await agents_service.authenticate_agent_oidc(session, _oidc(wired))


async def test_wrong_ref_rejected(session, wired):
    await _bind(session, ref="refs/heads/main")
    tok = _oidc(wired, ref="refs/heads/attacker", sub=f"repo:{_REPO}:ref:refs/heads/attacker")
    with pytest.raises(agents_service.AgentNotBound):
        await agents_service.authenticate_agent_oidc(session, tok)


async def test_wrong_aud_rejected(session, wired):
    """A token for a BOUND repo/ref/workflow but the WRONG audience (e.g. the default
    repo-owner aud, not the operator's declared one) is rejected — same opaque
    AgentNotBound as an absent binding."""
    await _bind(session, aud="aiko-island")
    tok = _oidc(wired, aud="https://github.com/nickmeinhold")  # GitHub default-ish aud
    with pytest.raises(agents_service.AgentNotBound):
        await agents_service.authenticate_agent_oidc(session, tok)


async def test_aud_as_list_matches(session, wired):
    """The JWT `aud` may be an array; the binding is satisfied if its aud appears."""
    await _bind(session, aud="aiko-island")
    tok = _oidc(wired, aud=["other", "aiko-island"])
    resolved = await agents_service.authenticate_agent_oidc(session, tok)
    assert resolved.kind == "agent"


async def test_wrong_repository_id_rejected(session, wired):
    """Finding 1: a token carrying the bound repo NAME but a DIFFERENT repository_id
    (a recycled/transferred slug pointing at a different underlying repo) is REJECTED.
    The immutable numeric id — not the mutable name — is the authorization root."""
    await _bind(session, repository_id="638902173")
    tok = _oidc(wired, repository_id="999999999")  # same NAME, different immutable id
    with pytest.raises(agents_service.AgentNotBound):
        await agents_service.authenticate_agent_oidc(session, tok)


async def test_wrong_repository_owner_id_rejected(session, wired):
    """Finding 1: same repo name + repo id story for the OWNER id — a transfer that
    keeps the slug but changes the owner (fresh owner id) is rejected."""
    await _bind(session, repository_owner_id="9919")
    tok = _oidc(wired, repository_owner_id="424242")
    with pytest.raises(agents_service.AgentNotBound):
        await agents_service.authenticate_agent_oidc(session, tok)


async def test_binding_to_non_agent_user_rejected(session, wired):
    """Finding 3: defense-in-depth — if a binding's user_id points at a NON-agent row
    (FK/data drift), the door fails closed rather than minting an aiko token for a
    human identity. We simulate drift by re-pointing the binding at a human user."""
    agent, binding = await _bind(session)
    human = await _user(session, "a-human")
    binding.user_id = human.id
    await session.commit()
    with pytest.raises(agents_service.AgentBindingDangling):
        await agents_service.authenticate_agent_oidc(session, _oidc(wired))


async def test_banned_agent_denied_at_service_layer(session, wired):
    """Finding 4: the ban gate lives INSIDE the auth mutator, so a banned agent is
    denied at the service layer (AgentBanned) — not only at the REST ban check. A
    future internal caller of authenticate_agent_oidc can't skip it."""
    agent, _ = await _bind(session)
    agent.banned_at = dt.datetime.now(dt.timezone.utc)
    await session.commit()
    with pytest.raises(agents_service.AgentBanned):
        await agents_service.authenticate_agent_oidc(session, _oidc(wired))


# ============================================================================
# HTTP: admin-gated binding + public token exchange
# ============================================================================

def _body(**overrides) -> dict:
    b = {"repository": _REPO, "repository_id": _REPO_ID,
         "repository_owner_id": _REPO_OWNER_ID, "ref": _REF,
         "workflow_ref": _WORKFLOW_REF, "aud": _AUD}
    b.update(overrides)
    return b


async def test_binding_endpoint_admin_gated(client, session, monkeypatch):
    plain = await _user(session, "plain")
    monkeypatch.setattr(settings, "agent_binding_admin_ids", [])  # nobody is admin
    body = _body()
    # unauthenticated → 401/403; authed non-admin → 403
    assert (await client.post("/v1/agents/bindings", json=body)).status_code in (401, 403)
    r = await client.post("/v1/agents/bindings", json=body, headers=_auth(plain))
    assert r.status_code == 403


async def test_binding_endpoint_not_reused_moderator_role(client, session, monkeypatch):
    """Finding 6: minting a production agent identity does NOT reuse the content-
    moderator role. A configured MODERATOR who is NOT in AGENT_BINDING_ADMINS is
    forbidden; only the dedicated allowlist opens the door."""
    mod = await _user(session, "mod-only")
    monkeypatch.setattr(settings, "moderator_user_ids", [mod.id])  # a real moderator
    monkeypatch.setattr(settings, "agent_binding_admin_ids", [])   # but not a binding admin
    r = await client.post("/v1/agents/bindings", json=_body(), headers=_auth(mod))
    assert r.status_code == 403


async def test_admin_creates_binding_then_agent_mints_token(client, session, monkeypatch, wired):
    mod = await _user(session, "mod")
    monkeypatch.setattr(settings, "agent_binding_admin_ids", [mod.id])
    body = _body()

    r = await client.post("/v1/agents/bindings", json=body, headers=_auth(mod))
    assert r.status_code == 201, r.text
    agent_user_id = r.json()["user_id"]
    assert r.json()["kind"] == "agent"

    # The agent presents ONLY a GitHub OIDC token — no prior aiko session — and gets a
    # short-lived access token it can use as a bearer credential.
    tr = await client.post("/v1/agents/token", json={"oidc_token": _oidc(wired)})
    assert tr.status_code == 200, tr.text
    access = tr.json()["access_token"]
    assert tr.json()["expires_in"] == settings.jwt_access_ttl_seconds
    assert "refresh_token" not in tr.json()  # short-lived access only, no refresh
    sub, _gen = security.decode_token(access, expected_type="access")
    assert sub == agent_user_id


async def test_duplicate_binding_endpoint_returns_409(client, session, monkeypatch):
    """Finding 5: the duplicate is caught by an explicit get_binding pre-check that
    returns a clean 409 BEFORE the insert — engine-portable, not dependent on parsing a
    raw SQLite error string (which would 500 on Postgres)."""
    mod = await _user(session, "mod2")
    monkeypatch.setattr(settings, "agent_binding_admin_ids", [mod.id])
    first = await client.post("/v1/agents/bindings", json=_body(), headers=_auth(mod))
    assert first.status_code == 201, first.text
    dup = await client.post("/v1/agents/bindings", json=_body(), headers=_auth(mod))
    assert dup.status_code == 409, dup.text


async def test_token_endpoint_malformed_aud_is_401_not_500(client, session, wired):
    """Finding 2: a SIGNED token whose `aud` is a non-str/non-list value fails closed
    as a 401 at the door (the verify boundary's type-guard), never a 500 from
    `list(token_aud)` in the audience match."""
    await _bind(session)
    tok = _oidc(wired, aud={"weird": "object"})
    r = await client.post("/v1/agents/token", json={"oidc_token": tok})
    assert r.status_code == 401, r.text


async def test_token_endpoint_wrong_repo_id_is_401(client, session, monkeypatch, wired):
    """Finding 1 at the HTTP door: same repo NAME, different repository_id → opaque 401
    (indistinguishable from an unbound token)."""
    mod = await _user(session, "mod3")
    monkeypatch.setattr(settings, "agent_binding_admin_ids", [mod.id])
    r = await client.post("/v1/agents/bindings", json=_body(), headers=_auth(mod))
    assert r.status_code == 201, r.text
    tok = _oidc(wired, repository_id="111111111")
    tr = await client.post("/v1/agents/token", json={"oidc_token": tok})
    assert tr.status_code == 401, tr.text


async def test_token_endpoint_unbound_is_401_and_creates_no_user(client, session, wired):
    before = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    r = await client.post("/v1/agents/token", json={"oidc_token": _oidc(wired)})
    assert r.status_code == 401
    after = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    assert after == before  # the OIDC door NEVER onboards a user (not a register bypass)


async def test_token_endpoint_bad_signature_is_401(client, session, wired):
    await _bind(session)
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = _oidc(attacker)  # header kid=_KID but signed by the wrong key
    r = await client.post("/v1/agents/token", json={"oidc_token": forged})
    assert r.status_code == 401


async def test_banned_agent_denied_token(client, session, wired):
    agent, _ = await _bind(session)
    agent.banned_at = dt.datetime.now(dt.timezone.utc)
    await session.commit()
    r = await client.post("/v1/agents/token", json={"oidc_token": _oidc(wired)})
    assert r.status_code == 403
    assert r.json()["error"] == "account_suspended"


# ============================================================================
# a human cannot self-assign kind='agent'; registration stays closed
# ============================================================================

async def test_human_ingresses_produce_kind_human(session):
    """Every human ingress creates kind='human'; there is no request field that sets
    kind. 'agent' is reachable ONLY through the admin-gated binding door."""
    pw = await users_service.create_user(
        session, username="pwuser", display_name="Pw", password="pw")
    soc = await users_service.create_social_user(
        session, provider="google", provider_sub="sub-1", handle="socuser",
        display_name="Soc")
    assert pw.kind == "human" and soc.kind == "human"
    # The binding door is the sole agent minter.
    agent, _ = await _bind(session)
    assert agent.kind == "agent"


async def test_register_still_closed_when_disabled(client, session, monkeypatch):
    """The agent door does not touch open_registration — register stays gated."""
    monkeypatch.setattr(settings, "open_registration", False)
    r = await client.post(
        "/v1/auth/register",
        json={"username": "x", "display_name": "X", "password": "pw"})
    assert r.status_code == 403


# ============================================================================
# an agent posts to a PRIVATE channel via the WS send frame
# ============================================================================

class _FakeBus:
    def __init__(self):
        self.sent: list = []

    def send(self, username, channel, text) -> bool:
        self.sent.append((username, channel, text))
        return True


class _RecordingHub:
    def __init__(self):
        self.fanned: list = []

    async def fanout(self, channel_id, frame, *, exclude_user_ids=None) -> None:
        self.fanned.append((channel_id, frame, exclude_user_ids))


class _FakeGw:
    def __init__(self):
        self.bus = _FakeBus()
        self.hub = _RecordingHub()


class _RecordingConn:
    def __init__(self, user_id):
        self.ws = None
        self.user_id = user_id
        self.subscribed: set[str] = set()
        self.sent: list[dict] = []

    async def send(self, frame):
        self.sent.append(frame)


class _StubSessionLocal:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def test_agent_posts_to_private_channel_via_ws_send(session, monkeypatch):
    import aiko_gateway.realtime.ws as ws_mod
    monkeypatch.setattr(ws_mod, "SessionLocal", lambda: _StubSessionLocal(session))

    agent, _ = await _bind(session)
    # A private channel with the agent as a posting member.
    ch = Channel(id="0" * 26, name="ops", kind="standard", aiko_channel="ops",
                 is_private=True)
    session.add(ch)
    session.add(Membership(channel_id=ch.id, user_id=agent.id, role="member",
                           can_post=True))
    await session.commit()

    gw = _FakeGw()
    conn = _RecordingConn(agent.id)
    await _handle_send(gw, conn, agent, {
        "type": "send", "channel_id": ch.id, "body": "agent reporting in",
        "client_msg_id": "a1",
    })
    # Acked, persisted attributed to the agent, fanned out + put on the bus.
    assert [f["type"] for f in conn.sent] == ["ack"]
    assert len(gw.hub.fanned) == 1 and len(gw.bus.sent) == 1
    row = (await session.execute(
        select(Message).where(Message.client_msg_id == "a1"))).scalar_one()
    assert row.sender_user_id == agent.id and row.body == "agent reporting in"


async def test_agent_without_membership_cannot_post_to_private_channel(session, monkeypatch):
    """The private-channel ACL applies to agents identically: no membership → the send
    is refused (existence-hiding no_channel), nothing persisted."""
    import aiko_gateway.realtime.ws as ws_mod
    monkeypatch.setattr(ws_mod, "SessionLocal", lambda: _StubSessionLocal(session))
    agent, _ = await _bind(session)
    ch = Channel(id="0" * 26, name="secret", kind="standard", aiko_channel="secret",
                 is_private=True)
    session.add(ch)
    await session.commit()
    gw = _FakeGw()
    conn = _RecordingConn(agent.id)
    await _handle_send(gw, conn, agent, {
        "type": "send", "channel_id": ch.id, "body": "let me in",
        "client_msg_id": "a1",
    })
    errors = [f for f in conn.sent if f["type"] == "error"]
    assert errors and errors[0]["code"] == "no_channel"
    assert gw.hub.fanned == [] and gw.bus.sent == []


# ============================================================================
# revoking a live-connected agent drops its socket
# ============================================================================

class _LiveWS:
    def __init__(self):
        self.closed_code = None

    async def close(self, code=1000):
        self.closed_code = code


async def test_banning_live_agent_drops_its_socket(session):
    """Revocation closes the live socket: a banned agent's open WS connection is
    actively dropped (hub.disconnect_user), not merely blocked at next handshake —
    the SAME mechanism a human ban uses (moderation_service.ban_user returns the row
    so the caller drops the socket)."""
    from aiko_gateway.domain import moderation_service

    mod = await _user(session, "mod")
    agent, _ = await _bind(session)

    hub = Hub()
    live = _LiveWS()
    hub.register(Connection(live, agent.id))
    assert live.closed_code is None  # connected, socket open

    # Revoke: ban sets banned_at, then the ban ingress drops the live socket.
    await moderation_service.ban_user(
        session, target_id=agent.id, moderator_id=mod.id)
    dropped = await hub.disconnect_user(agent.id)

    assert dropped == 1
    assert live.closed_code == 1008  # policy violation — the agent's socket is severed
    # And the ban is durable: the agent row is now suspended.
    refreshed = await users_service.get_by_id(session, agent.id)
    assert users_service.is_banned(refreshed)
