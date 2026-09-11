"""The APNs request headers — the wire this island actually puts on the network.

THE REPO HAD ZERO COVERAGE HERE UNTIL THIS FILE. Every other push test
monkeypatches `apns.send` wholesale (`FakeApns`), so nothing had ever asserted
the value of `apns-topic`, `apns-push-type`, `apns-priority` or
`apns-expiration`. That gap is exactly the wrong shape for the VoIP fork: every
kind/topic mismatch Apple can produce is a `400`, `_verdict` maps every
unhandled 400 to `REJECTED`, and `REJECTED` correctly never reaps — so a wrong
`.voip` suffix would ship as one WARNING line, no exception, no deleted row, and
no ring. The fail-safe direction is right and it is also why the bug is silent.

THE INSTRUMENT. `apns._client()` has no injection point, but the pooled client is
a module global chosen per-send only for the URL and headers, so a
`httpx.MockTransport` client assigned to `apns._client_singleton` captures the
real `send()` path end to end — `_topic_for`, `_render`, the headers dict and the
URL — with nothing stubbed but the socket. `_provider_token` IS stubbed: it is
the one part of `send()` that needs a real P-256 key, and authentication is not
what these tests claim to cover.

PAIRED ARMS THROUGHOUT, because a transport that always omitted `apns-collapse-id`
would satisfy the VoIP arm alone, and a transport that never forked would satisfy
the alert arm alone.
"""
from __future__ import annotations

import time

import httpx
import pytest
import pytest_asyncio

from aiko_gateway.config import settings
from aiko_gateway.domain import apns
from aiko_gateway.domain.models import ApnsEnvironment, TokenKind
from aiko_gateway.domain.push_result import WakeKind, WakePayload

CHANNEL = "01JDMCHANNELDM000000000000"


@pytest.fixture
def configured(monkeypatch):
    """An island WITH APNs credentials. Synthetic values only — a fixture that
    carries a production identifier teaches the next reader to paste real ones in."""
    monkeypatch.setattr(settings, "apns_key_id", "ABCDE12345", raising=False)
    monkeypatch.setattr(settings, "apns_team_id", "TEAMID1234", raising=False)
    monkeypatch.setattr(settings, "apns_topic", "cc.example.app", raising=False)
    # DELIBERATELY NOT `apns_topic + ".voip"`. The VoIP topic is STATED, not derived
    # (design 12 Decision 3; Nick, 2026-09-10), and a fixture carrying the derivable
    # value would let a re-derived implementation pass every test in this file —
    # the check would be independent of the thing it checks.
    monkeypatch.setattr(settings, "apns_voip_topic", "cc.example.app.stated.voip",
                        raising=False)
    monkeypatch.setattr(settings, "apns_private_key", "-----BEGIN PRIVATE KEY-----",
                        raising=False)
    # Signing needs a real P-256 key and is not what this file measures.
    monkeypatch.setattr(apns, "_provider_token", lambda: "stub-provider-jwt")
    apns.reset_for_tests()
    yield
    apns.reset_for_tests()


@pytest_asyncio.fixture
async def captured(monkeypatch):
    """Every request `apns.send` puts on the wire, in order."""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    monkeypatch.setattr(apns, "_client_singleton", client, raising=False)
    try:
        yield seen
    finally:
        await client.aclose()


async def _send(kind: TokenKind, *, collapse_id: str | None = CHANNEL,
                wake: WakeKind = WakeKind.CALL_INVITE):
    return await apns.send(
        "b" * 64, WakePayload(channel_id=CHANNEL, kind=wake),
        apns_environment=ApnsEnvironment.PRODUCTION,
        token_kind=kind, collapse_id=collapse_id)


# ---------------------------------------------------------------- alert (today)

