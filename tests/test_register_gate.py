"""`/v1/auth/register` is gated by settings.open_registration (task #38).

In dev it's open (frictionless testing); in prod it's closed by default so a
self-registered account can't appear and read everything (compounding the
deferred I2 membership gap, #36). The endpoint reads the flag at request time.

As with test_rest_auth, the app is built from just the auth router — never
`aiko_gateway.main` — to keep the suite free of the undeclared `aiko_services`
dependency.
"""
from __future__ import annotations

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiko_gateway.config import settings
from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest.deps import get_session


@pytest_asyncio.fixture
async def client(session):
    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


_REG_BODY = {"username": "alice", "display_name": "Alice", "password": "pw"}


async def test_register_open_allows_signup(client, monkeypatch):
    monkeypatch.setattr(settings, "open_registration", True)
    resp = await client.post("/v1/auth/register", json=_REG_BODY)
    assert resp.status_code == 200
    assert "access_token" in resp.json()


async def test_register_closed_rejects_signup(client, monkeypatch):
    monkeypatch.setattr(settings, "open_registration", False)
    resp = await client.post("/v1/auth/register", json=_REG_BODY)
    assert resp.status_code == 403
