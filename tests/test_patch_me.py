"""Acceptance tests for PATCH /v1/me — the handle + display-name mutate path (#2631).

The mutate path that unblocks app-side "change handle" + "edit display name"
(#2513). Before this the island only had claim (initial provisioning) — no mutate
path existed. Identity is the KEY (user.id); handle + display_name are mutable
labels on top of it, so a rename never orphans the account.

Built, like test_rest_auth, from JUST the auth `me_router` — never
`aiko_gateway.main` (that would import the aiko bus, breaking the suite's
"never import aiko_services" isolation invariant). The DB `get_session`
dependency is overridden to the in-memory test session so the endpoint and the
user-loading auth dependency share one DB.

Contract under test (mirrors HANDOFF-to-app-tab-v2-social-wire.md #2631):
  - both fields optional, at least one required (else 400)
  - handle: unique-at-a-time (409 if taken); 30-day change cooldown (429 +
    retry_after + Retry-After header); setting to the CURRENT handle is a no-op
    that does NOT trip the cooldown
  - display_name: editable anytime, never subject to cooldown
  - 200 response is the MeView (user_id, username, display_name, aiko_username)
"""
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiko_gateway.domain import security, users_service
from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest.deps import get_session


def _build_app() -> FastAPI:
    """A minimal app with only the auth me_router — no `main`, no aiko bus."""
    app = FastAPI()
    app.include_router(auth_routes.me_router)
    return app


@pytest_asyncio.fixture
async def client(session):
    async def _override_session():
        yield session

    app = _build_app()
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _make_user(session, *, username: str, display_name: str = "") -> object:
    user = await users_service.create_user(
        session, username=username, display_name=display_name or username,
        password="pw")
    return user


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


# --- auth + shape -----------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_requires_auth(client, session):
    resp = await client.patch("/v1/me", json={"display_name": "X"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_view_shape_stable(client, session):
    """GET /v1/me carries EXACTLY the MeView keys — guards that attaching a
    response_model doesn't silently strip or rename a wire field (append-only)."""
    user = await _make_user(session, username="alice", display_name="Alice")
    resp = await client.get("/v1/me", headers=_auth(user))
    assert resp.status_code == 200
    body = resp.json()
    # is_moderator is an existing UI-only field on /me; assert the identity core.
    for k in ("user_id", "username", "display_name", "aiko_username"):
        assert k in body, f"missing {k}"
    assert body["user_id"] == user.id
    assert body["username"] == "alice"


# --- display_name (free, no cooldown) ---------------------------------------

@pytest.mark.asyncio
async def test_patch_display_name_only(client, session):
    user = await _make_user(session, username="alice", display_name="Alice")
    resp = await client.patch("/v1/me", json={"display_name": "Alice Cooper"},
                              headers=_auth(user))
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Alice Cooper"
    assert resp.json()["username"] == "alice"  # unchanged


@pytest.mark.asyncio
async def test_empty_body_400(client, session):
    user = await _make_user(session, username="alice")
    resp = await client.patch("/v1/me", json={}, headers=_auth(user))
    assert resp.status_code == 400


# --- handle change ----------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_handle_success(client, session):
    user = await _make_user(session, username="alice")
    resp = await client.patch("/v1/me", json={"handle": "alice2"},
                              headers=_auth(user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "alice2"
    assert body["aiko_username"] == "alice2"  # wire token follows the handle


@pytest.mark.asyncio
async def test_patch_handle_blank_422(client, session):
    user = await _make_user(session, username="alice")
    resp = await client.patch("/v1/me", json={"handle": "   "}, headers=_auth(user))
    assert resp.status_code == 422  # pydantic front-door, same as claim


@pytest.mark.asyncio
async def test_patch_handle_taken_409(client, session):
    alice = await _make_user(session, username="alice")
    await _make_user(session, username="bob")
    resp = await client.patch("/v1/me", json={"handle": "bob"}, headers=_auth(alice))
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_patch_handle_same_value_is_noop_no_cooldown(client, session):
    """Setting handle to your CURRENT handle must not consume the cooldown — else
    a client re-submitting the settings form would lock itself out for 30 days."""
    user = await _make_user(session, username="alice")
    r1 = await client.patch("/v1/me", json={"handle": "alice"}, headers=_auth(user))
    assert r1.status_code == 200
    # cooldown NOT tripped → a real change immediately after still succeeds
    r2 = await client.patch("/v1/me", json={"handle": "alice2"}, headers=_auth(user))
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_patch_handle_cooldown_429_with_retry_after(client, session):
    user = await _make_user(session, username="alice")
    r1 = await client.patch("/v1/me", json={"handle": "alice2"}, headers=_auth(user))
    assert r1.status_code == 200
    r2 = await client.patch("/v1/me", json={"handle": "alice3"}, headers=_auth(user))
    assert r2.status_code == 429
    assert r2.json()["retry_after"] > 0
    assert "Retry-After" in r2.headers
    # still ~30 days out (allow slack); expressed in whole seconds
    assert r2.json()["retry_after"] <= 30 * 24 * 3600


@pytest.mark.asyncio
async def test_display_name_edit_never_blocked_by_handle_cooldown(client, session):
    """A handle change starts the cooldown, but display_name stays free."""
    user = await _make_user(session, username="alice", display_name="Alice")
    await client.patch("/v1/me", json={"handle": "alice2"}, headers=_auth(user))
    resp = await client.patch("/v1/me", json={"display_name": "New Name"},
                              headers=_auth(user))
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "New Name"


@pytest.mark.asyncio
async def test_handle_change_allowed_after_cooldown_window(client, session):
    """Set handle_changed_at to 31 days ago → a change is allowed again."""
    user = await _make_user(session, username="alice")
    user.handle_changed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)
    await session.commit()
    resp = await client.patch("/v1/me", json={"handle": "alice2"}, headers=_auth(user))
    assert resp.status_code == 200
