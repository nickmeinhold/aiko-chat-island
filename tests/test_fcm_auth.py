"""FCM's auth state machine, swept as a table (design 17 v4).

WHY A SWEEP AND NOT MORE TESTS. PR#192's cage-match found one auth defect in
every round for three rounds, each a (state, event) pair nobody had written
down. A list of cases written from memory is how the third one was missed. So
the expected behaviour is written below as DATA, one row per (phase, event
sample), and one test runs `transition` over every row. A second test proves the
table is TOTAL: it derives the phase and event classes from the `Phase` and
`Event` unions themselves, so adding a class without a row fails here instead
of waiting for a reviewer.

The table is copied from the design by hand, NOT generated from the code. A
table generated from the implementation would certify the implementation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import typing

import httpx
import pytest

from aiko_gateway.config import settings
from aiko_gateway.domain import fcm
from aiko_gateway.domain.push_result import Verdict, WakeKind, WakePayload

SINCE = 100.0   # when every sample phase was entered
NOW = 200.0     # when most sample events arrive
LATE = 400.0    # past every sample deadline (300)
DEADLINE = 300.0
TOKEN = "tok"

PHASES = {
    "empty": fcm.Empty(),
    "cached": fcm.Cached(TOKEN, DEADLINE),
    "mint_backoff": fcm.MintBackoff(DEADLINE),
    "send_denied": fcm.SendDenied(DEADLINE),
}

# name -> (event, the time it arrives)
EVENTS = {
    "minted": (fcm.Minted("fresh", 9999.0), NOW),
    "mint_failed:credential": (fcm.MintFailed("credential"), NOW),
    "mint_failed:unreadable": (fcm.MintFailed("unreadable"), NOW),
    "mint_failed:grant": (fcm.MintFailed("grant"), NOW),
    "blinked": (fcm.Blinked(), NOW),
    "refused:this_bearer": (fcm.Refused(TOKEN), NOW),
    "refused:other_bearer": (fcm.Refused("someone-else"), NOW),
    "denied:fresh": (fcm.Denied(sent_at=150.0), NOW),
    "denied:stale": (fcm.Denied(sent_at=50.0), NOW),
    "delivered": (fcm.Delivered(), NOW),
    "device_local": (fcm.DeviceLocal(), NOW),
    "tick:before_deadline": (fcm.Tick(), NOW),
    "tick:after_deadline": (fcm.Tick(), LATE),
    "unclassified": (fcm.Unclassified(404, ""), NOW),
}

KEEP = "keep"  # the SAME state object back: a real answer, tested as one

# (phase, event) -> KEEP, or (resulting phase, strikes: "same" | "+1" | "reset")
TABLE = {
    # ------------------------------------------------------------- empty
    ("empty", "minted"): ("cached", "same"),
    ("empty", "mint_failed:credential"): ("mint_backoff", "+1"),
    ("empty", "mint_failed:unreadable"): ("mint_backoff", "+1"),
    ("empty", "mint_failed:grant"): ("mint_backoff", "+1"),
    ("empty", "blinked"): KEEP,
    ("empty", "refused:this_bearer"): KEEP,
    ("empty", "refused:other_bearer"): KEEP,
    ("empty", "denied:fresh"): ("send_denied", "+1"),
    ("empty", "denied:stale"): KEEP,
    ("empty", "delivered"): ("empty", "reset"),
    ("empty", "device_local"): KEEP,
    ("empty", "tick:before_deadline"): KEEP,
    ("empty", "tick:after_deadline"): KEEP,
    ("empty", "unclassified"): KEEP,
    # ------------------------------------------------------------ cached
    ("cached", "minted"): ("cached", "same"),
    ("cached", "mint_failed:credential"): ("mint_backoff", "+1"),
    ("cached", "mint_failed:unreadable"): ("mint_backoff", "+1"),
    ("cached", "mint_failed:grant"): ("mint_backoff", "+1"),
    ("cached", "blinked"): KEEP,
    ("cached", "refused:this_bearer"): ("empty", "same"),
    ("cached", "refused:other_bearer"): KEEP,
    ("cached", "denied:fresh"): ("send_denied", "+1"),
    ("cached", "denied:stale"): KEEP,
    ("cached", "delivered"): ("cached", "reset"),
    ("cached", "device_local"): KEEP,
    ("cached", "tick:before_deadline"): KEEP,
    ("cached", "tick:after_deadline"): ("empty", "same"),
    ("cached", "unclassified"): KEEP,
    # ------------------------------------------------------ mint_backoff
    ("mint_backoff", "minted"): ("cached", "same"),
    ("mint_backoff", "mint_failed:credential"): ("mint_backoff", "+1"),
    ("mint_backoff", "mint_failed:unreadable"): ("mint_backoff", "+1"),
    ("mint_backoff", "mint_failed:grant"): ("mint_backoff", "+1"),
    ("mint_backoff", "blinked"): KEEP,
    ("mint_backoff", "refused:this_bearer"): KEEP,
    ("mint_backoff", "refused:other_bearer"): KEEP,
    ("mint_backoff", "denied:fresh"): ("send_denied", "+1"),
    ("mint_backoff", "denied:stale"): KEEP,
    ("mint_backoff", "delivered"): KEEP,
    ("mint_backoff", "device_local"): KEEP,
    ("mint_backoff", "tick:before_deadline"): KEEP,
    ("mint_backoff", "tick:after_deadline"): ("empty", "same"),
    ("mint_backoff", "unclassified"): KEEP,
    # ------------------------------------------------------- send_denied
    # Time alone ends a denial. `minted` above all: a fresh token from an
    # account that lost its role 403s again.
    **{("send_denied", e): KEEP for e in EVENTS if e != "tick:after_deadline"},
    ("send_denied", "tick:after_deadline"): ("empty", "same"),
}

PHASE_NAME = {fcm.Empty: "empty", fcm.Cached: "cached",
              fcm.MintBackoff: "mint_backoff", fcm.SendDenied: "send_denied"}


@pytest.mark.parametrize("phase_name,event_name", sorted(TABLE))
def test_transition_matches_the_table(phase_name, event_name):
    state = fcm.AuthState(PHASES[phase_name], strikes=2, since=SINCE)
    event, at = EVENTS[event_name]
    after = fcm.transition(state, event, at)
    expected = TABLE[(phase_name, event_name)]
    if expected == KEEP:
        assert after is state, f"{phase_name} x {event_name} must be a no-op"
        return
    want_phase, want_strikes = expected
    assert PHASE_NAME[type(after.phase)] == want_phase
    assert after.strikes == {"same": 2, "+1": 3, "reset": 0}[want_strikes]
    if PHASE_NAME[type(after.phase)] != phase_name:
        assert after.since == at, "a phase change must stamp when it happened"


def test_the_table_is_total_over_the_unions():
    """Derived from `Phase` and `Event` themselves, so a new class with no row
    fails HERE. The sample names map back to classes through EVENTS/PHASES."""
    phase_classes = set(typing.get_args(fcm.Phase))
    event_classes = set(typing.get_args(fcm.Event))
    assert {type(p) for p in PHASES.values()} == phase_classes
    assert {type(e) for e, _ in EVENTS.values()} == event_classes
    assert set(TABLE) == {(p, e) for p in PHASES for e in EVENTS}


def test_the_must_fail_arm_a_missing_row_is_caught():
    """The totality check above must be able to fail. Drop one row and the
    same comparison must disagree."""
    partial = dict(TABLE)
    partial.pop(("send_denied", "minted"))
    assert set(partial) != {(p, e) for p in PHASES for e in EVENTS}


def test_transition_is_pure():
    state = fcm.AuthState(fcm.Cached(TOKEN, DEADLINE), strikes=1, since=SINCE)
    snapshot = repr(state)
    for event, at in EVENTS.values():
        fcm.transition(state, event, at)
    assert repr(state) == snapshot


# ------------------------------------------------------------- the clocks

def test_grant_backoff_grows_from_a_short_base_to_the_cap():
    """A key minted seconds ago recovers fast (propagation measured <=30s); a
    deleted one decays to one POST per cap, not one every 10s forever."""
    state, windows = fcm.INITIAL, []
    for _ in range(9):
        state = fcm.transition(state, fcm.MintFailed("grant"), 0.0)
        windows.append(state.phase.until)
        state = fcm.transition(state, fcm.Tick(), state.phase.until)
    assert windows[:4] == [10.0, 20.0, 40.0, 80.0]
    assert max(windows) == fcm._BACKOFF_CAP_SECONDS


def test_denied_grows_and_a_delivered_send_resets_it():
    state, now = fcm.INITIAL, 1000.0
    for expected in (60.0, 120.0, 240.0):
        state = fcm.transition(state, fcm.Denied(sent_at=now), now)
        assert state.phase.until - now == expected
        now = state.phase.until
        state = fcm.transition(state, fcm.Tick(), now)
    state = fcm.transition(state, fcm.Delivered(), now)
    assert state.strikes == 0


def test_get_is_tri_state():
    assert fcm.get(fcm.AuthState(fcm.Empty())) == fcm.Mint()
    assert fcm.get(fcm.AuthState(fcm.Cached("t", 1.0))) == fcm.Have("t")
    assert fcm.get(fcm.AuthState(fcm.MintBackoff(1.0))) == fcm.Silent()
    assert fcm.get(fcm.AuthState(fcm.SendDenied(1.0))) == fcm.Silent()


# ---------------------------------------------------------- the classifiers

def _fcm_error(status: int, rpc_status: str, code: str | None) -> dict:
    details = [] if code is None else [{
        "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
        "errorCode": code}]
    return {"error": {"code": status, "status": rpc_status, "details": details}}


def test_the_island_wide_403_is_the_bare_one():
    """Measured 2026-10-08: a role-less account's send is 403, gRPC status
    PERMISSION_DENIED, and NO FcmError detail."""
    event = fcm.classify_send(403, _fcm_error(403, "PERMISSION_DENIED", None), "b", 5.0)
    assert event == fcm.Denied(sent_at=5.0)


def test_sender_id_mismatch_is_per_device_never_island_wide():
    """The 403 that DOES carry an FcmError code. Routing it to `Denied` would let
    one stale token from another project silence every Android ring."""
    event = fcm.classify_send(
        403, _fcm_error(403, "PERMISSION_DENIED", "SENDER_ID_MISMATCH"), "b", 5.0)
    assert event == fcm.DeviceLocal()


@pytest.mark.parametrize("body", [
    None, "PERMISSION_DENIED", {"error": "PERMISSION_DENIED"},
    {"error": {"status": ["PERMISSION_DENIED"]}}, {"error": {}}, [],
])
def test_a_garbled_403_is_unclassified_never_denied(body):
    """Tesla, temper r3: a misread 403 must not fall "by gravity" into `Denied`."""
    assert isinstance(fcm.classify_send(403, body, "b", 5.0), fcm.Unclassified)


@pytest.mark.parametrize("status,body,expected", [
    (200, {}, fcm.Delivered()),
    (401, {}, fcm.Refused("b")),
    (429, {}, fcm.Blinked()),
    (503, {}, fcm.Blinked()),
    (404, _fcm_error(404, "NOT_FOUND", "UNREGISTERED"), fcm.DeviceLocal()),
    (400, _fcm_error(400, "INVALID_ARGUMENT", "INVALID_ARGUMENT"), fcm.DeviceLocal()),
    (404, _fcm_error(404, "NOT_FOUND", None), fcm.Unclassified(404, "")),
])
def test_classify_send(status, body, expected):
    assert fcm.classify_send(status, body, "b", 5.0) == expected


@pytest.mark.parametrize("status,body,expected", [
    (400, {"error": "invalid_grant"}, fcm.MintFailed("grant")),
    (400, {"error": "invalid_request"}, fcm.MintFailed("unreadable")),
    (401, {"error": "invalid_client"}, fcm.MintFailed("unreadable")),
    (200, {"access_token": None}, fcm.MintFailed("unreadable")),
    (200, {"access_token": "t", "expires_in": "soon"}, fcm.MintFailed("unreadable")),
    (200, "not-a-dict", fcm.MintFailed("unreadable")),
    (503, None, fcm.Blinked()),
    (429, None, fcm.Blinked()),
])
def test_classify_mint(status, body, expected):
    assert fcm.classify_mint(status, body, 0.0) == expected


def test_a_good_mint_caches_for_its_lifetime_minus_the_skew():
    event = fcm.classify_mint(200, {"access_token": "t", "expires_in": 3599}, 10.0)
    assert event == fcm.Minted("t", 10.0 + 3599 - fcm._ACCESS_TOKEN_SKEW_SECONDS)


# ----------------------------------------------------- the orchestration

CREDENTIAL = json.dumps({
    "type": "service_account", "project_id": "aiko-island-test",
    "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
    "client_email": "ring@aiko-island-test.iam.gserviceaccount.com",
    "token_uri": "https://oauth2.googleapis.com/token",
})
CALL = "01JABCDEFGHJKMNPQRSTVWXYZ0"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "fcm_service_account_json", CREDENTIAL,
                        raising=False)
    monkeypatch.setattr(fcm, "_sign_assertion", lambda cred: "assertion")
    fcm.reset_for_tests()
    yield
    fcm.reset_for_tests()


def _invite() -> WakePayload:
    return WakePayload(channel_id="01JDMCHANNELDM000000000000",
                       kind=WakeKind.CALL_INVITE, call_id=CALL)


def _pin(monkeypatch, handler) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    async def _h(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return await handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_h))
    monkeypatch.setattr(fcm, "_client_singleton", client, raising=False)
    return client, seen


def _oauth(r: httpx.Request) -> bool:
    return "oauth2.googleapis.com" in str(r.url)


async def test_one_ring_to_many_devices_mints_once(configured, monkeypatch):
    """SINGLE-FLIGHT. The fanout is a concurrent gather; a cold cache used to be
    one mint PER DEVICE, not "one wasted exchange"."""
    async def handler(request):
        if _oauth(request):
            await asyncio.sleep(0.01)  # hold the flight open while the others arrive
            return httpx.Response(200, json={"access_token": "ya29", "expires_in": 3599})
        return httpx.Response(200, json={"name": "m"})

    client, seen = _pin(monkeypatch, handler)
    try:
        results = await asyncio.gather(*(fcm.send(f"t{i}", _invite()) for i in range(12)))
    finally:
        await client.aclose()
    assert all(r.verdict is Verdict.DELIVERED for r in results)
    assert len([r for r in seen if _oauth(r)]) == 1


async def test_a_revocation_ring_is_one_strike_and_silences_the_next(
    configured, monkeypatch
):
    """COALESCING. The first ring after a revocation returns N bare 403s; that is
    one fact, so one strike, and the next ring sends nothing at all."""
    async def handler(request):
        if _oauth(request):
            return httpx.Response(200, json={"access_token": "ya29", "expires_in": 3599})
        return httpx.Response(403, json=_fcm_error(403, "PERMISSION_DENIED", None))

    client, seen = _pin(monkeypatch, handler)
    try:
        first = await asyncio.gather(*(fcm.send(f"t{i}", _invite()) for i in range(20)))
        assert isinstance(fcm._state.phase, fcm.SendDenied)
        assert fcm._state.strikes == 1, "one revocation ring counted as many failures"
        sends_before = len([r for r in seen if not _oauth(r)])
        second = await fcm.send("t-next", _invite())
    finally:
        await client.aclose()
    assert sends_before == 20
    assert all(r.verdict is Verdict.TRANSIENT for r in first), (
        "a revocation is the ACCOUNT refused, not twenty bad devices")
    assert len([r for r in seen if not _oauth(r)]) == 20, "a send went out while denied"
    assert second.verdict is Verdict.TRANSIENT and second.reap is None
    assert all(r.reap is None for r in first)


async def test_a_hung_mint_releases_every_waiter_within_its_bound(
    configured, monkeypatch
):
    """Kelvin, temper r2: every caller waits on ONE flight, so the flight itself
    must be bounded, whatever the client's own timeout does."""
    monkeypatch.setattr(fcm, "_TIMEOUT_SECONDS", 0.05)

    async def handler(request):
        await asyncio.sleep(30)
        return httpx.Response(200)

    client, seen = _pin(monkeypatch, handler)
    try:
        tokens = await asyncio.wait_for(
            asyncio.gather(*(fcm._access_token() for _ in range(5))), timeout=2)
    finally:
        await client.aclose()
    assert tokens == [None] * 5
    assert isinstance(fcm._state.phase, fcm.Empty), (
        "a hang is a blink: it must not negative-cache the credential")
    assert len(seen) == 1