async def test_an_alert_send_uses_the_bare_topic_and_push_type_alert(
    configured, captured
):
    """THE REGRESSION FENCE ON THE LIVE WIRE OF TWO PRODUCTION ISLANDS.

    Every row on both boxes is `token_kind='alert'` by server_default the moment
    0025 lands, and the alert ring is PROVEN on real handsets (2026-08-23,
    2026-08-29). Whatever the VoIP fork does, this must not move.
    """
    before = int(time.time())
    await _send(TokenKind.ALERT)
    after = int(time.time())

    assert len(captured) == 1
    request = captured[0]
    assert request.headers["apns-topic"] == "cc.example.app"
    assert request.headers["apns-push-type"] == "alert"
    assert request.headers["apns-priority"] == "10"
    assert before + 60 <= int(request.headers["apns-expiration"]) <= after + 60
    assert request.headers["apns-collapse-id"] == CHANNEL
    assert str(request.url) == f"https://api.push.apple.com/3/device/{'b' * 64}"


async def test_the_alert_payload_is_byte_identical_to_the_pre_refactor_wire(
    configured, captured
):
    """`WakePayload` moved the payload renderer BELOW the transport boundary. The
    dict that reaches Apple must not change by accident — this is the only
    assertion standing between a refactor and a silently different notification
    on two live islands.

    CHANGED ONCE, DELIBERATELY, 2026-09-11 (claude-tasks#4254): `"k"` was added.
    Recording the reasoning here because this fence's whole job is to make the
    next change an argued one, and a fence that is quietly re-pinned protects
    nothing.

    WHY IT IS SAFE ON THE LIVE ALERT WIRE, verified against the SHIPPED app
    rather than promised by the app tab: `ios/Runner/AppDelegate.swift:222` reads
    the payload as `guard let channelId = userInfo["c"] as? String` — a lookup of
    one known key, so an unknown key is ignored structurally by every build
    already on a handset. Nothing on any live island reads this dict
    exhaustively.

    `"k"` is `"call_invite"` here and that is not incidental: an ALERT row can
    never receive a `CALL_END` (`push_service.plan_deliveries` skips it with
    `end_wake_needs_voip`), so this is the only value this wire can carry."""
    import json

    await _send(TokenKind.ALERT)
    assert json.loads(captured[0].content) == {
        "aps": {
            "alert": {"title": "Incoming call", "body": "Tap to join"},
            "sound": "default",
        },
        "c": CHANNEL,
        "k": "call_invite",
    }


# ------------------------------------------------------------------ voip (new)

async def test_the_voip_payload_of_a_hangup_says_so_on_the_wire(
    configured, captured
):
    """THE RENDERED HALF of the stop/start distinction (claude-tasks#4254).

    `test_push_service` asserts the policy object carries `CALL_END`; this
    asserts the bytes Apple forwards to the handset carry it too. Adjacent
    evidence is not behaviour — a payload can be right and the renderer can drop
    the field, and the symptom would be a phone that rings when someone hangs up.

    `"k"` IS PRESENT ON BOTH KINDS, never absent-means-invite. An absent key and
    a defaulted key are different states, and collapsing them means the client
    cannot tell "an island that predates the end wake" from "a ring".
    """
    import json as _json

    await _send(TokenKind.VOIP, wake=WakeKind.CALL_END)
    body = _json.loads(captured[0].content)
    assert body["k"] == "call_end"
    assert body["c"] == CHANNEL

    captured.clear()
    await _send(TokenKind.VOIP)
    assert _json.loads(captured[0].content)["k"] == "call_invite", (
        "the renderer must read the payload's kind, not hard-code one")


