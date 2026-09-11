"""Push wake — the gates that decide whether a handset gets woken (#3267 inc 2).

The boundary under test is "who may cause someone's phone to interrupt them",
which is a strictly louder capability than delivering a message. So the gates get
tested as PAIRS wherever a naive implementation would pass the happy arm alone:

  * unconfigured sends nothing / configured sends something,
  * 410 reaps the row / 400 BadDeviceToken does NOT reap it,
  * the sentinel wakes / a message merely starting with the sentinel does not.

The 410-vs-400 pair is the one that matters most. A reaper that deletes on both
codes passes every 410 test ever written, and would delete every registered
device the first time an operator sets `apns_use_sandbox` the wrong way — Apple
returns `BadDeviceToken` for a valid token sent to the wrong environment, and a
token carries no marking that distinguishes the two. The 400 test is the only
thing standing between that config slip and an empty table.

Built from the domain services only (never `main`), keeping the suite's
"never import aiko_services" isolation invariant.
"""
from __future__ import annotations
import time
from sqlalchemy import delete as sa_delete

import asyncio
import contextlib
import dataclasses
import re
import pathlib
import logging
import datetime as dt

import pytest
import pytest_asyncio

from aiko_gateway.config import settings
from aiko_gateway.domain import apns, push_service, users_service
import sqlalchemy as sa

from aiko_gateway.domain.models import (
    Message,
    ApnsEnvironment, Channel, ChannelKind, DeviceToken, Membership, TokenKind,
)
from aiko_gateway.domain.push_result import ReapOrder, SendResult, Verdict

CHANNEL = "01JDMCHANNELDM000000000000"
OTHER_CHANNEL = "01OTHERCHANNEL0000000000"

# A synthetic FCM service-account blob. No key material: every FCM test here
# replaces `apns.send` wholesale, so nothing ever signs anything.
FCM_CREDENTIAL = (
    '{"type":"service_account","project_id":"aiko-island-test",'
    '"private_key_id":"0123456789abcdef",'
    '"private_key":"-----BEGIN PRIVATE KEY-----\\nx\\n-----END PRIVATE KEY-----\\n",'
    '"client_email":"island@aiko-island-test.iam.gserviceaccount.com"}'
)


class FakeApns:
    """Records every send and returns programmed verdicts.

    Deliberately NOT a subclass or a mock of the real client: the point is to
    exercise `push_service`'s policy, and a fake that shares the transport's
    implementation would be blind to bugs in the shared layer. It answers only
    the question the service asks — "what did Apple say?".
    """

    def __init__(self, verdict: apns.Verdict = apns.Verdict.DELIVERED):
        self.verdict = verdict
        # Apple's 410 timestamp, in ms. None = "Apple sent no timestamp", which
        # the reaper must treat as NO EVIDENCE rather than as the epoch. Kept in
        # Apple's own units and converted here, because converting is exactly
        # what the real transport does with a 410 body — a fake that took a
        # ready-made ReapOrder would skip the step where the rule lives.
        self.invalid_since_ms: int | None = None
        self.sent: list[tuple[str, dict, str | None]] = []
        # The APNs environment the service asked for, per send (#3386). Recorded
        # separately from `sent` so the existing unpacking sites stay a 3-tuple.
        self.environments: list[ApnsEnvironment] = []
        # The token kind the service asked for, per send. Same reason.
        self.kinds: list[TokenKind] = []

    async def __call__(self, device_token, payload, *, apns_environment,
                       token_kind, collapse_id=None):
        self.sent.append((device_token, payload, collapse_id))
        self.environments.append(apns_environment)
        self.kinds.append(token_kind)
        # Returns the SAME shape the real transport returns. A fake whose
        # contract has drifted from the real API tests a system that does not
        # exist — this one drifted once already, when send() grew SendResult, and
        # the suite caught it immediately because every test goes through here.
        # CALLS the real rule rather than MIRRORING it (Tesla, cage-match PR#172
        # r2). This block used to re-implement `apns._reap_order` line for line, and
        # said so in a comment that read as a virtue — "MIRRORS apns._reap_order
        # EXACTLY". A mirror is not an independent instrument: the fake and the
        # function shared a representation, so they could not fail differently.
        # If `_reap_order` ever started returning `ReapOrder()` — the DESTRUCTIVE
        # default, a dateless order authorising an unbounded delete — for a
        # timestamp-less 410, this fake would keep returning None and every
        # reap-refusal test in the suite would stay green while the island emptied
        # the device table. The one behaviour these tests exist to protect is the
        # one a duplicated rule cannot check.
        #
        # Deriving from the production function makes that change PROPAGATE into
        # the tests instead of being hidden by them. The fake still owns the INPUT
        # (`invalid_since_ms` — Apple's units, converted by the real code, which is
        # where the rule lives); it no longer owns the DECISION.
        reap = apns._reap_order(self.verdict, self.invalid_since_ms)
        return apns.SendResult(self.verdict, reap)


@pytest.fixture
def configured(monkeypatch):
    """An island WITH working APNs credentials."""
    monkeypatch.setattr(settings, "apns_key_id", "ABCDE12345", raising=False)
    # Synthetic, not the real Team ID: a fixture that carries a production
    # identifier teaches the next reader to paste real ones in.
    monkeypatch.setattr(settings, "apns_team_id", "TEAMID1234", raising=False)
    monkeypatch.setattr(settings, "apns_topic", "cc.example.app", raising=False)
    monkeypatch.setattr(settings, "apns_private_key", "-----BEGIN PRIVATE KEY-----",
                        raising=False)
    # THE FIFTH CREDENTIAL. `apns.is_configured()` counts the VoIP topic (Carnot,
    # cage-match PR#172 r1), matching the settings all-or-none group — so a fixture
    # naming only four describes an island that CANNOT boot, and every test using it
    # would silently exercise the no-configured-platform path instead of the send
    # path it was written for.
    monkeypatch.setattr(settings, "apns_voip_topic", "cc.example.app.voip",
                        raising=False)
    apns.reset_for_tests()
    yield
    apns.reset_for_tests()


@pytest.fixture
def apns_unconfigured(monkeypatch):
    for k in ("apns_key_id", "apns_team_id", "apns_topic", "apns_private_key",
              "apns_voip_topic"):
        monkeypatch.setattr(settings, k, "", raising=False)


@pytest.fixture
def end_wake_gate_open(monkeypatch):
    """Opens the end-wake interlock for tests that are about the end wake's
    ROUTING rather than about the gate itself.

    The interlock (`END_WAKE_VOIP_GATE_OPEN`) is CLOSED in the shipped code until
    claude-tasks#4278 answers, so without this the end-wake tests would silently
    become tests of the gate — passing for the wrong reason, and leaving the
    routing they were written to prove completely unexercised the day the gate
    opens. Requesting it explicitly is what keeps the two questions separate.
    """
    monkeypatch.setattr(push_service, "END_WAKE_VOIP_GATE_OPEN", True)


@pytest.fixture
def fake_apns(monkeypatch):
    fake = FakeApns()
    monkeypatch.setattr(apns, "send", fake)
    return fake


@pytest_asyncio.fixture
async def dm(session, monkeypatch):
    """A two-party DM: alice (caller) and bob (callee, one iPhone registered).

    `push_service` opens its OWN session (it runs detached from the request), so
    the factory is pointed at the test session — and must NOT close it, or the
    assertions afterwards would run against a dead session.

    BOB HAS SPOKEN HERE, and that is now load-bearing (claude-tasks#4216). The
    conduct gate only rings a recipient who has posted in the channel before, so a
    fixture where the callee never speaks describes FIRST CONTACT — where the
    correct behaviour is silence. Adding bob's message makes this fixture an
    ESTABLISHED conversation, which is what every ring test here is actually about.
    Thirteen tests began failing when the gate landed; that was the gate working,
    not a regression, and the fix is to say which world the fixture is in rather
    than to weaken the gate. `test_a_first_contact_call_invite_does_not_wake` holds
    the other world.
    """
    alice = await users_service.create_user(
        session, username="alice", display_name="Alice", password="pw")
    bob = await users_service.create_user(
        session, username="bob", display_name="Bob", password="pw")
    # A REAL private DM channel row. The service reads the channel itself rather
    # than trusting the caller's `channel_kind` (cage-match #139 round 6), so a
    # fixture of bare Membership rows no longer wakes anything — correctly.
    session.add_all([
        Channel(id=CHANNEL, name="alice-bob", kind=ChannelKind.DM.value,
                aiko_channel="dm:alice-bob", is_private=True,
                community_id=sa.null()),
        Membership(channel_id=CHANNEL, user_id=alice.id),
        Membership(channel_id=CHANNEL, user_id=bob.id),
        DeviceToken(user_id=bob.id, platform="apns", token="b" * 64),
        Message(id="01BOBSPOKEHERE00000000000", channel_id=CHANNEL,
                sender_user_id=bob.id, sender_kind="user", body="hi"),
    ])
    await session.commit()

    @contextlib.asynccontextmanager
    async def _factory():
        yield session

    monkeypatch.setattr(push_service, "SessionLocal", _factory)
    return alice, bob