async def test_one_cancelled_waiter_does_not_cancel_the_flight(configured, monkeypatch):
    """`shield`: a ring torn down mid-mint must not take the mint with it."""
    release = asyncio.Event()

    async def handler(request):
        await release.wait()
        return httpx.Response(200, json={"access_token": "ya29", "expires_in": 3599})

    client, _ = _pin(monkeypatch, handler)
    try:
        doomed = asyncio.ensure_future(fcm._access_token())
        survivor = asyncio.ensure_future(fcm._access_token())
        await asyncio.sleep(0.01)
        doomed.cancel()
        await asyncio.sleep(0)
        release.set()
        assert await survivor == "ya29"
    finally:
        await client.aclose()
    assert doomed.cancelled()


async def test_an_unclassified_response_is_loud_and_changes_nothing(
    configured, monkeypatch, caplog
):
    async def handler(request):
        if _oauth(request):
            return httpx.Response(200, json={"access_token": "ya29", "expires_in": 3599})
        return httpx.Response(404, json=_fcm_error(404, "NOT_FOUND", None))

    client, _ = _pin(monkeypatch, handler)
    try:
        with caplog.at_level(logging.ERROR, logger="aiko_gateway.fcm"):
            await fcm.send("t", _invite())
    finally:
        await client.aclose()
    assert isinstance(fcm._state.phase, fcm.Cached)
    assert any("unclassified" in r.getMessage() for r in caplog.records)


