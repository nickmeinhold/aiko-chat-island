"""Per-token APNs environment (#3386) — which WORLD a credential belongs to.

THE BOUNDARY UNDER TEST. An APNs device token is minted against exactly one of
Apple's two environments and is invalid against the other, yet carries no marking
that says which (`apns.py` says so itself, in the reaping rule). Before this the
island answered that question ONCE, globally, from `settings.apns_use_sandbox` —
so a box could ring debug builds or TestFlight builds, never both. The answer is
a property of the TOKEN; these tests pin it there.

The load-bearing assertion is `test_host_follows_the_token_not_the_island`: a
sandbox-registered token must reach the sandbox host on an island whose global
flag says production, and vice versa. If that one passes while the global flag is
set the OTHER way, the routing genuinely reads per-token.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from aiko_gateway.config import settings
from aiko_gateway.domain import apns, devices_service, security, users_service
from aiko_gateway.domain.models import DeviceToken, PushEnvironment
from aiko_gateway.rest import devices as device_routes
from aiko_gateway.rest.deps import get_session


async def _user(session, username: str):
    return await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")


@pytest_asyncio.fixture
async def client(session):
    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(device_routes.router)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _headers(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


# ------------------------------------------------------------- routing (crux)

@pytest.mark.parametrize("island_sandbox", [True, False])
def test_host_follows_the_token_not_the_island(monkeypatch, island_sandbox):
    """The whole point of the ticket. Parameterized over BOTH island settings so
    a `_host()` that silently kept reading the global would fail in one arm
    whichever way the flag happens to sit — a single-arm test would pass by
    coincidence on the arm that agrees with the token."""
    monkeypatch.setattr(settings, "apns_use_sandbox", island_sandbox, raising=False)
    assert apns._host(PushEnvironment.SANDBOX) == "https://api.sandbox.push.apple.com"
    assert apns._host(PushEnvironment.PRODUCTION) == "https://api.push.apple.com"


def test_host_rejects_an_unknown_environment(monkeypatch):
    """FAIL CLOSED, and never by falling through to the global. An out-of-set
    value can only arrive from a corrupted row or a future enum member added
    without touching this function; either way, guessing a host would send a live
    credential to the wrong world."""
    monkeypatch.setattr(settings, "apns_use_sandbox", True, raising=False)
    with pytest.raises(ValueError):
        apns._host("staging")  # type: ignore[arg-type]


# ------------------------------------------------------- registration default

async def test_registration_defaults_to_the_island_setting(client, session, monkeypatch):
    """Today's behaviour, preserved bit-for-bit: an app that does not declare an
    environment gets the island's — which is exactly what its token was minted
    against, because until the app opts in there is only one kind of build."""
    monkeypatch.setattr(settings, "apns_use_sandbox", True, raising=False)
    alice = await _user(session, "alice")
    resp = await client.post("/v1/devices", headers=_headers(alice),
                             json={"platform": "apns", "token": "a" * 64})
    assert resp.status_code == 201
    row = await session.get(DeviceToken, resp.json()["id"])
    assert row.push_environment == PushEnvironment.SANDBOX.value


async def test_explicit_environment_overrides_the_island_setting(
    client, session, monkeypatch
):
    """A TestFlight build on a sandbox-pinned island — the case the ticket exists
    for. The declared value wins; the island's flag is only ever the default."""
    monkeypatch.setattr(settings, "apns_use_sandbox", True, raising=False)
    bob = await _user(session, "bob")
    resp = await client.post(
        "/v1/devices", headers=_headers(bob),
        json={"platform": "apns", "token": "b" * 64, "push_environment": "production"})
    assert resp.status_code == 201
    row = await session.get(DeviceToken, resp.json()["id"])
    assert row.push_environment == PushEnvironment.PRODUCTION.value


async def test_out_of_set_environment_is_422_at_the_boundary(client, session):
    """Closed set enforced at the API edge, so a bad value is a 422 and not a 500
    from the DB CHECK — the same two-layer discipline as `platform`."""
    carol = await _user(session, "carol")
    resp = await client.post(
        "/v1/devices", headers=_headers(carol),
        json={"platform": "apns", "token": "c" * 64, "push_environment": "staging"})
    assert resp.status_code == 422


async def test_reregistration_moves_the_environment(client, session, monkeypatch):
    """A device that changes builds (debug -> TestFlight) keeps its token string
    in the same row via the upsert, so the environment MUST move with it. A
    re-register that refreshed user_id but left a stale environment would route
    a live token to the wrong world for the rest of that row's life."""
    monkeypatch.setattr(settings, "apns_use_sandbox", True, raising=False)
    dave = await _user(session, "dave")
    first = await client.post("/v1/devices", headers=_headers(dave),
                              json={"platform": "apns", "token": "d" * 64})
    again = await client.post(
        "/v1/devices", headers=_headers(dave),
        json={"platform": "apns", "token": "d" * 64, "push_environment": "production"})
    assert first.json()["id"] == again.json()["id"]  # same row, upserted
    row = await session.get(DeviceToken, again.json()["id"])
    await session.refresh(row)
    assert row.push_environment == PushEnvironment.PRODUCTION.value


# ------------------------------------------------------------------- DB CHECK

async def test_db_check_rejects_an_out_of_set_environment(session):
    """Defense BEYOND the API (the #11 pattern): a direct SQL writer that bypasses
    the router still cannot store an out-of-set environment."""
    erin = await _user(session, "erin")
    with pytest.raises(Exception) as exc:
        await session.execute(
            text("INSERT INTO device_tokens "
                 "(id, user_id, platform, token, push_environment, created_at, updated_at) "
                 "VALUES ('01AAAAAAAAAAAAAAAAAAAAAAAA', :u, 'apns', 'zzz', "
                 "'staging', :t, :t)"),
            {"u": erin.id, "t": "2026-08-25T00:00:00+00:00"})
        await session.commit()
    assert "ck_device_tokens_push_environment" in str(exc.value) or "CHECK" in str(exc.value)


