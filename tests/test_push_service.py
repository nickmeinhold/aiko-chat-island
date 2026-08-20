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

import asyncio
import contextlib
import datetime as dt

import pytest
import pytest_asyncio

from aiko_gateway.config import settings
from aiko_gateway.domain import apns, push_service, users_service
from aiko_gateway.domain.models import DeviceToken, Membership

CHANNEL = "01JDMCHANNELDM000000000000"


class FakeApns:
    """Records every send and returns programmed verdicts.

    Deliberately NOT a subclass or a mock of the real client: the point is to
    exercise `push_service`'s policy, and a fake that shares the transport's
    implementation would be blind to bugs in the shared layer. It answers only
    the question the service asks — "what did Apple say?".
    """

    def __init__(self, verdict: apns.Verdict = apns.Verdict.DELIVERED):
        self.verdict = verdict
        self.sent: list[tuple[str, dict, str | None]] = []

    async def __call__(self, device_token, payload, *, collapse_id=None):
        self.sent.append((device_token, payload, collapse_id))
        return self.verdict


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
    apns.reset_for_tests()
    yield
    apns.reset_for_tests()


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
    """
    alice = await users_service.create_user(
        session, username="alice", display_name="Alice", password="pw")
    bob = await users_service.create_user(
        session, username="bob", display_name="Bob", password="pw")
    session.add_all([
        Membership(channel_id=CHANNEL, user_id=alice.id),
        Membership(channel_id=CHANNEL, user_id=bob.id),
        DeviceToken(user_id=bob.id, platform="apns", token="b" * 64),
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


@pytest.mark.parametrize(
    "kind,body,expected",
    [
        ("dm", push_service.CALL_INVITE_BODY, True),
        # A prefix match would hand an attacker a wake primitive with arbitrary
        # trailing content — the app's `isCallInviteBody` is exact for the same reason.
        ("dm", push_service.CALL_INVITE_BODY + " and now you ring", False),
        ("dm", "look: " + push_service.CALL_INVITE_BODY, False),
        ("dm", "hello", False),
        # Video is DM-only, so a call invitation in a public room is not a call.
        ("public", push_service.CALL_INVITE_BODY, False),
        ("private", push_service.CALL_INVITE_BODY, False),
        ("dm", "", False),
    ],
)
def test_should_wake_truth_table(kind, body, expected):
    assert push_service.should_wake(kind, body) is expected


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
    assert payload["c"] == CHANNEL


# --------------------------------------------------------------------------
# Reaping — the pair that protects the device table from a config slip.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_410_unregistered_reaps_the_row(session, dm, configured, fake_apns):
    """Apple making a POSITIVE claim about the device: the app is gone. Reap."""
    alice, bob = dm
    fake_apns.verdict = apns.Verdict.DEAD_TOKEN
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


def test_verdict_mapping_is_narrow():
    """The mapping itself, at the unit level — the reaping rule stated once."""
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
async def test_android_row_is_skipped_not_sent_to_apple(session, dm, configured,
                                                        fake_apns):
    """FCM is a separate transport behind the same door and is NOT built. An
    Android token must never be handed to APNs — it would be a guaranteed
    rejection, and (before the narrow reaping rule) a candidate for deletion."""
    alice, bob = dm
    session.add(DeviceToken(user_id=bob.id, platform="fcm", token="f" * 100))
    await session.commit()
    await _wake(sender_id=alice.id)
    assert [t for t, _, _ in fake_apns.sent] == ["b" * 64]


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
