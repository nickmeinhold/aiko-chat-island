"""I1 enforcement: REST read endpoints require a valid access token.

These are the HTTP-layer acceptance tests for `/v1/channels` and
`/v1/channels/{id}/messages` — the seam where the auth dependency lives. Before
this, those endpoints were inline `@app.get` with NO auth dependency: any
unauthenticated caller could list channels and read history (I1 violation, plan
§A3). The endpoints now route through `CurrentUser`, so an unauthenticated
request is rejected before any row is read.

The app under test is assembled from JUST the two read routers — NOT
`aiko_gateway.main`. Importing `main` would transitively load the aiko bus
client (`aiko.client` → `import aiko_services`), an undeclared, locally-editable
dependency absent on clean CI. The whole suite's isolation invariant is "never
import aiko_services"; the router chain (rest/* + domain/* + db) is clean, so we
build a minimal FastAPI app from the routers and keep that invariant.

The app is driven via httpx's ASGITransport (no lifespan → no aiko bus). The DB
`get_session` dependency is overridden to the in-memory test session so the
endpoints (and the user-loading auth dependency) share one DB.
"""
from __future__ import annotations

import datetime as dt

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiko_gateway.domain import security, users_service
from aiko_gateway.domain.models import Channel, Message
from aiko_gateway.rest import channels as channel_routes
from aiko_gateway.rest import messages as message_routes
from aiko_gateway.rest.deps import get_session


def _build_app() -> FastAPI:
    """A minimal app with only the read routers — no `main`, no aiko bus."""
    app = FastAPI()
    app.include_router(channel_routes.router)
    app.include_router(message_routes.router)
    return app


def _ulid(n: int) -> str:
    return f"{n:026d}"


async def _seed_channel_with_history(session, *, count: int = 3) -> str:
    channel = Channel(id=_ulid(0), name="general", kind="standard", aiko_channel="general")
    session.add(channel)
    now = dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc)
    for i in range(1, count + 1):
        session.add(Message(
            id=_ulid(i), channel_id=channel.id, sender_kind="human",
            body=f"msg {i}", created_at=now + dt.timedelta(seconds=i),
        ))
    await session.commit()
    return channel.id


@pytest_asyncio.fixture
async def client(session):
    """An httpx client bound to the app, with the DB session overridden to the
    in-memory test session. Lifespan is NOT run, so the aiko bus never starts."""
    async def _override_session():
        yield session

    app = _build_app()
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _auth_header(session) -> dict:
    """Create a real user and mint a real access token for it."""
    user = await users_service.create_user(
        session, username="alice", display_name="Alice", password="pw")
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


# --- I1: unauthenticated reads are rejected ---------------------------------

async def test_list_channels_requires_auth(client, session):
    await _seed_channel_with_history(session)
    resp = await client.get("/v1/channels")
    # Missing credentials → 401 (verified against the runtime), never 200.
    assert resp.status_code == 401


async def test_history_requires_auth(client, session):
    cid = await _seed_channel_with_history(session)
    resp = await client.get(f"/v1/channels/{cid}/messages")
    assert resp.status_code == 401


async def test_invalid_token_is_rejected_on_channels(client, session):
    await _seed_channel_with_history(session)
    resp = await client.get(
        "/v1/channels", headers={"Authorization": "Bearer not-a-real-jwt"})
    # Present-but-invalid token → 401 (get_current_user).
    assert resp.status_code == 401


async def test_invalid_token_is_rejected_on_history(client, session):
    cid = await _seed_channel_with_history(session)
    resp = await client.get(
        f"/v1/channels/{cid}/messages",
        headers={"Authorization": "Bearer not-a-real-jwt"})
    # Both read endpoints reject a present-but-invalid token symmetrically.
    assert resp.status_code == 401


# --- I1: authenticated reads succeed ----------------------------------------

async def test_list_channels_with_valid_token(client, session):
    await _seed_channel_with_history(session)
    headers = await _auth_header(session)
    resp = await client.get("/v1/channels", headers=headers)
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()["channels"]]
    assert "general" in names


async def test_history_with_valid_token(client, session):
    cid = await _seed_channel_with_history(session, count=3)
    headers = await _auth_header(session)
    resp = await client.get(f"/v1/channels/{cid}/messages", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert [m["msg_id"] for m in body["messages"]] == [_ulid(1), _ulid(2), _ulid(3)]
    assert body["next_after"] == _ulid(3)


async def test_history_unknown_channel_404_when_authed(client, session):
    headers = await _auth_header(session)
    resp = await client.get("/v1/channels/nonexistent/messages", headers=headers)
    # Auth passes, then the channel lookup fails → 404 (not an auth error).
    assert resp.status_code == 404