async def _wake(body: str = push_service.CALL_INVITE_BODY, *, sender_id: str,
                kind: str = "dm", exclude: set[str] | None = None):
    await push_service.wake_for_message(
        channel_id=CHANNEL, channel_kind=kind, sender_id=sender_id,
        body=body, exclude_user_ids=exclude or set())


# --------------------------------------------------------------------------
# The sentinel is a wire contract with the app, in another repo.
# --------------------------------------------------------------------------

def test_sentinel_is_pinned_byte_for_byte():
    """A ONE-WAY DOOR: this string is inside signatures already sent to both live
    islands and stored in permanent history. It is duplicated in the app repo
    (`call_invite.dart`) because the two halves cannot import from each other, so
    the only thing holding them in sync is this assertion and its twin.

    Asserted by CODEPOINT, not just by equality with itself — a look-alike
    substitution (U+00B7 MIDDLE DOT for U+2022 BULLET, a different phone emoji)
    would silently stop every ring on every device with no error anywhere, and a
    plain string literal comparison in a source file is exactly where such a
    substitution hides from a human reader.
    """
    assert push_service.CALL_INVITE_BODY == "aiko:call/1 · 📞 started a call"
    assert [ord(c) for c in push_service.CALL_INVITE_BODY[:14]] == [
        ord("a"), ord("i"), ord("k"), ord("o"), ord(":"), ord("c"), ord("a"),
        ord("l"), ord("l"), ord("/"), ord("1"), ord(" "), 0x00B7, ord(" "),
    ]
    assert push_service.CALL_INVITE_BODY[14] == "\U0001F4DE"


def test_end_sentinel_is_pinned_byte_for_byte():
    """The hangup's twin of `test_sentinel_is_pinned_byte_for_byte`, and it is a
    ONE-WAY DOOR for the same reason plus one: the app has been signing this body
    since 2026-08-22, so it is ALREADY in permanent history on enspyr. The island
    is the late half here — it is learning to read a string the client has been
    emitting for three weeks.

    Codepoints, not just equality with itself: a look-alike substitution (U+2022
    BULLET for U+00B7 MIDDLE DOT, a different phone emoji) would silently stop
    every hangup from reaching a locked handset — and the failure is INVISIBLE,
    because the call still ends everywhere the app is awake to see it. The ring
    that does not stop is on someone else's phone.
    """
    assert push_service.CALL_END_BODY == "aiko:call/1 \u00b7 \U0001f4de ended the call"
    assert [ord(c) for c in push_service.CALL_END_BODY[:14]] == [
        ord("a"), ord("i"), ord("k"), ord("o"), ord(":"), ord("c"), ord("a"),
        ord("l"), ord("l"), ord("/"), ord("1"), ord(" "), 0x00B7, ord(" "),
    ]
    assert push_service.CALL_END_BODY[14] == "\U0001F4DE"
    # NOT THE INVITE. Both sentinels share a 12-character prefix, so a copy-paste
    # that edited the constant's NAME and not its VALUE would leave two names for
    # one string — and `should_wake` would return CALL_INVITE for a hangup, which
    # is the ring-that-will-not-stop wearing a green test.
    assert push_service.CALL_END_BODY != push_service.CALL_INVITE_BODY


@pytest.mark.skipif(
    not (pathlib.Path(__file__).resolve().parents[2] / "aiko_chat_app"
         / "lib/features/call/domain/call_invite.dart").exists(),
    reason="app repo not checked out beside this one")
def test_both_sentinels_match_the_app_repo_source_when_it_is_present():
    """A SECOND INSTRUMENT THAT FAILS DIFFERENTLY (the codepoint pins above are
    the first). Those assert that the constant has not changed; this asserts that
    it agrees with the OTHER REPO, which is the property that actually matters
    and which no amount of self-consistency can establish. A codec pinned only
    against its own inverse can be self-consistently wrong.

    SKIPPED, NOT REQUIRED, and that is honest rather than convenient: CI has no
    app checkout, so this cannot be the gate — the codepoint pins are. It earns
    its place on a developer machine, where the two repos ARE side by side and a
    drift introduced by either half surfaces the moment anyone runs the suite,
    instead of at a handset.
    """
    dart = (pathlib.Path(__file__).resolve().parents[2] / "aiko_chat_app"
            / "lib/features/call/domain/call_invite.dart").read_text()
    for const, ours in (("kCallInviteBody", push_service.CALL_INVITE_BODY),
                        ("kCallEndBody", push_service.CALL_END_BODY)):
        m = re.search(r"const String " + const + r" = '([^']*)';", dart)
        assert m, f"{const} not found in the app source — it moved or was renamed"
        assert m.group(1) == ours, (
            f"{const} has DRIFTED between the repos: app has {m.group(1)!r}, "
            f"island has {ours!r}. One of the two halves stopped working and "
            f"neither would have logged anything.")


@pytest.mark.parametrize("body,expected", [
    (push_service.CALL_INVITE_BODY, "CALL_INVITE"),
    (push_service.CALL_END_BODY, "CALL_END"),
    ("aiko:call/1 \u00b7 \U0001f4de started a call and then some", None),
    ("aiko:call/1 \u00b7 \U0001f4de ended the call, honest", None),
    ("hello", None),
])
def test_should_wake_maps_each_sentinel_to_its_own_kind(body, expected):
    """The gate is the ONLY supply of a `WakeKind`, so this is where a hangup
    stops being indistinguishable from a ring.

    THE TWO TRAILING-CONTENT ROWS ARE THE POINT, not padding. Both sentinels are
    matched by EXACT equality; a `startswith` would hand any sender a VoIP wake
    primitive with arbitrary content after it, and the end sentinel is not
    exempt just because forging a stop is the less dangerous direction — it still
    spends the recipient's wake budget.
    """
    got = push_service.should_wake("dm", body)
    assert (got.name if got is not None else None) == expected


def test_the_end_sentinel_does_not_wake_outside_a_dm():
    """The DM check is hoisted ABOVE both sentinels rather than repeated inside
    each arm, so this is the assertion that the hoist actually covers the new
    one. A per-arm copy that forgot `channel_kind` would be a wake primitive
    aimed at every member of a public room."""
    for kind in ("standard", "llm", "robot"):
        assert push_service.should_wake(kind, push_service.CALL_END_BODY) is None


def test_channel_kind_literal_matches_the_enum():
    """`ChannelKindStr` is a hand-copied duplicate of `ChannelKind` — `Literal`
    cannot be derived from an enum at type-check time — so it is exactly the kind
    of closed set that drifts silently.

    It already did: the first draft carried a fifth member, "authenticated",
    lifted from an unrelated `kind ==` comparison elsewhere in the codebase.
    Nothing at runtime would ever have complained, because the alias is erased at
    execution and only a type-checker reads it. This assertion is the only thing
    standing between that alias and quiet nonsense.
    """
    import typing

    assert sorted(typing.get_args(push_service.ChannelKindStr)) == sorted(
        m.value for m in ChannelKind
    )


@pytest.mark.parametrize(
    "kind,body,expected",
    [
        ("dm", push_service.CALL_INVITE_BODY, push_service.WakeKind.CALL_INVITE),
        # A prefix match would hand an attacker a wake primitive with arbitrary
        # trailing content — the app's `isCallInviteBody` is exact for the same reason.
        ("dm", push_service.CALL_INVITE_BODY + " and now you ring", None),
        ("dm", "look: " + push_service.CALL_INVITE_BODY, None),
        ("dm", "hello", None),
        # Video is DM-only, so a call invitation in a public room is not a call.
        ("public", push_service.CALL_INVITE_BODY, None),
        ("private", push_service.CALL_INVITE_BODY, None),
        ("dm", "", None),
    ],
)
def test_should_wake_returns_the_wake_kind(kind, body, expected):
    """The gate now returns WHAT KIND OF WAKE this is, not merely whether to
    wake — and the router accepts a VoIP delivery only from a `WakeKind` it was
    handed, whose only supply is this predicate.

    That turns "every push this module can emit is a call invite" from a true
    sentence about two functions four hundred lines apart into a data-flow fact.
    When design 12 Decision 5's cancel wake lands, adding `WakeKind.CALL_END`
    makes the router's match non-exhaustive — which is exactly the moment
    somebody must DECIDE whether a cancel rings, instead of a non-call silently
    inheriting a VoIP push whose penalty is invisible to `SendResult` forever.
    """
    assert push_service.should_wake(kind, body) is expected


