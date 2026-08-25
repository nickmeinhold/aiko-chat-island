"""Push reachability — an island that cannot reach its own devices says so (#3397).

THE STATE THIS EXISTS FOR. `push_service` gate 1 declines every wake when APNs is
unconfigured — correctly, because an operator who never set up push should not get
a crash. But the decline is silent and total, so an island holding registered
device tokens with no credentials is DEAF while every other signal reads healthy:
registration returns 201, the message persists, `/health` says ok, and the
recipient simply never hears anything.

That is not hypothetical. On 2026-08-23 a handset registered against enspyr (APNs
present-but-empty) while the credential lived on imagineering (zero tokens). Both
islands were internally correct; jointly the system was deaf and nothing said so.
Roughly four hours went into it, most of them spent reading a `0` from the wrong
island's `device_tokens` as a fact about the app rather than a fact about the query.

WHAT IS DELIBERATELY *NOT* HERE. There is no per-environment reachability, because
there is no such state: an APNs auth key (`.p8`) is environment-AGNOSTIC — the same
key authenticates against both hosts, proven 2026-08-23 — so a configured island can
reach a sandbox token and a production token alike. Reporting "reachable for
sandbox" separately would be a mechanism for a condition that cannot occur.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiko_gateway.config import settings
from aiko_gateway.domain import push_service, users_service
from aiko_gateway.domain.models import DeviceToken


async def _user_with_devices(session, n: int):
    user = await users_service.create_user(
        session, username="alice", display_name="Alice", password="pw")
    for i in range(n):
        session.add(DeviceToken(user_id=user.id, platform="apns",
                                token=f"{i}" * 64, push_environment="sandbox"))
    await session.commit()
    return user


@pytest.fixture
def configured(monkeypatch):
    for k, v in (("apns_key_id", "ABCDE12345"), ("apns_team_id", "TEAMID1234"),
                 ("apns_topic", "cc.example.app"),
                 ("apns_private_key", "-----BEGIN PRIVATE KEY-----")):
        monkeypatch.setattr(settings, k, v, raising=False)


@pytest.fixture
def unconfigured(monkeypatch):
    for k in ("apns_key_id", "apns_team_id", "apns_topic", "apns_private_key"):
        monkeypatch.setattr(settings, k, "", raising=False)


# ------------------------------------------------------------------ the report

async def test_unconfigured_island_holding_tokens_reports_them_unreachable(
    session, unconfigured
):
    """The exact enspyr state. The count is what makes it actionable — "push is
    off" is a shrug, "push is off AND 2 devices are registered to it" is a bug."""
    await _user_with_devices(session, 2)
    report = await push_service.reachability(session)
    assert report == {"configured": False, "registered_devices": 2,
                      "unreachable_devices": 2}


async def test_unconfigured_island_with_no_tokens_is_not_a_problem(
    session, unconfigured
):
    """Push simply not set up is a legitimate, intended state — most islands.
    It must NOT be reported as unreachable, or the signal becomes noise that
    every operator learns to ignore, which is worse than no signal."""
    report = await push_service.reachability(session)
    assert report == {"configured": False, "registered_devices": 0,
                      "unreachable_devices": 0}


async def test_configured_island_reaches_every_token_it_holds(session, configured):
    """Configured means reachable for EVERY token, sandbox and production alike:
    the .p8 authenticates against both hosts. There is no partial-reachability
    state to report, and inventing one would be a mechanism for an impossible
    condition."""
    await _user_with_devices(session, 3)
    report = await push_service.reachability(session)
    assert report == {"configured": True, "registered_devices": 3,
                      "unreachable_devices": 0}


# ------------------------------------------------------- the startup log line

async def test_startup_warns_when_devices_are_unreachable(
    session, unconfigured, caplog
):
    """The signal that would have ended the 4-hour investigation at minute one.
    Asserted on the WARNING level and on the count, because a DEBUG line nobody
    greps is the same silence in a different costume."""
    await _user_with_devices(session, 1)
    with caplog.at_level("WARNING"):
        await push_service.warn_if_unreachable(session)
    assert any(r.levelname == "WARNING" and "UNREACHABLE" in r.message.upper()
               for r in caplog.records), caplog.text
    assert "1" in caplog.text


async def test_startup_is_silent_when_there_is_nothing_to_say(
    session, configured, caplog
):
    """A warning that fires on a healthy island is a warning nobody reads.
    Positive control for the test above: same call, same fixtures, must be QUIET —
    without this, the assertion above could pass on a function that always logs."""
    await _user_with_devices(session, 2)
    with caplog.at_level("WARNING"):
        await push_service.warn_if_unreachable(session)
    assert not [r for r in caplog.records if r.levelname == "WARNING"], caplog.text


# -------------------------------------------------------------------- /health

@pytest_asyncio.fixture
async def health_client(session):
    from aiko_gateway.rest.deps import get_session

    async def _override():
        yield session

    app = FastAPI()
    from aiko_gateway import main as main_mod
    app.add_api_route("/health", main_mod.health, methods=["GET"])
    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_health_reports_live_reachability_not_a_boot_snapshot(
    health_client, session, unconfigured
):
    """READ LIVE, never cached at boot (#3193 is the standing complaint that
    /health reports config INTENT rather than reality — do not add a third
    instance). A device registered AFTER startup must appear immediately; a
    value computed once at boot would answer "at boot", not "now", which is a
    different question from the one the operator is asking."""
    first = await health_client.get("/health")
    assert first.json()["push"] == {"configured": False, "registered_devices": 0,
                                    "unreachable_devices": 0}
    await _user_with_devices(session, 1)
    second = await health_client.get("/health")
    assert second.json()["push"]["unreachable_devices"] == 1, (
        "/health served a boot-time snapshot instead of live state")


async def test_health_keeps_its_existing_shape(health_client, unconfigured):
    """Additive only — monitoring depends on these keys."""
    body = (await health_client.get("/health")).json()
    assert body["status"] == "ok"
    assert "aiko_connected" in body and "channels" in body