async def test_db_rejects_a_null_environment(session):
    """NOT NULL is the half a CHECK cannot do: `NULL IN ('a','b')` is UNKNOWN,
    which a CHECK constraint PASSES (the 0022 lesson). Without this the closed
    set would have a silent third member."""
    frank = await _user(session, "frank")
    with pytest.raises(Exception):
        await session.execute(
            text("INSERT INTO device_tokens "
                 "(id, user_id, platform, token, push_environment, created_at, updated_at) "
                 "VALUES ('01BBBBBBBBBBBBBBBBBBBBBBBB', :u, 'apns', 'yyy', "
                 "NULL, :t, :t)"),
            {"u": frank.id, "t": "2026-08-25T00:00:00+00:00"})
        await session.commit()


# ------------------------------------------------------------------- service

async def test_service_stores_the_declared_environment(session, monkeypatch):
    """The service is the one door (route, in-process and test paths all pass
    here), so the default resolution lives in it and not in the router."""
    monkeypatch.setattr(settings, "apns_use_sandbox", False, raising=False)
    gina = await _user(session, "gina")
    row = await devices_service.register_device(
        session, user_id=gina.id, platform="apns", token="g" * 64,
        push_environment=PushEnvironment.SANDBOX)
    assert row.push_environment == PushEnvironment.SANDBOX.value


async def test_reregistration_without_a_declaration_preserves_the_stored_value(
    client, session, monkeypatch
):
    """OMISSION PRESERVES (cage-match, Carnot + Maxwell). A device that declared
    'production' and later re-registers WITHOUT the field must keep production —
    re-resolving the island default would silently reset a live TestFlight token
    to sandbox and break it until the app registered again.

    Asymmetry is the argument: APNs mints a DIFFERENT token string per
    environment, so "same token, environment changed" barely exists, while "a
    client version stopped sending the field" is an ordinary regression."""
    monkeypatch.setattr(settings, "apns_use_sandbox", True, raising=False)
    heidi = await _user(session, "heidi")
    first = await client.post(
        "/v1/devices", headers=_headers(heidi),
        json={"platform": "apns", "token": "h" * 64,
              "push_environment": "production"})
    again = await client.post("/v1/devices", headers=_headers(heidi),
                              json={"platform": "apns", "token": "h" * 64})
    assert first.json()["id"] == again.json()["id"]
    row = await session.get(DeviceToken, again.json()["id"])
    await session.refresh(row)
    assert row.push_environment == PushEnvironment.PRODUCTION.value, (
        "an omitted declaration re-resolved the island default over an "
        "explicitly-registered environment")


async def test_an_empty_environment_is_rejected_not_defaulted(session):
    """`is None`, not falsy (cage-match, Carnot HIGH). An empty string is an
    INVALID closed-set value; `or` would have quietly turned it into the island
    default — an invalid value becoming a valid one inside the module that claims
    to be the single door. It must reach the DB CHECK and be refused."""
    ivan = await _user(session, "ivan")
    with pytest.raises(Exception):
        await devices_service.register_device(
            session, user_id=ivan.id, platform="apns", token="i" * 64,
            push_environment="")  # type: ignore[arg-type]
        await session.commit()


async def test_a_corrupt_environment_row_is_skipped_not_fatal(monkeypatch):
    """The conversion moved from `_host` to the ORM edge (cage-match, Carnot
    MEDIUM), so prove the blast radius did NOT move with it: a row carrying an
    out-of-set string must still raise INSIDE push_service's per-device try —
    logged, skipped, never reaped, and never abandoning the rest of the fanout.

    Asserted at the conversion itself rather than through a full fanout because
    that is the line that changed; the per-device boundary it lands in is already
    proven by test_one_exploding_device_does_not_abandon_the_others."""
    with pytest.raises(ValueError):
        PushEnvironment("staging")


def test_host_accepts_an_in_set_bare_string_and_rejects_every_other(monkeypatch):
    """PINS THE REAL CONTRACT (cage-match, Carnot round 3).

    Carnot flagged that `PushEnvironment` is a `StrEnum`, so `_host("sandbox")`
    matches `case PushEnvironment.SANDBOX` and the enum signature is not enforced
    at runtime. The mechanism is TRUE — asserted below. The proposed remedy, an
    `isinstance` guard, is rejected: it would make a CORRECT call raise while
    buying no safety, because the property that actually matters is that every
    value OUTSIDE the closed set fails closed, and it already does.

    Carnot's round-2 wording said a `str` signature "allows every caller to pass
    'prod', 'production '". It does not: both raise, as pinned here. So the leak
    is one of type purity, not of behaviour — an in-set string produces the
    correct host, and nothing else produces any host at all.

    Python type hints are never runtime-enforced anywhere in this codebase;
    guarding this one seam would be a mechanism where a statement does the job.
    The statement is this test."""
    monkeypatch.setattr(settings, "apns_use_sandbox", True, raising=False)
    assert apns._host("sandbox") == apns._host(PushEnvironment.SANDBOX)
    assert apns._host("production") == apns._host(PushEnvironment.PRODUCTION)
    for bad in ("prod", "production ", " sandbox", "SANDBOX", "staging", ""):
        with pytest.raises(ValueError):
            apns._host(bad)  # type: ignore[arg-type]