def test_wake_kind_is_compared_by_identity_not_truthiness():
    """Callers must use `is None`, never `not wake`. The one member is truthy
    today, so a falsy-valued member added later would silently turn the gate
    off — the failure mode being an island that stops ringing with no error."""
    import inspect
    for fn in (push_service.wake_for_message, push_service.schedule_wake):
        source = inspect.getsource(fn)
        assert "not should_wake" not in source, (
            f"{fn.__name__} tests the WakeKind for truthiness")


# --------------------------------------------------------------------------
# Configured / unconfigured — a PAIR, so "sent nothing" cannot be vacuous.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_configured_island_wakes_the_peer(session, dm, configured, fake_apns):
    """POSITIVE CONTROL for the test below. If this ever stops sending, the
    'unconfigured sends nothing' assertion becomes meaningless — it would pass
    for a service that can never send at all."""
    alice, bob = dm
    await _wake(sender_id=alice.id)
    assert len(fake_apns.sent) == 1
    device_token, payload, collapse_id = fake_apns.sent[0]
    assert device_token == "b" * 64
    assert collapse_id == CHANNEL


@pytest.mark.asyncio
async def test_unconfigured_island_sends_nothing(session, dm, fake_apns, monkeypatch):
    """No credentials → push is simply off, and the island runs normally. An
    operator can stand up an island without an Apple developer account."""
    monkeypatch.setattr(settings, "apns_key_id", "", raising=False)
    monkeypatch.setattr(settings, "apns_private_key", "", raising=False)
    alice, bob = dm
    await _wake(sender_id=alice.id)
    assert fake_apns.sent == []


# --------------------------------------------------------------------------
# The payload's opacity is a security property, so it is asserted, not assumed.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_payload_never_names_the_caller(session, dm, configured, fake_apns):
    """APNs can read everything we send it. The wake says that SOMETHING is
    waiting and where to go, never who is calling — so Apple learns timing and
    frequency, not the social graph. A future 'improvement' that puts the
    caller's display name in the alert would be a design change, and this test is
    what makes it a deliberate one."""
    alice, bob = dm
    await _wake(sender_id=alice.id)
    _, payload, _ = fake_apns.sent[0]
    flat = repr(payload)
    assert "alice" not in flat.lower()
    assert "Alice" not in flat
    assert alice.id not in flat
    # The channel id IS present — it is what makes the tap land in the right
    # conversation, and it is the one identifier we accept leaking.
    assert payload.channel_id == CHANNEL
    # THE DOCTRINE IS STRUCTURAL, not merely asserted — and it is pinned as a
    # CLOSED SET of fields rather than a COUNT of them. It used to read `==
    # ["channel_id"]` with a comment reasoning from "ONE field, so there is
    # nowhere to put a name". The count was never the property: when the end wake
    # needed a second field (`kind`, claude-tasks#4254), that assertion failed for
    # a change that adds no identity at all, while an attacker-shaped change —
    # renaming `channel_id` to `caller_name` — would have kept the count at one
    # and passed.
    #
    # So the guard is now "these exact fields, and nothing else". A new field
    # still fails it, which is the whole point: adding one must be a deliberate
    # act with this test's docstring read, not a quiet append.
    assert [f.name for f in dataclasses.fields(payload)] == ["channel_id", "kind"]
    # AND `kind` CANNOT CARRY AN IDENTITY, which is why its arrival does not
    # weaken this test. It ranges over a closed enum defined in this repo — there
    # is no free-form string in it for a display name to hide in, and a
    # `WakeKind("alice")` is a ValueError at construction.
    assert payload.kind in set(push_service.WakeKind)


# --------------------------------------------------------------------------
# The end sentinel — design 12 Decision 5, claude-tasks#4254.
# The ring that can be started must be stoppable on a LOCKED handset, which is
# the one place the live socket cannot reach.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_end_wake_reaches_the_voip_row_and_skips_the_alert_row(
    session, dm, configured, fake_apns, caplog, end_wake_gate_open
):
    """THE WHOLE PIECE, end to end: bob holds both an alert row (from the
    fixture) and a voip row, alice hangs up, and exactly one push goes out.

    THE SKIP IS AS LOAD-BEARING AS THE SEND. An alert push runs no app code, so
    it cannot end a CallKit ring — and what it WOULD do is render the invite's
    "Incoming call / Tap to join" banner for a call that just ended. So the alert
    row is not an incidental non-delivery, it is a decision, and it must name
    itself in the log or it is indistinguishable from the delivery bug this
    module has a standing rule against.
    """
    alice, bob = dm
    session.add(DeviceToken(user_id=bob.id, platform="apns", token="v" * 64,
                            token_kind=TokenKind.VOIP.value))
    await session.commit()

    with caplog.at_level(logging.INFO, logger="aiko_gateway.push"):
        await _wake(push_service.CALL_END_BODY, sender_id=alice.id)

    assert fake_apns.kinds == [TokenKind.VOIP], (
        "the hangup must reach the VoIP row and ONLY the VoIP row; "
        f"got {fake_apns.kinds}")
    assert fake_apns.sent[0][0] == "v" * 64
    assert any("reason=end_wake_needs_voip" in r.message for r in caplog.records), (
        f"the alert row's skip must name itself. Log: {caplog.text}")


@pytest.mark.asyncio
async def test_an_end_wake_carries_the_end_kind_in_the_payload(
    session, dm, configured, fake_apns, end_wake_gate_open
):
    """THE FIELD THE RING'S STOPPABILITY RESTS ON.

    Every PushKit push must be reported to CallKit before the handler returns. A
    stop that arrives in the same shape as a start IS a start — Swift has no way
    to tell them apart and rings again, which is design 16's temper finding #7
    ("a hangup delivered as a VoIP push becomes a second ring") landing on the
    island side. `kind` is what makes the stop stoppable.

    Asserted on the POLICY object here; `test_apns_headers` asserts the rendered
    `"k"` on the wire. Two layers, because the payload being right and the
    envelope carrying it are different claims.
    """
    alice, bob = dm
    session.add(DeviceToken(user_id=bob.id, platform="apns", token="v" * 64,
                            token_kind=TokenKind.VOIP.value))
    await session.commit()

    await _wake(push_service.CALL_END_BODY, sender_id=alice.id)
    assert fake_apns.sent, "no push at all — the end wake never fired"
    _, payload, _ = fake_apns.sent[0]
    assert payload.kind is push_service.WakeKind.CALL_END

    # THE MUST-DIFFER ARM. Without it this test passes against an implementation
    # that hard-codes CALL_END, or one that ignores `kind` entirely and renders a
    # constant — both of which break the invite in exactly the way this field
    # exists to prevent.
    fake_apns.sent.clear()
    await _wake(push_service.CALL_INVITE_BODY, sender_id=alice.id)
    _, invite_payload, _ = fake_apns.sent[0]
    assert invite_payload.kind is push_service.WakeKind.CALL_INVITE


def test_the_end_wake_interlock_ships_closed():
    """THE DEFAULT IS THE SAFETY PROPERTY, so it is asserted rather than assumed.

    claude-tasks#4178 measured that `reportCall(endedAt:)` alone does not satisfy
    Apple's must-report rule, and that three unreported VoIP pushes blackhole VoIP
    delivery to that app on that device — invisibly, behind a 200. Until #4278
    says whether report-then-immediately-end counts as reported, the island must
    not route an end wake to a VoIP handset.

    Before this constant, the only thing preventing that was the ABSENCE OF ANY
    VOIP ROW plus a sentence in a ticket. This test is what makes flipping the
    default a deliberate act with a red test in front of it, rather than a line
    someone changes while doing something else.
    """
    assert push_service.END_WAKE_VOIP_GATE_OPEN is False, (
        "the end-wake interlock must ship CLOSED until claude-tasks#4278 answers "
        "whether report-then-immediately-end satisfies must-report")


