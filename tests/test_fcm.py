"""FCM HTTP v1 — the Android transport, at exactly `apns.py`'s layer.

THREE SEMANTIC TRAPS, none of which a schema check catches, all of which pass
FCM's validator and then fail silently:

  1. a `notification` block makes the phone UNABLE TO RING (the system tray
     renders it and the app gets no code execution, so no foreground service, no
     full-screen intent, no Telecom);
  2. `android.ttl` is a RELATIVE protobuf Duration string while `apns-expiration`
     is an ABSOLUTE unix timestamp — reusing the APNs value emits a LEGAL
     ~56,000-year duration, clamped to FCM's four-week ceiling, with no error;
  3. reaping keyed on HTTP status deletes the whole device table the first time
     someone typos `PROJECT_ID`, because a wrong project is a 404 for EVERY
     device on the island.

Each gets a test AND a must-fail arm, because a guard whose outcome does not
depend on the thing it guards is not a guard.

THE LEGACY HAZARD: `fcm/send` + `Authorization: key=` + `to` + `time_to_live` +
lowercase `"high"` was decommissioned 2024-06-20. Most circulating FCM material
is legacy-shaped and none of it works, so the shape is asserted rather than
assumed.
"""
from __future__ import annotations

import json
import re

import httpx
import pytest
import pytest_asyncio

from aiko_gateway.config import settings
from aiko_gateway.domain import fcm
from aiko_gateway.domain.push_result import Verdict, WakePayload

CHANNEL = "01JDMCHANNELDM000000000000"
TOKEN = "cX9:APA91b" + "Z" * 140

# A syntactically real service-account blob. The private key is NEVER used here —
# `_access_token` is stubbed in every send test — so this carries no key material.
CREDENTIAL = json.dumps({
    "type": "service_account",
    "project_id": "aiko-island-test",
    "private_key_id": "0123456789abcdef",
    "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",
    "client_email": "island@aiko-island-test.iam.gserviceaccount.com",
    "token_uri": "https://oauth2.googleapis.com/token",
})


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "fcm_service_account_json", CREDENTIAL,
                        raising=False)
    fcm.reset_for_tests()
    yield
    fcm.reset_for_tests()


def _message(**kw) -> dict:
    return fcm.build_message(TOKEN, WakePayload(channel_id=CHANNEL), **kw)


# ------------------------------------------------------- the message shape

def test_a_call_message_carries_no_notification_block_anywhere(configured):
    """PROVES THE PHONE CAN RING, and it is a correctness property rather than a
    style choice. A message carrying `notification` is a DISPLAY message: a
    backgrounded or killed app gets NO code execution at all. Data-only at HIGH
    priority invokes `onMessageReceived` even out of Doze and earns Android 12+'s
    background foreground-service-start exemption.

    Asserted on the SERIALISED message, not on a key of the top-level dict, so a
    `notification` nested under `android` cannot slip past.
    """
    assert "notification" not in json.dumps(_message())


def test_every_data_value_is_a_string(configured):
    """`message.data` is `map<string,string>`. A non-string value or a nested
    object is a hard 400 INVALID_ARGUMENT — which fires for EVERY device on the
    island, not for one bad token."""
    data = _message()["message"]["data"]
    assert data == {"c": CHANNEL}
    assert all(isinstance(v, str) for v in data.values())


def test_the_android_priority_is_uppercase_high(configured):
    """Protobuf JSON enum parsing is case-sensitive. Lowercase `"high"` is the
    DECOMMISSIONED legacy API's spelling and is rejected here."""
    assert _message()["message"]["android"]["priority"] == "HIGH"


def test_the_target_is_message_token_not_a_legacy_field(configured):
    """v1's target is a `oneof` — exactly one of token/topic/condition, INSIDE
    `message`. Legacy `to` / `registration_ids` do not exist."""
    message = _message()["message"]
    assert message["token"] == TOKEN
    assert "to" not in message and "registration_ids" not in message


_TTL = re.compile(r"^\d+(\.\d+)?s$")


def _assert_ttl_is_a_relative_duration(ttl: str) -> None:
    """The guard both TTL tests share, so the must-fail arm exercises THE SAME
    assertion the real one does rather than a lookalike."""
    assert _TTL.match(ttl), f"{ttl!r} is not a protobuf Duration string"
    assert int(float(ttl[:-1])) <= 86400, (
        f"{ttl!r} is a legal duration but not a RELATIVE one — an absolute unix "
        "timestamp reused from apns-expiration matches the regex and clamps to "
        "FCM's four-week ceiling"
    )