async def test_a_voip_send_uses_the_dot_voip_topic_and_push_type_voip(
    configured, captured
):
    """Apple defines the VoIP topic AS `<bundle-id>.voip` — a suffixed namespace
    on one app record, not a second app, key or team. A wrong topic is a 400
    DeviceTokenNotForTopic, which is REJECTED, which never reaps: one WARNING
    line and a phone that does not ring.

    THE VALUE IS READ FROM CONFIG, NEVER COMPUTED. The fixture's voip topic is
    deliberately not `apns_topic + ".voip"`, so this assertion FAILS against a
    derived implementation. That is what makes it a test of the ruling rather
    than a restatement of Apple's naming convention.

    The 30s expiration is the RING LEASE (Nick, 2026-09-09, #3744): the island
    expires its own push, which is a fact about our delivery rather than a claim
    about the call.
    """
    before = int(time.time())
    await _send(TokenKind.VOIP)
    after = int(time.time())

    request = captured[0]
    assert request.headers["apns-topic"] == "cc.example.app.stated.voip"
    assert request.headers["apns-topic"] != "cc.example.app" + ".voip", (
        "the VoIP topic must come from settings.apns_voip_topic, not from "
        "suffixing settings.apns_topic — see apns._topic_for"
    )
    assert request.headers["apns-push-type"] == "voip"
    assert request.headers["apns-priority"] == "10"
    assert before + 30 <= int(request.headers["apns-expiration"]) <= after + 30


async def test_a_hangup_outlives_the_ring_lease_it_must_be_able_to_stop(
    configured, captured
):
    """THE ASYMMETRY BETWEEN A START AND A STOP, asserted on the wire
    (cage-match PR#176 r1, claude-tasks#4254).

    An end wake used to inherit `_VOIP_LEASE_SECONDS` because `token_kind` was the
    only axis deciding a push's lifetime — and that constant is THE RING LEASE, a
    number whose every justifying sentence is about a ring. Discarding a late
    invite is correct: the recipient reaches for a call that already ended.
    Discarding a late END is never correct, because ending an already-ended call is
    idempotent and free, while the thing it fails to stop is a CallKit ring that
    does not self-expire. Thirty-one seconds in a lift and the handset returns to a
    ring nothing can end — the phantom ring, arriving through the expiration header
    instead of the missing sentinel.

    THE PAIRED ARM IS THE POINT. Asserting the end's 300s alone would pass against
    an implementation that gave EVERY VoIP push 300s — which would silently delete
    the ring lease and restore the phantom-ring-on-a-late-invite this module
    already fought for. The two assertions only hold together if lifetime really is
    a function of the wake kind.
    """
    before = int(time.time())
    await _send(TokenKind.VOIP, wake=WakeKind.CALL_END)
    after = int(time.time())
    end_expiry = int(captured[0].headers["apns-expiration"])
    assert before + 300 <= end_expiry <= after + 300, (
        "the hangup must outlive the ring lease; an expiring stop is a ring that "
        "cannot be stopped")

    captured.clear()
    before = int(time.time())
    await _send(TokenKind.VOIP)
    after = int(time.time())
    invite_expiry = int(captured[0].headers["apns-expiration"])
    assert before + 30 <= invite_expiry <= after + 30, (
        "the INVITE must keep the 30s ring lease — widening it for both kinds "
        "deletes the ceiling and rings for a call that is already over")


async def test_a_voip_send_never_carries_a_collapse_id(configured, captured):
    """PAIRED WITH THE ALERT ARM ABOVE, which asserts the header IS present — a
    transport that dropped `apns-collapse-id` unconditionally would satisfy this
    test alone and silently lose lock-screen coalescing for every alert wake.

    Collapse identity is user-visible-notification coalescing and a VoIP push
    displays nothing. Whether APNs can REPLACE a queued VoIP push with it is
    UNVERIFIED, and a silently dropped ring is the one cost this design cannot
    pay. CallKit's own call UUID de-duplicates.
    """
    await _send(TokenKind.VOIP, collapse_id=CHANNEL)
    assert "apns-collapse-id" not in captured[0].headers


# ------------------------------------------------------------ the missing default