@pytest.mark.asyncio
async def test_a_closed_interlock_refuses_the_end_wake_and_says_so(
    session, dm, configured, fake_apns, caplog
):
    """THE INTERLOCK, EXERCISED END-TO-END AT ITS SHIPPED SETTING — deliberately
    NOT requesting the `end_wake_gate_open` fixture.

    A REFUSAL MUST NAME ITSELF. Routing the refusal through `plan_deliveries`'s
    skip list rather than making the wake vanish upstream in `should_wake` is the
    whole design: a gate that silently produced no wake would be indistinguishable
    from a delivery bug, which is the failure mode this module has a standing rule
    against. So this asserts BOTH halves — nothing was sent, AND the log says why.
    """
    alice, bob = dm
    session.add(DeviceToken(user_id=bob.id, platform="apns", token="v" * 64,
                            token_kind=TokenKind.VOIP.value))
    await session.commit()

    with caplog.at_level(logging.INFO, logger="aiko_gateway.push"):
        await _wake(push_service.CALL_END_BODY, sender_id=alice.id)

    assert fake_apns.sent == [], (
        "the interlock is closed — an end wake must not reach a VoIP handset")
    assert any("reason=end_wake_gated_pending_4278" in r.message
               for r in caplog.records), (
        f"the refusal must name itself; a silent skip is indistinguishable from a "
        f"delivery bug. Log: {caplog.text}")


@pytest.mark.asyncio
async def test_the_conduct_gate_runs_on_an_end_wake_too(
    session, dm, configured, fake_apns, caplog
):
    """Gate 7 is called UNCONDITIONALLY rather than under `if wake is
    CALL_INVITE`, and this is the test that the unconditional call covers the
    second member — the exact silent bypass that `if` was replaced to prevent
    (Tesla, cage-match PR#173 r1).

    THE DIRECTION THAT MATTERS IS THE SAFE ONE, and it is worth stating why this
    cannot strand a ringing phone. `_spoken_here` is MONOTONIC: posting history
    only grows, so a recipient who passed the gate for the invite cannot fail it
    for the end. There is no window in which a ring is admitted and its stop is
    refused. What this refuses is an end wake for a ring that was never allowed
    to happen — a wake primitive with no call behind it.
    """
    alice, bob = dm
    # Remove bob's only message, so he has never spoken here.
    await session.execute(sa_delete(Message).where(Message.sender_user_id == bob.id))
    session.add(DeviceToken(user_id=bob.id, platform="apns", token="v" * 64,
                            token_kind=TokenKind.VOIP.value))
    await session.commit()

    with caplog.at_level(logging.INFO, logger="aiko_gateway.push"):
        await _wake(push_service.CALL_END_BODY, sender_id=alice.id)

    assert fake_apns.sent == [], (
        "an end wake bypassed the conduct gate — the gate must run on EVERY wake "
        "kind, not just the one it was written for")
    assert any("reason=no_prior_conduct" in r.message for r in caplog.records), (
        f"the skip must name itself. Log: {caplog.text}")


# --------------------------------------------------------------------------
# Reaping — the pair that protects the device table from a config slip.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_410_unregistered_reaps_the_row(session, dm, configured, fake_apns):
    """Apple making a POSITIVE claim about the device: the app is gone. Reap."""
    alice, bob = dm
    fake_apns.verdict = apns.Verdict.DEAD_TOKEN
    fake_apns.invalid_since_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    await _wake(sender_id=alice.id)
    remaining = (await session.execute(
        DeviceToken.__table__.select().where(DeviceToken.user_id == bob.id)
    )).all()
    assert remaining == []


@pytest.mark.asyncio
async def test_rejected_does_not_reap_the_row(session, dm, configured, fake_apns):
    """THE CONTROL THAT EARNS THE ONE ABOVE.

    `400 BadDeviceToken` is also what Apple returns for a perfectly valid token
    sent to the WRONG ENVIRONMENT (a development-build token against the
    production host, or the reverse). An over-eager reaper would therefore empty
    the entire table on the first ring after an `apns_use_sandbox` slip — and the
    recovery is not a config fix, it is every user reopening the app to
    re-register, which is exactly what push exists to avoid needing.

    So REJECTED must leave the row alone. Failing safe for a reaper means NOT
    deleting: destroyed device rows cannot be re-derived from anything the island
    holds.

    SCOPE, STATED HONESTLY — this test does NOT on its own protect against that
    scenario, and it reads as though it does. It injects `Verdict.REJECTED`
    through the fake, so `apns._verdict` never runs: it proves the SERVICE does
    not reap on REJECTED, not that a `400 BadDeviceToken` BECOMES REJECTED. The
    second half lives in `test_verdict_mapping_is_narrow`, and the two are only
    protective TOGETHER. Verified by mutation: making 400 reap leaves this test
    green and fails only the mapping test. Do not delete either one believing the
    other covers it.
    """
    alice, bob = dm
    fake_apns.verdict = apns.Verdict.REJECTED
    await _wake(sender_id=alice.id)
    remaining = (await session.execute(
        DeviceToken.__table__.select().where(DeviceToken.user_id == bob.id)
    )).all()
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_a_row_re_registered_during_the_send_is_not_reaped(
    session, dm, configured, monkeypatch
):
    """THE RACE, ACTUALLY CREATED — a reaper test that cannot produce the failure
    cannot clear it (cage-match #139 round 3, Carnot).

    `apns.send` is an awaited network call. Between issuing it and acting on the
    410, the device can re-register: `register_device` upserts keyed on the
    globally-unique token, so the same row can be refreshed or reassigned when a
    handset changes hands. Deleting by id alone would act on a verdict about the
    row as it WAS and destroy a registration made while we were waiting.

    The fake mutates the row MID-SEND, which is the window itself — not a
    simulation of it.

    WHY THE FAKE RETURNS A DATELESS `ReapOrder(None)`, STATED BECAUSE IT IS NOT
    APPLE'S BEHAVIOUR (Tesla, cage-match PR#172 r1). `apns._reap_order` returns
    `None` — not `ReapOrder(None)` — for a 410 carrying no timestamp, so this exact
    value is one the APNs transport can never produce; it is FCM's weaker,
    dateless permission wearing Apple's name. That is deliberate and load-bearing,
    but it was previously unstated, which is worse than either choice on its own:

      - `reap=None` would make the row survive TRIVIALLY, because nothing would
        attempt a delete at all. The assertion would pass without the guard under
        test ever executing — a test that cannot produce the failure it screens for.
      - A DATED order would let the row survive for TWO reasons at once (the triple
        mismatch AND `updated_at <= not_reregistered_since`), so a broken triple
        check would still go green.

    A dateless-but-present order is the only value that isolates the compare-and-
    delete triple, which is the guard this test exists for. Read it as a test of the
    SHARED reap path, not of Apple's verdict mapping — `test_a_410_without_a_
    timestamp_does_not_reap` is where Apple's own dateless behaviour is pinned.

    Honest residual: `ReapOrder(None)` is indistinguishable from `ReapOrder()`'s
    default, so this test cannot tell a caller that deliberately passed no date from
    one that forgot to pass a date at all. That is a real blind spot in this
    fixture, named rather than papered over.
    """
    alice, bob = dm
    row = (await session.execute(
        DeviceToken.__table__.select().where(DeviceToken.user_id == bob.id)
    )).first()
    assert row is not None, "fixture precondition: bob has a registered device"

    async def _send_then_reregister(device_token, payload, *, apns_environment,
                                    token_kind, collapse_id=None):
        # The device comes back to life while APNs is still answering.
        await session.execute(
            DeviceToken.__table__.update()
            .where(DeviceToken.token == device_token)
            .values(updated_at=dt.datetime.now(dt.UTC))
        )
        await session.commit()
        return apns.SendResult(apns.Verdict.DEAD_TOKEN, ReapOrder(None))

    monkeypatch.setattr(apns, "send", _send_then_reregister)
    await _wake(sender_id=alice.id)

    survivors = (await session.execute(
        DeviceToken.__table__.select().where(DeviceToken.user_id == bob.id)
    )).all()
    assert len(survivors) == 1, (
        "a device that re-registered during the send was reaped on a stale verdict"
    )


