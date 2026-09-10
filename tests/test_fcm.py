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
    # THIS TEST STUBS THE SIGNER, so it can only prove the send path FORWARDS what
    # the signer returned — and that is what it now asserts. It previously closed on
    # `assert fcm._SCOPE == "...firebase.messaging"`, which is a module constant
    # compared to a literal: true whether or not the scope ever reaches a JWT, so a
    # signer minting `cloud-platform`, or no scope at all, passed identically
    # (Tesla, cage-match PR#172 r1). The scope claim is checked for real in
    # `test_the_signed_assertion_actually_carries_the_narrow_scope`, which does NOT
    # stub the signer — that is the only place the question can honestly be asked.
    assert "stub-assertion" in body, (
        "the token request must carry the assertion the signer produced; this is "
        "the strongest claim a signer-stubbed test can make")


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


# ---------------------------------------------------------------------------
# THE CREDENTIAL THE BOOT LADDER BLESSES AND THE SIGNER CANNOT USE.
#
# config.py's ladder requires only ("project_id", "client_email", "private_key").
# A real service-account blob normally also carries "private_key_id", and the
# signer passed it straight into PyJWT's `kid` header — where a None is a hard
# rejection, not a shrug. So a credential that BOOTS FINE made every send raise:
# Android totally deaf, /health green, one traceback per device per ring, and the
# negative cache unreachable because it lives on the return path.
#
# `kid` is optional on a JWT-bearer assertion (Google identifies the key from
# `iss`), so the fix is to omit it rather than to widen the ladder.
# ---------------------------------------------------------------------------


