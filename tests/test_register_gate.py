"""`/v1/auth/register` (the PASSWORD door) is gated by settings.open_registration
(task #38), and in production it is closed whatever the flag says.

In dev it follows the flag (frictionless testing). Production used to refuse to
boot with the flag open ("until I2 membership", which landed 2026-06-24); when
that refusal was retired on 2026-10-28 so passkey sign-up could open, this door
stayed shut in production (Nick's call): it accepts any username and any
password, and no client uses it. The endpoint reads both at request time.

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


async def test_register_is_closed_in_production_even_with_the_flag_open(
        client, monkeypatch):
    """The pair to `test_register_open_allows_signup`: the same open flag, and only
    the environment differs. If this goes green-to-200, opening passkey sign-up on
    a production island has reopened a door that lets anyone claim `nick`."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "open_registration", True)
    resp = await client.post("/v1/auth/register", json=_REG_BODY)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "registration is closed"