@pytest.mark.asyncio
async def test_a_stale_410_does_not_reap_a_newer_registration(
    session, dm, configured, fake_apns
):
    """APPLE'S OWN RULE (cage-match #139 round 4, Carnot). A 410 body carries the
    moment APNs confirmed the token invalid, and Apple says to resume pushing if
    the app has registered that token AGAIN since.

    Distinct from the mid-send race: here the row was ALREADY refreshed BEFORE we
    sent, and the 410 we get back is simply stale — a user who deleted the app and
    reinstalled it. The equality guards cannot see this; only the timestamp can.
    """
    alice, bob = dm
    # The device re-registered one hour AFTER Apple says the token died.
    await session.execute(
        DeviceToken.__table__.update()
        .where(DeviceToken.user_id == bob.id)
        .values(updated_at=dt.datetime(2026, 8, 21, 12, 0, tzinfo=dt.UTC))
    )
    await session.commit()
    fake_apns.verdict = apns.Verdict.DEAD_TOKEN
    fake_apns.invalid_since_ms = int(
        dt.datetime(2026, 8, 21, 11, 0, tzinfo=dt.UTC).timestamp() * 1000)

    await _wake(sender_id=alice.id)

    survivors = (await session.execute(
        DeviceToken.__table__.select().where(DeviceToken.user_id == bob.id)
    )).all()
    assert len(survivors) == 1, "a re-registered device was reaped on a stale 410"


@pytest.mark.asyncio
async def test_a_current_410_still_reaps(session, dm, configured, fake_apns):
    """THE CONTROL FOR THE TEST ABOVE. A guard that never reaps would satisfy the
    stale-timestamp test perfectly — so prove a 410 NEWER than the registration
    still deletes. Withholding must be conditional, not total."""
    alice, bob = dm
    await session.execute(
        DeviceToken.__table__.update()
        .where(DeviceToken.user_id == bob.id)
        .values(updated_at=dt.datetime(2026, 8, 21, 11, 0, tzinfo=dt.UTC))
    )
    await session.commit()
    fake_apns.verdict = apns.Verdict.DEAD_TOKEN
    fake_apns.invalid_since_ms = int(
        dt.datetime(2026, 8, 21, 12, 0, tzinfo=dt.UTC).timestamp() * 1000)

    await _wake(sender_id=alice.id)

    survivors = (await session.execute(
        DeviceToken.__table__.select().where(DeviceToken.user_id == bob.id)
    )).all()
    assert survivors == [], "a genuinely dead token was not reaped"


@pytest.mark.asyncio
async def test_the_service_reads_blocks_itself_not_only_from_the_caller(
    session, dm, configured, fake_apns
):
    """A door whose lock is supplied by whoever knocks is not a door (cage-match
    #139 round 4, Carnot). The block set used to arrive only as the caller's
    `exclude_user_ids`, so a second caller that forgot the argument would silently
    lose the block gate on a capability louder than a message.

    Here the caller passes NOTHING and a real block exists — the service must
    still refuse.
    """
    from aiko_gateway.domain.models import UserBlock

    alice, bob = dm
    session.add(UserBlock(blocker_user_id=bob.id, blocked_user_id=alice.id))
    await session.commit()

    await _wake(sender_id=alice.id, exclude=set())   # caller supplies no exclusion
    assert fake_apns.sent == [], "the service trusted the caller's empty block set"


@pytest.mark.asyncio
async def test_a_lying_caller_cannot_wake_a_public_room(
    session, dm, configured, fake_apns
):
    """THE DM GATE IS READ, NOT TRUSTED (cage-match #139 round 6, Carnot).

    `channel_kind` arrives as an argument. A future caller could pass "dm" beside
    a NON-DM channel_id and the sentinel, and wake every member of a public room.
    Here the caller lies exactly that way — the channel row says 'standard' — and
    the service must refuse on ground truth.
    """
    alice, bob = dm
    from aiko_gateway.domain.models import DEFAULT_COMMUNITY_ID
    await session.execute(
        Channel.__table__.update().where(Channel.id == CHANNEL)
        .values(kind=ChannelKind.STANDARD.value, aiko_channel="general",
                community_id=DEFAULT_COMMUNITY_ID)
    )
    await session.commit()
    await _wake(sender_id=alice.id, kind="dm")   # the caller insists it is a DM
    assert fake_apns.sent == [], "the service took the caller's word for the DM gate"


@pytest.mark.asyncio
async def test_a_three_member_dm_fails_closed(session, dm, configured, fake_apns):
    """DM safety rests on the room being {sender, one peer}. A malformed
    3-member kind='dm' channel would otherwise wake everyone in it — the
    unbounded-fanout case DM-only exists to prevent. Nobody flagged this; it came
    out of aligning with the video-token path's cardinality assertion."""
    alice, bob = dm
    carol = await users_service.create_user(
        session, username="carol", display_name="Carol", password="pw")
    session.add_all([
        Membership(channel_id=CHANNEL, user_id=carol.id),
        DeviceToken(user_id=carol.id, platform="apns", token="c" * 64),
    ])
    await session.commit()
    await _wake(sender_id=alice.id)
    assert fake_apns.sent == [], "a 3-member 'DM' woke its members"


@pytest.mark.asyncio
async def test_a_three_member_dm_with_a_banned_peer_still_fails_closed(
    session, dm, configured, fake_apns
):
    """CARDINALITY IS A STRUCTURAL PROPERTY, NOT A HEADCOUNT OF WHO IS SENDABLE
    (cage-match #139 round 7, Carnot).

    The previous revision counted rows that had already been ban-filtered, so a
    malformed THREE-member DM containing one banned peer counted as two, passed
    the two-party assertion, and woke the remaining peer. The channel was still
    structurally not a DM — only the sendable set happened to look like one.

    Note what this test needed that the plain 3-member test did not: a BANNED
    third member. The existing suite had both a 3-member test and a banned-peer
    test and neither could produce this state, because the bug lives in their
    INTERACTION. Feature-interaction, not a missing case.
    """
    alice, bob = dm
    carol = await users_service.create_user(
        session, username="carol", display_name="Carol", password="pw")
    session.add(Membership(channel_id=CHANNEL, user_id=carol.id))
    await session.commit()
    # Carol is banned, so the eligibility filter would remove her — leaving bob
    # alone and the channel looking two-party.
    await session.execute(
        sa.update(sa.table("users", sa.column("id"), sa.column("banned_at")))
        .where(sa.column("id") == carol.id)
        .values(banned_at=dt.datetime.now(dt.UTC))
    )
    await session.commit()

    await _wake(sender_id=alice.id)
    assert fake_apns.sent == [], (
        "a 3-member channel passed the two-party gate because one member was banned"
    )


@pytest.mark.asyncio
async def test_a_non_member_sender_cannot_wake_the_channel(
    session, dm, configured, fake_apns
):
    """THE INVARIANT IS {sender, peer}, NOT "one peer" (cage-match #139 round 8).

    Counting non-sender members and accepting exactly one never proved the SENDER
    was a member. A malformed one-member private DM containing only Bob, plus a
    caller-supplied sender_id from outside the channel, yielded exactly one
    "peer" and woke Bob for a stranger.

    Mallory is a real account with no membership row here.
    """
    alice, bob = dm
    mallory = await users_service.create_user(
        session, username="mallory", display_name="Mallory", password="pw")
    # Leave only Bob in the channel, so a non-sender count would read exactly 1.
    await session.execute(
        Membership.__table__.delete().where(Membership.c.user_id == alice.id)
        if hasattr(Membership, "c") else
        sa.delete(Membership).where(Membership.user_id == alice.id)
    )
    await session.commit()

    await _wake(sender_id=mallory.id)
    assert fake_apns.sent == [], "a non-member sender woke the channel"


@pytest.mark.asyncio
async def test_one_exploding_device_does_not_abandon_the_others(
    session, dm, configured, monkeypatch
):
    """PER-DEVICE BOUNDARY (cage-match #139 round 6, Carnot). `apns.send` can
    still raise from provider-token signing or client construction, and the only
    other catch is outside the whole loop — so one bad row would abandon every
    remaining device. Entropy localizes only where you build the boundary."""
    alice, bob = dm
    session.add(DeviceToken(user_id=bob.id, platform="apns", token="z" * 64))
    await session.commit()

    reached = []

    async def _explode_on_first(device_token, payload, *, apns_environment,
                                token_kind, collapse_id=None):
        if device_token.startswith("b"):
            raise RuntimeError("provider token signing blew up")
        reached.append(device_token)
        return apns.SendResult(apns.Verdict.DELIVERED)

    monkeypatch.setattr(apns, "send", _explode_on_first)
    await _wake(sender_id=alice.id)
    assert reached == ["z" * 64], "a raising device aborted the rest of the batch"