def test_the_ttl_is_a_relative_duration_string_within_one_day(configured):
    """THE BOUND IS THE POINT, not the regex. `"1789000000s"` — an absolute epoch
    reused from `apns-expiration` — matches `^\\d+(\\.\\d+)?s$` perfectly, so a
    regex-only guard passes the exact value it exists to catch."""
    _assert_ttl_is_a_relative_duration(_message()["message"]["android"]["ttl"])


def test_an_absolute_epoch_ttl_would_be_rejected():
    """THE MUST-FAIL ARM. Built BEFORE the assertion was trusted: a harness
    examined for whether it CAN fail tends to look like it can."""
    import time
    with pytest.raises(AssertionError):
        _assert_ttl_is_a_relative_duration(f"{int(time.time())}s")


def test_a_collapse_key_rides_under_android_when_asked(configured):
    """Nesting is the whole difference from the legacy API, where `collapse_key`
    sat at the top level. Paired with the arm below, which asserts absence."""
    assert _message(collapse_key=CHANNEL)["message"]["android"]["collapse_key"] == CHANNEL
    assert "collapse_key" not in _message()["message"]["android"]


# ------------------------------------------------------------------ the URL

async def test_the_send_url_carries_the_project_id_from_the_credential_blob(
    configured, monkeypatch
):
    """DERIVED, never a second setting. A `fcm_project_id` field creates a class
    where the key and the project disagree, and that class's symptom is a 404 for
    every device on the island."""
    seen: list[httpx.Request] = []

    async def _run():
        return await fcm.send(TOKEN, WakePayload(channel_id=CHANNEL))

    await _drive(monkeypatch, seen, httpx.Response(200, json={"name": "projects/x/messages/1"}), _run)
    assert str(seen[0].url) == (
        "https://fcm.googleapis.com/v1/projects/aiko-island-test/messages:send")


# ------------------------------------------------------------ verdict mapping

def _error(status: int, code: str | None, *, extra_details: list | None = None) -> dict:
    details = list(extra_details or [])
    if code is not None:
        details.append({
            "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
            "errorCode": code,
        })
    return {"error": {"code": status, "status": "X", "message": "m",
                      "details": details}}


def test_unregistered_is_the_only_reaping_verdict():
    """Apple's 410 analogue: FCM making a positive claim about the DEVICE."""
    body = _error(404, "UNREGISTERED")
    assert fcm._verdict(404, fcm._fcm_error_code(body)) is Verdict.DEAD_TOKEN


def test_a_404_without_an_fcm_error_detail_does_not_reap():
    """THE MUST-FAIL ARM FOR THE TEST ABOVE, and the single reason the reap key
    is the FcmError detail rather than the HTTP status. A wrong `PROJECT_ID` in
    the URL is a bare 404 NOT_FOUND for EVERY device on the island; a
    status-keyed reaper would empty the device table on the first ring, and the
    recovery is every user reopening the app."""
    body = _error(404, None)
    assert fcm._fcm_error_code(body) == ""
    assert fcm._verdict(404, fcm._fcm_error_code(body)) is Verdict.REJECTED


def test_the_error_code_is_read_by_type_not_by_position():
    """`details` is an array that can carry `google.rpc.RetryInfo` alongside the
    `FcmError`. A `details[0]` reader goes red here."""
    body = _error(404, "UNREGISTERED", extra_details=[
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "10s"},
    ])
    assert body["error"]["details"][0]["@type"].endswith("RetryInfo")
    assert fcm._fcm_error_code(body) == "UNREGISTERED"


def test_invalid_argument_never_reaps():
    """THE BRIEF'S TAXONOMY, CORRECTED. `INVALID_ARGUMENT` is overwhelmingly OUR
    bug — a bad ttl string, a non-string data value, a lowercase "high", an
    unknown field — and therefore fires IDENTICALLY FOR EVERY DEVICE. It can also
    mean "this token string is unparseable" and the response gives no way to tell
    them apart. One direction costs a wasted request per send; the other destroys
    state nothing can rebuild."""
    body = _error(400, "INVALID_ARGUMENT")
    assert fcm._verdict(400, fcm._fcm_error_code(body)) is Verdict.REJECTED


