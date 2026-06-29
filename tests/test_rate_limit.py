"""Rate limiting on the public auth endpoints (#28).

Unit-tests the fixed-window counter + the client-IP extraction (the security
hinge), then integration-tests that a real endpoint returns 429 past the limit,
that the limit is per-IP (X-Forwarded-For), and that the flag disables it.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from aiko_gateway.config import settings
from aiko_gateway.domain.rate_limit import RateLimiter, client_ip
from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest.deps import get_session


# ---- unit: RateLimiter fixed window --------------------------------------- #

def test_limiter_allows_up_to_limit_then_rejects():
    rl = RateLimiter()
    # limit=3, huge window so it never resets mid-test
    assert [rl.hit("b", "ip", 3, 1000)[0] for _ in range(3)] == [True, True, True]
    allowed, retry = rl.hit("b", "ip", 3, 1000)
    assert allowed is False
    assert retry >= 1  # a positive Retry-After


def test_limiter_window_resets_after_elapse(monkeypatch):
    rl = RateLimiter()
    t = {"now": 1000.0}
    monkeypatch.setattr("aiko_gateway.domain.rate_limit.time.monotonic", lambda: t["now"])
    assert rl.hit("b", "ip", 1, 60)[0] is True
    assert rl.hit("b", "ip", 1, 60)[0] is False  # second within window — blocked
    t["now"] += 61  # window elapses
    assert rl.hit("b", "ip", 1, 60)[0] is True  # fresh window


def test_limiter_buckets_and_ips_are_independent():
    rl = RateLimiter()
    assert rl.hit("a", "ip1", 1, 1000)[0] is True
    assert rl.hit("a", "ip1", 1, 1000)[0] is False  # ip1/bucket-a exhausted
    assert rl.hit("b", "ip1", 1, 1000)[0] is True   # different bucket — own budget
    assert rl.hit("a", "ip2", 1, 1000)[0] is True   # different ip — own budget


def test_limiter_evicts_expired_windows(monkeypatch):
    rl = RateLimiter()
    t = {"now": 0.0}
    monkeypatch.setattr("aiko_gateway.domain.rate_limit.time.monotonic", lambda: t["now"])
    monkeypatch.setattr("aiko_gateway.domain.rate_limit._MAX_KEYS", 2)
    rl.hit("b", "ip1", 100, 60)
    rl.hit("b", "ip2", 100, 60)
    t["now"] += 61  # both windows now expired
    rl.hit("b", "ip3", 100, 60)  # trips eviction (len > 2)
    assert ("b", "ip1") not in rl._windows and ("b", "ip2") not in rl._windows


# ---- unit: client_ip (the security hinge) --------------------------------- #

def _req_with(headers: dict, client_host: str | None) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (client_host, 12345) if client_host else None,
    }
    return Request(scope)


def test_client_ip_takes_rightmost_xff_entry():
    # Caddy APPENDS the real peer; an attacker-spoofed left entry must be ignored.
    r = _req_with({"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 9.9.9.9"}, client_host="127.0.0.1")
    assert client_ip(r) == "9.9.9.9"


def test_client_ip_single_xff_entry():
    r = _req_with({"X-Forwarded-For": "203.0.113.7"}, client_host="127.0.0.1")
    assert client_ip(r) == "203.0.113.7"


def test_client_ip_falls_back_to_peer_when_no_xff():
    r = _req_with({}, client_host="203.0.113.50")
    assert client_ip(r) == "203.0.113.50"


def test_client_ip_unknown_when_nothing_available():
    r = _req_with({}, client_host=None)
    assert client_ip(r) == "unknown"


# ---- integration: 429 on a real endpoint ---------------------------------- #

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


# /passkey/register/start: anonymous, no body, writes a challenge — a clean target.
START = "/v1/auth/passkey/register/start"


@pytest.mark.asyncio
async def test_endpoint_429s_past_the_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_rate_limit", 3)
    monkeypatch.setattr(settings, "auth_rate_limit_window_seconds", 1000)
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    hdr = {"X-Forwarded-For": "198.51.100.1"}
    codes = [(await client.post(START, headers=hdr)).status_code for _ in range(3)]
    assert all(c == 200 for c in codes), codes
    blocked = await client.post(START, headers=hdr)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1


@pytest.mark.asyncio
async def test_limit_is_per_ip(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_rate_limit", 1)
    monkeypatch.setattr(settings, "auth_rate_limit_window_seconds", 1000)
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    assert (await client.post(START, headers={"X-Forwarded-For": "10.0.0.1"})).status_code == 200
    assert (await client.post(START, headers={"X-Forwarded-For": "10.0.0.1"})).status_code == 429
    # A different client IP has its own budget — not collateral-damaged.
    assert (await client.post(START, headers={"X-Forwarded-For": "10.0.0.2"})).status_code == 200


@pytest.mark.asyncio
async def test_spoofed_left_xff_entry_cannot_evade(client, monkeypatch):
    """Rotating the LEFT (attacker-suppliable) XFF entry must NOT mint fresh budget:
    the limiter keys on the rightmost (Caddy-appended) entry."""
    monkeypatch.setattr(settings, "auth_rate_limit", 1)
    monkeypatch.setattr(settings, "auth_rate_limit_window_seconds", 1000)
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    h1 = {"X-Forwarded-For": "1.1.1.1, 5.5.5.5"}
    h2 = {"X-Forwarded-For": "2.2.2.2, 5.5.5.5"}  # different left, SAME real peer
    assert (await client.post(START, headers=h1)).status_code == 200
    assert (await client.post(START, headers=h2)).status_code == 429


@pytest.mark.asyncio
async def test_disabled_flag_bypasses_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_rate_limit", 1)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    hdr = {"X-Forwarded-For": "192.0.2.9"}
    for _ in range(5):
        assert (await client.post(START, headers=hdr)).status_code == 200