@pytest.mark.asyncio
async def test_a_dead_token_without_a_reap_order_is_not_deleted(
    session, dm, configured, fake_apns, caplog
):
    """NO REAP ORDER, NO REAP (cage-match #139 round 6, Carnot, generalised).

    The refusal encodes APPLE's documented resume-if-re-registered rule, so it
    belongs in `apns.py` and not in the shared reaper: FCM's UNREGISTERED carries
    no timestamp at all, and a shared rule keyed on one would build an FCM reaper
    that can never fire — a true sentence filed against the wrong owner. The
    reaper's remaining question is the one it can answer for every transport: did
    the transport that observed the death issue an order?

    Its control is `test_a_current_410_still_reaps`, which DOES supply one.
    """
    alice, bob = dm
    fake_apns.verdict = apns.Verdict.DEAD_TOKEN
    fake_apns.invalid_since_ms = None
    with caplog.at_level(logging.WARNING, logger="aiko_gateway.push"):
        await _wake(sender_id=alice.id)
    survivors = (await session.execute(
        DeviceToken.__table__.select().where(DeviceToken.user_id == bob.id)
    )).all()
    assert len(survivors) == 1

    # THE ROW MUST SURVIVE BY DECISION, NOT BY ACCIDENT. Mutation-testing caught
    # this test passing for the wrong reason: with the guard removed, the code
    # reached `fromtimestamp(None)`, threw, and the broad outer `except` swallowed
    # it — the row survived because the delete never ran, which is
    # indistinguishable from the guard working if you only count survivors. So
    # assert the REASON, and assert nothing exploded.
    assert any("dead_without_reap_order" in r.message for r in caplog.records), (
        "the row survived, but not via the no-reap-order guard"
    )
    assert not any(r.exc_info for r in caplog.records), (
        "the row survived because something threw, not because the guard fired"
    )


def test_verdict_mapping_is_narrow():
    """The mapping itself, at the unit level — the reaping rule stated once.

    (The FCM sibling went with the transport — design 14 temper. `test_unregistered_is_the_only_
    reaping_verdict` and its 404-without-a-detail must-fail arm), because each
    transport's mapping is a fact about that protocol and belongs beside it. Both
    halves are the same protective pair as the 410/400 one below: deleting either
    breaks both.
    """
    assert apns._verdict(200, "") is apns.Verdict.DELIVERED
    assert apns._verdict(410, "Unregistered") is apns.Verdict.DEAD_TOKEN
    # 410 reaps on STATUS alone; the reason string is not consulted.
    assert apns._verdict(410, "") is apns.Verdict.DEAD_TOKEN
    # A 400 carrying reason "Unregistered" must NOT reap (cage-match #139,
    # Carnot). An earlier revision accepted this as belt-and-braces, which
    # contradicted the docstring one line above it and widened the only
    # state-destroying operation in the module on an undocumented, untested
    # combination. Extra arms on a guard cost a false refusal; extra arms on a
    # reaper cost a row nothing can rebuild.
    assert apns._verdict(400, "Unregistered") is apns.Verdict.REJECTED
    # Config-shaped refusals: OURS to fix, never the device's fault.
    assert apns._verdict(400, "BadDeviceToken") is apns.Verdict.REJECTED
    assert apns._verdict(400, "DeviceTokenNotForTopic") is apns.Verdict.REJECTED
    assert apns._verdict(400, "BadTopic") is apns.Verdict.REJECTED
    assert apns._verdict(403, "InvalidProviderToken") is apns.Verdict.REJECTED
    # Transient: the device is not implicated.
    assert apns._verdict(429, "TooManyRequests") is apns.Verdict.TRANSIENT
    assert apns._verdict(503, "ServiceUnavailable") is apns.Verdict.TRANSIENT


# --------------------------------------------------------------------------
# Who gets woken.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_caller_is_never_woken(session, dm, configured, fake_apns,
                                         monkeypatch):
    """Alice starting a call must not ring Alice's own phone."""
    alice, bob = dm
    session.add(DeviceToken(user_id=alice.id, platform="apns", token="a" * 64))
    await session.commit()
    await _wake(sender_id=alice.id)
    assert [t for t, _, _ in fake_apns.sent] == ["b" * 64]


@pytest.mark.asyncio
async def test_a_blocked_peer_is_excluded(session, dm, configured, fake_apns):
    """Belt-and-braces over the structural gate: a blocked pair cannot get the
    message written at all (`create_outbound` raises `BlockedDmSend`), so this
    path should never be reached with a blocked peer — but the exclusion set the
    fanout computes is applied here too, so that a future caller that skips the
    mutator still cannot ring someone who blocked them."""
    alice, bob = dm
    await _wake(sender_id=alice.id, exclude={bob.id})
    assert fake_apns.sent == []


@pytest.mark.asyncio
async def test_a_handset_with_both_kinds_receives_both_pushes(
    session, dm, configured, fake_apns, monkeypatch
):
    """ARM (B), PINNED so it cannot drift silently.

    Rows carry no device identity, so "one push per handset" is not computable
    and no selection rule can be correct. Sending to every selected row costs a
    dual-registered iPhone a redundant banner beside its CallKit ring; preferring
    voip would silently never ring an alert-only second Apple device. This module
    already made that trade in those words: a duplicate notification is a
    blemish, a missed call is the bug.

    ONE budget slot for the fanout, because the budget protects the PERSON.
    """
    alice, bob = dm
    monkeypatch.setattr(settings, "apns_wake_per_recipient_per_minute", 1,
                        raising=False)
    session.add(DeviceToken(user_id=bob.id, platform="apns", token="v" * 64,
                            token_kind=TokenKind.VOIP.value))
    await session.commit()

    await _wake(sender_id=alice.id)
    assert sorted(t for t, _, _ in fake_apns.sent) == ["b" * 64, "v" * 64]
    assert sorted(k.value for k in fake_apns.kinds) == ["alert", "voip"]


@pytest.mark.asyncio
async def test_a_voip_only_recipient_is_rung_on_the_voip_row(
    session, dm, configured, fake_apns
):
    """THE PERMANENT POPULATION, end to end (design 12 Decision 2.1).

    A PushKit VoIP token needs no user permission at all, while a handset that
    declined notifications cannot be RUNG by an alert push — so "holds voip and
    never alert" is not a half-registered edge case, it is a normal and permanent
    state. An implementation that only ever reached alert rows would leave exactly
    the people who most need a ring unreachable, silently.

    Note what this needed that the both-kinds test did not: the alert row DELETED.
    `_wake_user`'s early return used to read "no alert-sendable row" as "this user
    cannot be woken", and only a recipient with no alert row at all can show it.
    """
    alice, bob = dm
    await session.execute(DeviceToken.__table__.delete())
    session.add(DeviceToken(user_id=bob.id, platform="apns", token="v" * 64,
                            token_kind=TokenKind.VOIP.value))
    await session.commit()

    await _wake(sender_id=alice.id)
    assert [t for t, _, _ in fake_apns.sent] == ["v" * 64]
    assert fake_apns.kinds == [TokenKind.VOIP]