def test_sender_id_mismatch_never_reaps():
    """A LIVE token minted against a different Firebase project — an island
    CONFIG fact, the structural twin of BadDeviceToken-from-the-wrong-environment,
    arriving as a 403. A second independent reason never to reap on a generic 4xx."""
    body = _error(403, "SENDER_ID_MISMATCH")
    assert fcm._verdict(403, fcm._fcm_error_code(body)) is Verdict.REJECTED


def test_transients_are_transient_and_nothing_else_reaps():
    assert fcm._verdict(200, "") is Verdict.DELIVERED
    assert fcm._verdict(429, "QUOTA_EXCEEDED") is Verdict.TRANSIENT
    assert fcm._verdict(503, "UNAVAILABLE") is Verdict.TRANSIENT
    assert fcm._verdict(500, "INTERNAL") is Verdict.TRANSIENT
    assert fcm._verdict(401, "") is Verdict.REJECTED


def test_only_unregistered_carries_a_reap_order():
    """The shared `SendResult` has a genuinely WEAKER arm on this side: FCM's
    UNREGISTERED response carries no timestamp, so `not_reregistered_since` is
    None and the compare-and-delete carries the reversibility alone."""
    order = fcm._reap_order_for(Verdict.DEAD_TOKEN)
    assert order is not None and order.not_reregistered_since is None
    assert fcm._reap_order_for(Verdict.REJECTED) is None
    assert fcm._reap_order_for(Verdict.TRANSIENT) is None
    assert fcm._reap_order_for(Verdict.DELIVERED) is None


# ------------------------------------------------------------------ the send

async def _drive(monkeypatch, seen: list, response, coro_factory):
    """Run `coro_factory()` against a MockTransport pinned as fcm's pooled client.

    `_access_token` is stubbed: these arms are about the MESSAGE endpoint, and
    the OAuth arms below drive the real exchange instead.
    """
    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response(request) if callable(response) else response

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    monkeypatch.setattr(fcm, "_client_singleton", client, raising=False)

    async def _token():
        return "stub-access-token"

    monkeypatch.setattr(fcm, "_access_token", _token)
    try:
        return await coro_factory()
    finally:
        await client.aclose()


async def test_a_200_is_delivered_and_the_body_is_the_v1_envelope(
    configured, monkeypatch
):
    seen: list[httpx.Request] = []
    result = await _drive(
        monkeypatch, seen,
        httpx.Response(200, json={"name": "projects/aiko-island-test/messages/1"}),
        lambda: fcm.send(TOKEN, WakePayload(channel_id=CHANNEL), collapse_key=CHANNEL))
    assert result.verdict is Verdict.DELIVERED and result.reap is None
    body = json.loads(seen[0].content)
    assert set(body) == {"message"}
    assert body["message"]["android"] == {
        "priority": "HIGH", "ttl": "60s", "collapse_key": CHANNEL}
    assert seen[0].headers["authorization"] == "Bearer stub-access-token"


async def test_an_unregistered_response_returns_a_reap_order(configured, monkeypatch):
    result = await _drive(
        monkeypatch, [],
        httpx.Response(404, json=_error(404, "UNREGISTERED")),
        lambda: fcm.send(TOKEN, WakePayload(channel_id=CHANNEL)))
    assert result.verdict is Verdict.DEAD_TOKEN
    assert result.reap is not None and result.reap.not_reregistered_since is None


async def test_a_transport_error_is_transient_and_never_raises(configured, monkeypatch):
    """`push_service`'s per-device `except Exception` must stay the EXCEPTIONAL
    path. If a transport raises for an ordinary network failure, Google being
    briefly unreachable reads as our bug forever."""
    def _boom(request):
        raise httpx.ConnectError("no route to host")

    result = await _drive(monkeypatch, [], _boom,
                          lambda: fcm.send(TOKEN, WakePayload(channel_id=CHANNEL)))
    assert result.verdict is Verdict.TRANSIENT


async def test_send_raises_only_when_unconfigured(monkeypatch):
    """The same raise contract `apns.send` states: `FcmNotConfigured` is a CALLER
    bug, because `push_service` gates on `is_configured()` first."""
    monkeypatch.setattr(settings, "fcm_service_account_json", "", raising=False)
    fcm.reset_for_tests()
    with pytest.raises(fcm.FcmNotConfigured):
        await fcm.send(TOKEN, WakePayload(channel_id=CHANNEL))