def test_send_requires_an_explicit_token_kind():
    """#3386's property, one axis over. `apns_environment` was made
    keyword-only-with-no-default so no caller could silently fall back to a
    global switch; a `token_kind=ALERT` default would recreate exactly that bug,
    and its symptom is an alert push to a VoIP-only handset — 400, REJECTED, no
    reap, no ring."""
    import inspect

    signature = inspect.signature(apns.send)
    parameter = signature.parameters["token_kind"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty, (
        "token_kind has a default — a caller that forgets it now sends the wrong "
        "push type instead of failing"
    )


def test_is_configured_counts_the_voip_topic_as_a_credential(monkeypatch):
    """Four-of-five must read as NOT configured (Carnot, cage-match PR#172 r1).

    THE ARM THAT MAKES THIS DISCRIMINATE: it sets the other four to real values and
    blanks ONLY `apns_voip_topic`. Before the fix this returned True, so the
    assertion below is not decoration — reverting the one-line change reddens
    exactly this test and nothing else. A test that set all five, or none, would
    pass identically against the old four-field tuple and prove nothing.
    """
    for field, value in (("apns_key_id", "KEYID12345"),
                         ("apns_team_id", "TEAMID6789"),
                         ("apns_topic", "cc.example.app"),
                         ("apns_private_key", "-----BEGIN PRIVATE KEY-----")):
        monkeypatch.setattr(settings, field, value, raising=False)

    monkeypatch.setattr(settings, "apns_voip_topic", "", raising=False)
    assert apns.is_configured() is False, (
        "four APNs credentials plus an EMPTY voip topic must read as NOT "
        "configured: apns_voip_topic joined the settings all-or-none group, so a "
        "predicate that ignores it claims a credential set the island does not have"
    )

    monkeypatch.setattr(settings, "apns_voip_topic", "cc.example.app.voip",
                        raising=False)
    assert apns.is_configured() is True, (
        "all five present must read as configured — otherwise the guard above is "
        "just switching push off permanently rather than tracking the credentials"
    )


@pytest.mark.asyncio
async def test_the_voip_body_is_pinned_even_though_its_shape_is_an_open_question(
    configured, captured
):
    """The VoIP BODY had no test at all (Carnot, cage-match PR#172 r5).

    `_render` emits `aps.alert` + `sound` unconditionally and both kinds go through
    it, so a VoIP push carries a payload whose semantics say "show a banner" under
    `apns-push-type: voip`. The header tests pin the alert body byte-for-byte and
    the VoIP tests pin only HEADERS — so the VoIP body could have been anything, or
    could change to anything, and the suite would not notice.

    WHAT THIS TEST DOES AND DOES NOT CLAIM. It pins TODAY'S shape so the wire cannot
    drift silently. It does NOT assert that shape is correct — that is a cross-repo
    contract question (the app's PushKit handler defines what a VoIP wake must
    carry for CallKit), and this repo's standing rule is that a wire change is
    agreed with aiko_chat_app BEFORE merge, never decided here. Raised with them
    rather than tie-broken.

    So: if someone changes the VoIP body, this reddens and forces the conversation.
    That is the entire point — the previous state was a wire nobody was watching.
    """
    await apns.send("v" * 64, WakePayload(channel_id=CHANNEL, kind=WakeKind.CALL_INVITE),
                    apns_environment=ApnsEnvironment.PRODUCTION,
                    token_kind=TokenKind.VOIP)
    import json as _json
    payload = _json.loads(captured[-1].content.decode())

    assert payload["c"] == CHANNEL, (
        "the channel id is the ONE field the client needs to route the ring; "
        "losing it makes the wake unactionable")
    # Pinned as OBSERVED, not as ENDORSED — see the docstring.
    assert payload["aps"] == {
        "alert": {"title": "Incoming call", "body": "Tap to join"},
        "sound": "default",
    }, ("the VoIP body changed. That may well be an improvement — a PushKit wake "
        "arguably should not carry an alert body at all — but it is a WIRE change "
        "and belongs in a conversation with the app tab, not in a silent diff.")
