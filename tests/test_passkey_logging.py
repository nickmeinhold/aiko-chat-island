"""Passkey ceremony observability (#1471 follow-up).

The endpoints emit structured trace lines so the gate-3 device test (a real iPhone
driving the ceremony) is debuggable from `docker logs -f`. These tests prove the
trace fires on a success and a reject, and that it logs only a SAFE PREFIX of the
challenge state — never the full value.
"""
from __future__ import annotations

import logging

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest.deps import get_session

AUTH_LOGGER = "aiko_gateway.rest.auth"


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


@pytest.mark.asyncio
async def test_register_start_logs_challenge_issued(client, caplog):
    with caplog.at_level(logging.INFO, logger=AUTH_LOGGER):
        r = await client.post("/v1/auth/passkey/register/start")
    assert r.status_code == 200
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("passkey.register.start: challenge issued" in m for m in msgs), msgs


@pytest.mark.asyncio
async def test_register_finish_bad_challenge_logs_reject(client, caplog):
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        r = await client.post(
            "/v1/auth/passkey/register/finish",
            json={"state": "definitely-not-a-real-challenge-handle", "credential": {}},
        )
    assert r.status_code == 400
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("passkey.register.finish: REJECT invalid/expired challenge" in m for m in msgs), msgs


@pytest.mark.asyncio
async def test_trace_logs_only_a_safe_prefix_not_the_full_state(client, caplog):
    """A log-safe prefix only: the full challenge handle must never appear in logs."""
    full_state = "abcdefghijklmnopqrstuvwxyz0123456789-secret-handle"
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        await client.post(
            "/v1/auth/passkey/register/finish",
            json={"state": full_state, "credential": {}},
        )
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "passkey.register.finish: REJECT" in joined
    assert full_state not in joined  # only the truncated prefix is logged
    assert full_state[:10] in joined  # ...and the prefix IS present (traceable)