# ------------------------------------------------------------------- oauth

def _oauth(monkeypatch, seen: list, token_response):
    """A MockTransport that answers the Google token endpoint and the FCM send
    endpoint differently, with `_access_token` NOT stubbed."""
    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "oauth2.googleapis.com" in str(request.url):
            return token_response
        return httpx.Response(200, json={"name": "projects/x/messages/1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    monkeypatch.setattr(fcm, "_client_singleton", client, raising=False)
    monkeypatch.setattr(fcm, "_sign_assertion", lambda cred: "stub-assertion")
    return client


async def test_an_oauth_failure_returns_transient_and_never_raises(
    configured, monkeypatch
):
    """FCM auth is a NETWORK EXCHANGE that can fail independently of the send —
    the sharpest divergence from APNs, whose auth is local signing that can only
    fail on a broken key the boot guard already caught."""
    seen: list[httpx.Request] = []
    client = _oauth(monkeypatch, seen, httpx.Response(401, json={"error": "x"}))
    try:
        result = await fcm.send(TOKEN, WakePayload(channel_id=CHANNEL))
    finally:
        await client.aclose()
    assert result.verdict is Verdict.TRANSIENT
    assert all("oauth2" in str(r.url) for r in seen), (
        "a send went out on an unauthenticated request")


async def test_a_hard_oauth_rejection_is_negative_cached(configured, monkeypatch):
    """Without the negative cache one bad credential is one POST to Google's
    rate-limited token endpoint PER DEVICE PER RING, plus an ERROR line each."""
    seen: list[httpx.Request] = []
    client = _oauth(monkeypatch, seen, httpx.Response(401, json={"error": "x"}))
    try:
        await fcm.send(TOKEN, WakePayload(channel_id=CHANNEL))
        await fcm.send(TOKEN, WakePayload(channel_id=CHANNEL))
    finally:
        await client.aclose()
    assert len([r for r in seen if "oauth2" in str(r.url)]) == 1, (
        "the second send re-minted against a credential already known bad")


async def test_a_good_oauth_token_is_cached_across_sends(configured, monkeypatch):
    """THE POSITIVE CONTROL for the negative cache: a working credential must
    also mint once — otherwise "exactly one token request" above would be
    satisfied by a transport that never mints at all."""
    seen: list[httpx.Request] = []
    client = _oauth(monkeypatch, seen,
                    httpx.Response(200, json={"access_token": "ya29.stub",
                                              "expires_in": 3599}))
    try:
        first = await fcm.send(TOKEN, WakePayload(channel_id=CHANNEL))
        second = await fcm.send(TOKEN, WakePayload(channel_id=CHANNEL))
    finally:
        await client.aclose()
    assert first.verdict is Verdict.DELIVERED and second.verdict is Verdict.DELIVERED
    assert len([r for r in seen if "oauth2" in str(r.url)]) == 1
    assert len([r for r in seen if "fcm.googleapis.com" in str(r.url)]) == 2


async def test_the_assertion_requests_the_narrow_messaging_scope(configured, monkeypatch):
    """`firebase.messaging`, not `cloud-platform` — which also works and is far
    broader."""
    seen: list[httpx.Request] = []
    client = _oauth(monkeypatch, seen,
                    httpx.Response(200, json={"access_token": "ya29.stub",
                                              "expires_in": 3599}))
    try:
        await fcm.send(TOKEN, WakePayload(channel_id=CHANNEL))
    finally:
        await client.aclose()
    token_request = [r for r in seen if "oauth2" in str(r.url)][0]
    body = token_request.content.decode()
    assert "grant-type%3Ajwt-bearer" in body or "grant-type:jwt-bearer" in body
    assert fcm._SCOPE == "https://www.googleapis.com/auth/firebase.messaging"


# ------------------------------------------------------------------ configured

def test_is_configured_is_a_single_field(monkeypatch):
    """ONE field means no half-configured state exists — which is why this is
    `bool(...)` rather than the written-out `all()` `apns.is_configured` uses.
    Honest ONLY because the boot guard rejects an unparseable blob."""
    monkeypatch.setattr(settings, "fcm_service_account_json", "", raising=False)
    assert fcm.is_configured() is False
    monkeypatch.setattr(settings, "fcm_service_account_json", CREDENTIAL,
                        raising=False)
    assert fcm.is_configured() is True