async def test_the_mint_failure_message_does_not_blame_the_role(
    configured, monkeypatch, caplog
):
    """Minting never consults IAM (measured). The role belongs to SendDenied."""
    async def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    client, _ = _pin(monkeypatch, handler)
    try:
        with caplog.at_level(logging.ERROR, logger="aiko_gateway.fcm"):
            await fcm._access_token()
    finally:
        await client.aclose()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "invalid_grant" in text and "role" not in text


# ------------------------------------------------------------- visibility

def test_auth_status_is_not_ready_while_denied(configured):
    fcm._state = fcm.AuthState(fcm.SendDenied(until=float("inf")), strikes=1)
    assert fcm.auth_status() == {"ready": False, "phase": "send_denied", "strikes": 1}
    fcm._state = fcm.INITIAL
    assert fcm.auth_status()["ready"] is None, (
        "Empty is not evidence: never-tried must not read as working")
    fcm._state = fcm.AuthState(fcm.Cached("t", float("inf")))
    assert fcm.auth_status()["ready"] is True


async def test_health_says_android_not_ready_and_still_answers_200(
    configured, session
):
    """THE GUARD ON THE GUARD. The container healthcheck is `curl -f`, i.e. the
    status code only. Android being denied must change the BODY and never the
    status, or an IAM outage under `restart: always` becomes a restart loop that
    takes APNs down too (design 14's temper)."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from aiko_gateway import main as main_mod
    from aiko_gateway.rest.deps import get_session

    async def _override():
        yield session

    app = FastAPI()
    app.add_api_route("/health", main_mod.health, methods=["GET"])
    app.dependency_overrides[get_session] = _override
    fcm._state = fcm.AuthState(fcm.SendDenied(until=float("inf")), strikes=1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        response = await c.get("/health")
    assert response.status_code == 200
    assert response.json()["push"]["android_ready"] is False
    assert "strikes" not in json.dumps(response.json()["push"]), (
        "/health is public and booleans-only; the strike count stays internal")


def test_a_huge_strike_count_still_caps():
    assert fcm._grow(fcm._DENIED_BACKOFF_BASE_SECONDS, 10**6) == fcm._BACKOFF_CAP_SECONDS


async def test_a_flight_from_a_dead_loop_is_not_reused(configured, monkeypatch):
    """A pending task from a closed loop is never done(); reusing it would make
    every later mint raise (Maxwell, PR#192)."""
    other = asyncio.new_event_loop()
    stale = other.create_future()          # pending forever, wrong loop
    other.close()
    fcm._mint_flight = stale

    async def handler(request):
        return httpx.Response(200, json={"access_token": "ya29", "expires_in": 3599})

    client, _ = _pin(monkeypatch, handler)
    try:
        assert await fcm._access_token() == "ya29"
    finally:
        await client.aclose()