@pytest.mark.asyncio
async def test_a_wake_that_delivered_to_nobody_is_logged_at_error(
    session, dm, configured, fake_apns, caplog
):
    """THE ALARM NOTHING ELSE IN THE SYSTEM HAS. A recipient was selected, sends
    were attempted, and not one returned DELIVERED — today that produces only
    per-device warnings and no statement anywhere that the ring failed."""
    alice, bob = dm
    fake_apns.verdict = apns.Verdict.REJECTED
    await _wake(sender_id=alice.id)
    assert any("delivered_to=0" in r.message and r.levelname == "ERROR"
               for r in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_a_delivered_wake_raises_no_alarm(session, dm, configured, fake_apns,
                                                caplog):
    """THE CONTROL. An alarm that fires on the healthy path is the silence this
    module keeps rediscovering, wearing a high-vis vest."""
    alice, bob = dm
    with caplog.at_level(logging.ERROR, logger="aiko_gateway.push"):
        await _wake(sender_id=alice.id)
    assert not [r for r in caplog.records if r.levelname == "ERROR"], caplog.text


@pytest.mark.asyncio
async def test_wake_budget_is_per_recipient(session, dm, configured, fake_apns,
                                            monkeypatch):
    """Waking interrupts a person wherever they are, so the budget is keyed on the
    person being WOKEN — not on the sender, which a second sender would route
    around."""
    alice, bob = dm
    monkeypatch.setattr(settings, "apns_wake_per_recipient_per_minute", 3,
                        raising=False)
    for _ in range(5):
        await _wake(sender_id=alice.id)
    assert len(fake_apns.sent) == 3


@pytest.mark.asyncio
async def test_a_user_with_no_device_is_a_silent_no_op(session, dm, configured,
                                                       fake_apns):
    """The normal state for every account that has not yet run a build with push
    wired in. Not an error, and must not raise into the send path."""
    alice, bob = dm
    await session.execute(DeviceToken.__table__.delete())
    await session.commit()
    await _wake(sender_id=alice.id)
    assert fake_apns.sent == []


@pytest.mark.asyncio
async def test_a_banned_peer_is_not_woken(session, dm, configured, fake_apns):
    """Ban is an auth-INGRESS gate, so a suspended account keeps its membership
    row and would otherwise still get its handset rung. The block layer traverses
    the push path structurally (nothing can be written); the ban layer does not,
    because nothing between `create_outbound` and the wake consults it."""
    alice, bob = dm
    bob.banned_at = dt.datetime.now(dt.UTC)
    await session.commit()
    await _wake(sender_id=alice.id)
    assert fake_apns.sent == []


# --------------------------------------------------------------------------
# Lifecycle: scheduling and shutdown.
# --------------------------------------------------------------------------

def test_schedule_wake_never_raises_without_a_loop(configured):
    """`asyncio.create_task` raises RuntimeError with no running loop or a
    closing one. This is called synchronously from the WS send path, OUTSIDE any
    try/except — unguarded it could take down the message path the whole module
    swears it cannot touch (cage-match #139). Called here from a plain sync
    context, which is exactly the no-running-loop case."""
    push_service.schedule_wake(
        channel_id=CHANNEL, channel_kind="dm",
        body=push_service.CALL_INVITE_BODY,
        sender_id="someone", exclude_user_ids=set(),
    )  # must not raise


@pytest.mark.asyncio
async def test_aclose_drains_in_flight_wakes_before_the_client_closes():
    """`_in_flight` (stop the GC eating a wake) and `apns.aclose()` (stop leaking
    the connection) are each correct alone and collided: closing the shared
    client mid-send tore the connection out from under a live task, surfacing as
    a misleading "wake failed". Draining is the fix, and the ordering is the
    contract — so prove the drain actually waits."""
    finished = []

    async def _slow_wake():
        await asyncio.sleep(0.05)
        finished.append(True)

    task = asyncio.create_task(_slow_wake())
    push_service._in_flight.add(task)
    task.add_done_callback(push_service._in_flight.discard)

    await push_service.aclose(timeout=5.0)
    assert finished == [True], "aclose returned before the in-flight wake finished"
    assert not push_service._in_flight


@pytest.mark.asyncio
async def test_aclose_is_bounded_and_cancels_a_hung_wake():
    """Bounded, not unbounded: shutdown must not hang on an unreachable Apple.
    A lost wake at shutdown beats a gateway that will not stop."""
    async def _hangs_forever():
        await asyncio.sleep(3600)

    task = asyncio.create_task(_hangs_forever())
    push_service._in_flight.add(task)
    task.add_done_callback(push_service._in_flight.discard)

    await push_service.aclose(timeout=0.05)
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_a_push_failure_never_escapes(session, dm, configured, monkeypatch):
    """A failed push must not be able to fail the message send that triggered it.
    The message is the durable, authoritative thing; the push is a hint that one
    arrived."""
    alice, bob = dm

    async def _explode(*a, **kw):
        raise RuntimeError("apple is down")

    monkeypatch.setattr(apns, "send", _explode)
    await _wake(sender_id=alice.id)  # must not raise


# ------------------------------------------------- per-token APNs environment


async def test_the_send_carries_the_ROW_environment_not_the_island(
    session, dm, configured, fake_apns, monkeypatch
):
    """#3386, at the seam that matters. `test_apns_environment` proves `_host`
    maps an environment to a host; this proves the SERVICE hands it the token's
    own value rather than the island's.

    The island is pinned to sandbox and the row says production — deliberately
    opposed, so a service that quietly kept reading `settings.apns_use_sandbox`
    would still produce 'sandbox' here and fail. Both rows are woken in one
    fanout, because the real hazard is not one wrong token, it is a fanout that
    reads the environment ONCE and applies it to every device.
    """
    monkeypatch.setattr(settings, "apns_use_sandbox", True, raising=False)
    _, bob = dm
    session.add(DeviceToken(user_id=bob.id, platform="apns", token="p" * 64,
                            apns_environment="production"))
    await session.execute(
        DeviceToken.__table__.update()
        .where(DeviceToken.token == "b" * 64)
        .values(apns_environment="sandbox"))
    await session.commit()

    await _wake(sender_id=dm[0].id)

    by_token = dict(zip([t for t, _, _ in fake_apns.sent], fake_apns.environments))
    assert by_token == {"b" * 64: "sandbox", "p" * 64: "production"}, (
        "the fanout applied one environment to every device instead of reading "
        "each row's own")


# ---------------------------------------------------------------------------
# CROSS-TRANSPORT TIME ISOLATION — the half of the boundary that was only a
# comment. `_wake_user` has always claimed "Apple being down must not cost the
# Android half of a fanout, or the reverse." While the fanout was a serial `for`
# loop that was FALSE, and the test above could not see it: it inserts the FCM
# row SECOND, so Apple is reached first by rowid accident, and it asserts only
# THAT the send happened, never WHEN.
#
# The coupling was TIME, not exceptions. Both clients carry a 10s httpx timeout
# and an FCM OAuth transport failure is deliberately not negative-cached, so a
# blackholed Google cost the iPhone up to ~10s per Android row — inside the 30s
# ring lease and outside the app's admission window.
#
# BOTH arms insert the SLOW transport's row FIRST, so a regression to serial
# dispatch fails instead of passing by ordering.
# ---------------------------------------------------------------------------


async def _reinsert_apns_row_last(session, user_id: str, token: str) -> None:
    """Force the APNs row to be the LAST row for this user.

    The fanout query carries no ORDER BY, so rowid order applies. A test that
    wants Apple reached second has to say so structurally rather than hope.
    """
    await session.execute(
        sa_delete(DeviceToken).where(DeviceToken.user_id == user_id,
                                     DeviceToken.platform == "apns"))
    session.add(DeviceToken(user_id=user_id, platform="apns", token=token))
    await session.commit()


@pytest.mark.asyncio
async def test_an_unreapable_dead_token_does_not_ring_the_operator_alarm(
    session, dm, configured, fake_apns, caplog
):
    """A dead token the reaper may NOT delete must not fire the ERROR alarm on
    every wake, forever (Carnot, cage-match PR#172 r1).

    THE DEFECT. `DEAD_TOKEN` with no reap order is the reaper DELIBERATELY
    withholding authority — an APNs 410 carrying no timestamp. The row is retained
    ON PURPOSE, so it answers identically on every future wake, so `delivered`
    never leaves zero and the ERROR fires forever for a state the system is
    correctly holding. That is the warning-nobody-reads this module's own
    `warn_if_unreachable` note argues against, manufactured by the one alarm meant
    to be worth trusting.

    WHY THIS TEST CAN FAIL. Two arms, and the second is the one that matters:
    asserting the WARNING is present would still pass if the fix had merely ADDED a
    line beside the ERROR — leaving the false alarm exactly where it was. So this
    also asserts NO ERROR record exists. Reverting the fix reddens the second
    assertion, which is the whole point of the change.

    NOT a severity downgrade: it is a different FACT. "The ring failed and I do not
    know why" is an emergency; "every device I can reach is dead and I am not
    permitted to reap it" is a cleanup backlog. The REJECTED case above still
    ERRORs, which is the control proving the alarm was not simply silenced.
    """
    alice, _bob = dm
    fake_apns.verdict = apns.Verdict.DEAD_TOKEN
    fake_apns.invalid_since_ms = None   # Apple sent no timestamp => no reap order

    with caplog.at_level(logging.INFO, logger="aiko_gateway.push"):
        await _wake(sender_id=alice.id)

    assert any("reason=dead_unreapable" in r.message and r.levelname == "WARNING"
               for r in caplog.records), (
        "an unreapable dead token must name itself, so an operator can tell a "
        f"cleanup backlog from a ring outage. Got: {caplog.text}")

    assert not [r for r in caplog.records
                if "delivered_to=0" in r.message and r.levelname == "ERROR"], (
        "THE ARM THAT DISCRIMINATES: the ERROR alarm must be REPLACED, not "
        "accompanied. A fix that logs the warning and still errors leaves the "
        f"forever-alarm exactly where it was. Got: {caplog.text}")


@pytest.mark.asyncio
async def test_a_first_contact_call_invite_does_not_wake(
    session, dm, configured, fake_apns, caplog
):
    """THE MUST-FAIL ARM for the stranger gate (claude-tasks#4216).

    Before this gate, any authenticated user could open a DM with any user id, send
    the call sentinel, and ring a locked phone full-screen through silent mode.
    Blocks and bans were the only person-level gates and both are opt-OUT — they
    need the recipient to have already acted against someone they may never have
    heard of.

    THE ARM MODELS THE WORLD PRODUCTION ACTUALLY PRODUCES (Tesla, cage-match
    PR#173 r1), which the first version did not. It deleted EVERY message and woke
    against an EMPTY channel — a state `ws.py` can never create, because a wake is
    scheduled only after `create_outbound` has already written the caller's own
    sentinel row. So real first contact has exactly ONE message in the channel: the
    stranger's invite.

    Why that mattered rather than being pedantry: a WEAKER predicate — "the channel
    has any row", or "anyone has spoken here, caller included" — passes the empty
    version (nothing to see, silence) and RINGS in production (Alice's invite row is
    right there). The test would have been green while the gate leaked. So the
    caller's message is seeded and bob stays mute, which is the shape of the attack.

    Deleting the whole gate is a different probe and does not cover this: it catches
    "no gate", not "a gate that reads the wrong row."
    """
    alice, bob = dm
    await session.execute(Message.__table__.delete())
    session.add_all([
        # The caller's own sentinel — production always has this by the time the
        # wake is scheduled, so a "the channel has any row" predicate must fail.
        Message(id="01ALICEINVITE00000000000", channel_id=CHANNEL,
                sender_user_id=alice.id, sender_kind="user",
                body=push_service.CALL_INVITE_BODY),
        # AND Bob has spoken SOMEWHERE ELSE (Tesla, cage-match PR#173 r2). The
        # predicate is TWO conjuncts — this channel AND this recipient — and the
        # previous arm could only falsify one of them. With Bob mute everywhere,
        # a weaker query that DROPPED `channel_id` (`sender_user_id IN (:ids)`)
        # refused in the suite and ADMITTED in production, where Bob has posted in
        # #general: a stranger opens a fresh DM and rings a locked phone. That is
        # not "any row in this channel", it is "any row on the island" — which is
        # nearly every live user, i.e. no gate at all. This row is what makes the
        # channel conjunct falsifiable.
        # A REAL channel row, not an orphan message (Carnot, cage-match PR#173 r3).
        # Prod runs SQLite with FK OFF (ISL-0002), so a message pointing at a
        # non-existent channel inserts happily — and would have modelled a state
        # production cannot produce, proving the predicate against a row that could
        # not exist rather than against Bob genuinely having spoken elsewhere.
        #
        # Making it real immediately surfaced a schema invariant the orphan was
        # hiding: `ck_channels_community_required` refuses a non-DM channel with a
        # NULL community. So this is a second DM (community-less by design), which
        # also models the attack better — Bob talks to Carol, and a stranger tries
        # to ring him off the back of it.
        Channel(id=OTHER_CHANNEL, name="bob-carol", kind=ChannelKind.DM.value,
                aiko_channel="dm:bob-carol", is_private=True,
                community_id=sa.null()),
        Membership(channel_id=OTHER_CHANNEL, user_id=bob.id),
        Message(id="01BOBSPOKEELSEWHERE00000", channel_id=OTHER_CHANNEL,
                sender_user_id=bob.id, sender_kind="user", body="hi from #general"),
    ])
    await session.commit()

    with caplog.at_level(logging.INFO, logger="aiko_gateway.push"):
        await _wake(sender_id=alice.id)

    assert fake_apns.sent == [], (
        "a stranger's first message rang the callee's phone — the whole point of "
        "the gate is that this cannot happen")
    assert any("reason=no_prior_conduct" in r.message for r in caplog.records), (
        f"the skip must name itself so it is not indistinguishable from a delivery "
        f"bug. Log: {caplog.text}")


@pytest.mark.asyncio
async def test_a_call_invite_wakes_once_the_recipient_has_spoken(
    session, dm, configured, fake_apns
):
    """THE CONTROL. Same channel, same caller — bob has posted, so the ring lands.

    Without this, the test above passes just as well against a gate that refuses
    EVERY call invite, which would be a silent outage of the whole feature rather
    than a stranger gate.
    """
    alice, _bob = dm
    await _wake(sender_id=alice.id)
    assert [t for t, _, _ in fake_apns.sent] == ["b" * 64], (
        "an established conversation did not ring — the gate is refusing everyone")


@pytest.mark.asyncio
async def test_an_ordinary_message_does_not_wake_at_all_gate_or_no_gate(
    session, dm, configured, fake_apns
):
    """A CALL INVITE IS THE ONLY THING THAT WAKES A HANDSET TODAY.

    Written after a wrong assumption: I added a test asserting ordinary message
    wakes are NOT gated by prior conduct, expecting the gate to be scoped to one
    `WakeKind` among several. It failed — `should_wake` returns `CALL_INVITE` or
    `None` and nothing else, so an ordinary message never wakes anyone and there
    was no un-gated wake to exempt.

    FIRST CONTACT, NOT THE ESTABLISHED FIXTURE (Tesla, cage-match PR#173 r2). This
    ran against the `dm` fixture where Bob has already spoken, so gated or
    un-gated a future message-wake kind would send, and the natural "fix" when it
    reddened would be to assert that it DOES send — pinning nothing. With Bob mute
    here, a new wake kind that reaches a stranger's recipient reddens this, and the
    only way to make it green is to decide, explicitly, whether a stranger's
    MESSAGE may wake a locked phone.

    Honest scope: the real enforcement is the UNCONDITIONAL `_spoken_here` call in
    `wake_for_message` — this test cannot see someone re-wrapping it in an
    `if wake is WakeKind.CALL_INVITE`, the tautology this file already had to
    unlearn. That is a code-shape guarantee, not a test one, and saying so here is
    better than a docstring implying coverage the fixture does not have.
    """
    alice, bob = dm
    await session.execute(Message.__table__.delete())
    await session.commit()

    await _wake(sender_id=alice.id, body="just saying hello")
    assert fake_apns.sent == [], (
        "an ordinary message woke a handset. If that is now intended, decide "
        "explicitly whether the conduct gate applies to it — a stranger's MESSAGE "
        "waking a locked phone is the same class of harm as their call "
        "(claude-tasks#4216)")


@pytest.mark.asyncio
async def test_a_soft_deleted_message_still_counts_as_conduct(
    session, dm, configured, fake_apns
):
    """The docstring claims soft-delete is not withdrawal. PIN IT (Carnot, r1).

    Carnot's concern was exact: that claim depends on the soft-delete
    implementation preserving `sender_user_id`, and if deletion ever anonymises the
    sender the claim silently stops being true — a guarantee living in prose, which
    is the failure mode this repo keeps finding. So it becomes a test.

    Checked while writing this: `Message.deleted_at` is a separate column and the
    moderation/soft-delete path does not touch `sender_user_id`. ACCOUNT deletion
    DOES null it (`accounts_service.py:185`), and that is fine — a deleted account
    has no devices to ring, so conduct vanishing with it fails CLOSED.

    Mutation-proven: making soft-delete also null `sender_user_id` — the exact
    drift Carnot said the prose would not survive — reddens this test.
    """
    alice, bob = dm
    await session.execute(
        Message.__table__.update().values(deleted_at=dt.datetime.now(dt.UTC)))
    await session.commit()

    await _wake(sender_id=alice.id)
    assert [t for t, _, _ in fake_apns.sent] == ["b" * 64], (
        "deleting your own message retracted your conduct. That would invent a "
        "revocation channel this design deliberately does not have — and an "
        "all-or-nothing, invisible one at that. Withdrawal is what the friends "
        "edge is for (claude-tasks#2792)")


@pytest.mark.asyncio
async def test_the_predicate_filters_a_mixed_recipient_list(session, dm):
    """Mixed recipients: one has spoken, one has not (Carnot, r1).

    UNREACHABLE ON TODAY'S WAKE PATH, and saying so is the point rather than
    skipping the test. `should_wake` fires only for `ChannelKind.DM`, and
    `_recipients` asserts a DM has exactly one peer — so the live recipient list is
    always length 1 and a mixed list cannot occur.

    The FUNCTION still takes a list, so it is tested as a function. The day a
    non-DM wake kind exists, this is the arm that already says what the predicate
    must do — rather than that behaviour being discovered by whoever adds it.
    """
    _alice, bob = dm
    silent = await users_service.create_user(
        session, username="carol", display_name="Carol", password="pw")
    kept = await push_service._spoken_here(
        session, channel_id=CHANNEL, user_ids=[bob.id, silent.id])
    assert kept == [bob.id], (
        f"the predicate must keep only the recipient who has posted here; "
        f"got {kept}")