def _credential_without_kid() -> str:
    """Exactly what the boot ladder accepts, and nothing more."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return json.dumps({
        "project_id": "test-project",
        "client_email": "svc@test-project.iam.gserviceaccount.com",
        "private_key": pem,
    })


def test_a_credential_with_no_private_key_id_still_signs():
    """THE MUST-FAIL ARM. Against the pre-fix signer this raises
    `InvalidTokenError: Key ID header parameter must be a string`."""
    cred = json.loads(_credential_without_kid())
    token = fcm._sign_assertion(cred)
    assert isinstance(token, str) and token.count(".") == 2
    import jwt as _jwt
    assert "kid" not in _jwt.get_unverified_header(token), (
        "an absent private_key_id must OMIT the kid header, never send a null one"
    )


def test_a_real_private_key_id_is_still_carried():
    """The positive control. Without it the test above would pass just as well if
    the signer dropped `kid` unconditionally — which would be a different bug."""
    cred = json.loads(_credential_without_kid())
    cred["private_key_id"] = "abc123"
    import jwt as _jwt
    assert _jwt.get_unverified_header(fcm._sign_assertion(cred))["kid"] == "abc123"


@pytest.mark.asyncio
async def test_an_unusable_credential_returns_transient_and_never_raises(monkeypatch):
    """The stated raise contract, enforced. `fcm.send`'s docstring promises it
    never raises for an auth failure; before this change `_credential()` and
    `_sign_assertion()` sat OUTSIDE every handler, so the promise held only for
    the network POST."""
    monkeypatch.setattr(settings, "fcm_service_account_json",
                        '{"project_id":"p","client_email":"e","private_key":"not-a-pem"}',
                        raising=False)
    fcm.reset_for_tests()
    # A REAL `WakePayload`, not a bare dict (Tesla, cage-match PR#172 r1). These
    # two tests passed `{"c": "chan"}` — the WIRE shape, which is why it looked
    # right — but production passes a `WakePayload`, and the credential path
    # returns BEFORE `build_message` ever reads `payload.channel_id`. So the
    # never-raises contract was proven only for an input the real door never sends:
    # a `build_message` that raised on the actual type would sail straight through.
    result = await fcm.send("f" * 100, WakePayload(channel_id=CHANNEL))
    assert result.verdict is Verdict.TRANSIENT
    assert result.reap is None, "an unusable credential must never reap a token"


@pytest.mark.asyncio
async def test_an_unusable_credential_is_negative_cached(monkeypatch):
    """Unlike a transport blip, a credential defect will not fix itself — so it is
    backed off rather than retried once per device per ring."""
    monkeypatch.setattr(settings, "fcm_service_account_json",
                        '{"project_id":"p","client_email":"e","private_key":"not-a-pem"}',
                        raising=False)
    fcm.reset_for_tests()
    await fcm.send("f" * 100, WakePayload(channel_id=CHANNEL))
    assert fcm._oauth_backoff_until is not None, (
        "the negative cache must be reachable from the credential path, not only "
        "from the HTTP-status path"
    )


def test_the_signed_assertion_actually_carries_the_narrow_scope():
    """`firebase.messaging`, not `cloud-platform` — asserted against the MINTED JWT.

    THE ARM THAT DISCRIMINATES: this signs with a real RSA key and decodes the
    resulting assertion, so the claim under test is a property of what
    `_sign_assertion` PRODUCED. Change `_SCOPE` to `cloud-platform` and this
    reddens; the signer-stubbed test upstairs would not notice, because it compared
    a module constant to a literal and never looked inside a JWT.

    Why the narrow scope matters enough to pin: `cloud-platform` also works, and is
    a grant over every Google Cloud API this service account can touch. An island
    minting that on every ring is handing itself authority it has no use for, and
    nothing outside this assertion would ever reveal it.
    """
    import jwt as _jwt
    cred = json.loads(_credential_without_kid())
    assertion = _jwt.decode(fcm._sign_assertion(cred), options={
        "verify_signature": False, "verify_aud": False})
    assert assertion["scope"] == "https://www.googleapis.com/auth/firebase.messaging", (
        f"the assertion requests {assertion['scope']!r}. Google will happily issue a "
        "token for a broader scope, so nothing downstream fails loudly — the JWT is "
        "the only place this is visible.")
    assert assertion["iss"] == cred["client_email"], (
        "iss must be the service-account email — Google identifies the signing key "
        "from it, which is why omitting `kid` is safe")


async def test_a_message_endpoint_401_drops_the_cached_bearer(configured, monkeypatch):
    """A bearer the SEND endpoint refuses must not stay cached (Tesla r2,
    Carnot r4).

    THE DEFECT: `_access_token` negative-caches failures of the OAuth EXCHANGE, but
    a token the exchange issued happily and the MESSAGE endpoint then refused stayed
    valid-by-the-clock for its full lifetime. A revoked key or disabled service
    account therefore made every Android send in the fanout — and every ring after
    it — fail identically for up to ~55 minutes, with no retry that could ever
    succeed and no operator-actionable line anywhere.

    WHY THIS DISCRIMINATES: it asserts on `_cached_access_token` DIRECTLY, before
    and after. Asserting only the verdict would pass unchanged with the fix
    reverted, because the verdict is REJECTED either way — the row is spared
    correctly in both worlds. The cache is the only place the difference exists.
    """
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "oauth2.googleapis.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "ya29.stub",
                                             "expires_in": 3599})
        return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    monkeypatch.setattr(fcm, "_client_singleton", client, raising=False)
    monkeypatch.setattr(fcm, "_sign_assertion", lambda cred: "stub-assertion")
    try:
        result = await fcm.send(TOKEN, WakePayload(channel_id=CHANNEL))
        assert result.verdict is Verdict.REJECTED
        assert result.reap is None, "an auth refusal must never reap a device row"
        assert fcm._cached_access_token is None, (
            "the bearer the message endpoint refused is still cached — every "
            "subsequent send will replay the same doomed credential until it "
            "expires on the clock")

        # AND THE BACKOFF IS ARMED, which is the half r4 got wrong (Tesla r6).
        # r4 asserted the next send RE-MINTS. That is precisely the incident: for a
        # credential-shaped refusal Google will happily issue another token — the
        # project's API is disabled, not the account — so the message endpoint 403s
        # again, the cache drops again, and the island posts one token mint PER
        # DEVICE PER RING with no backoff at all. r4's comment claimed the existing
        # `_OAUTH_FAILURE_BACKOFF_SECONDS` door would catch that; it could not,
        # because the exchange never fails. So the door is now armed here directly.
        assert fcm._oauth_backoff_until is not None, (
            "dropping the bearer without arming the backoff turns one bad "
            "credential into a token-mint storm, one per device per ring")
        await fcm.send(TOKEN, WakePayload(channel_id=CHANNEL))
        oauth_calls = [r for r in seen if "oauth2" in str(r.url)]
        assert len(oauth_calls) == 1, (
            f"the second send re-minted ({len(oauth_calls)} exchanges) — the "
            "backoff must suppress it, or a disabled project produces a mint per "
            "device per ring forever")
    finally:
        await client.aclose()


async def test_a_device_level_403_does_NOT_drop_the_island_wide_bearer(configured,
                                                                      monkeypatch):
    """`SENDER_ID_MISMATCH` is a DEVICE fact, not a credential death (Tesla r6).

    THE DEFECT THIS PINS: r4 keyed the bearer drop on the HTTP STATUS, so a single
    foreign Android row — a live token minted against a different Firebase project,
    which arrives as 403 — dropped the island-wide bearer for every OTHER device in
    the same fanout. One stale row could de-authenticate the whole ring.

    The discriminator is the presence of an `FcmError` detail: FCM attaches one when
    it is telling us about THIS message or device, and omits it when it is refusing
    our credential. `_verdict`'s own docstring already said so; r4 did not use it.

    THE ARM THAT DISCRIMINATES: the bearer must still be cached afterwards. Asserting
    only the verdict would pass either way — REJECTED is correct in both worlds.
    """
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "oauth2.googleapis.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "ya29.stub",
                                             "expires_in": 3599})
        return httpx.Response(403, json={"error": {
            "status": "PERMISSION_DENIED",
            "details": [{
                "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                "errorCode": "SENDER_ID_MISMATCH"}]}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    monkeypatch.setattr(fcm, "_client_singleton", client, raising=False)
    monkeypatch.setattr(fcm, "_sign_assertion", lambda cred: "stub-assertion")
    try:
        result = await fcm.send(TOKEN, WakePayload(channel_id=CHANNEL))
        assert result.verdict is Verdict.REJECTED
        assert result.reap is None, (
            "SENDER_ID_MISMATCH is a live token in another project — reaping it "
            "would delete a working registration")
        assert fcm._cached_access_token is not None, (
            "a device-level 403 dropped the island-wide bearer: one foreign row "
            "would de-authenticate every other send in the fanout")
        assert fcm._oauth_backoff_until is None, (
            "a device-level refusal must not arm the credential backoff")
    finally:
        await client.aclose()
