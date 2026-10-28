"""`/v1/auth/register` (the PASSWORD door) is gated by settings.open_registration
(task #38), and production does not serve it at all.

In dev it follows the flag (frictionless testing). Production used to refuse to
boot with the flag open ("until I2 membership", which landed 2026-06-24); when
that refusal was retired on 2026-10-28 so passkey sign-up could open, this door
stayed out of production (Nick's call): it accepts any username and any
password, and no client uses it. It lives on `dev_router`, which
`include_auth_routers` mounts only outside production.

As with test_rest_auth, the app is built from just the auth routers — never
`aiko_gateway.main` — to keep the suite free of the undeclared `aiko_services`
dependency.
"""
from __future__ import annotations

import pytest
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
    app.include_router(auth_routes.dev_router)  # the dev door under test
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


@pytest.mark.parametrize("environment, expected", [("test", 200), ("production", 404)])
async def test_production_does_not_serve_register(
        session, monkeypatch, environment, expected):
    """The flag is OPEN in both cells and only the environment differs, and the
    surface is built by `include_auth_routers`, the one function main.py mounts
    auth through. Production must not SERVE the route (404), not merely refuse it:
    a refusal inside the predicate the passkey door shares is one "make the flag
    mean the same on every door" cleanup from letting anyone claim `nick`
    (cage-match PR#433, Carnot + Tesla). The dev cell is the control: without it,
    a function that never mounted the door would pass."""
    async def _override_session():
        yield session

    monkeypatch.setattr(settings, "open_registration", True)
    monkeypatch.setattr(settings, "environment", environment)
    app = FastAPI()
    auth_routes.include_auth_routers(app)
    app.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/v1/auth/register", json=_REG_BODY)
    assert resp.status_code == expected, resp.text


async def test_a_dev_register_of_an_auto_shaped_username_is_not_auto(client, monkeypatch):
    """The dev password door is the other place a person chooses a handle at creation."""
    from aiko_gateway.domain import users_service

    monkeypatch.setattr(settings, "open_registration", True)
    resp = await client.post("/v1/auth/register", json={
        **_REG_BODY, "username": users_service._new_auto_handle()})
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["handle_auto"] is False
