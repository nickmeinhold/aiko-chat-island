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
import logging
import datetime as dt

import pytest
import pytest_asyncio

from aiko_gateway.config import settings
from aiko_gateway.domain import apns, fcm, push_service, users_service
import sqlalchemy as sa

from aiko_gateway.domain.models import (
    ApnsEnvironment, Channel, ChannelKind, DeviceToken, Membership, TokenKind,
)
from aiko_gateway.domain.push_result import ReapOrder

CHANNEL = "01JDMCHANNELDM000000000000"

# A synthetic FCM service-account blob. No key material: every FCM test here
# replaces `fcm.send` wholesale, so nothing ever signs anything.
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
        # MIRRORS `apns._reap_order` EXACTLY, including its refusal: a DEAD_TOKEN
        # with no timestamp issues NO ORDER AT ALL, because for Apple the
        # timestamp is the only evidence separating "dead" from "was dead before
        # the reinstall". A fake that issued a dateless order here would be
        # modelling FCM's rule while wearing Apple's name, and the no-reap-order
        # test would pass against a transport that reaps on weaker evidence than
        # Apple's protocol supports.
        reap = None
        if (self.verdict is apns.Verdict.DEAD_TOKEN
                and self.invalid_since_ms is not None):
            reap = ReapOrder(dt.datetime.fromtimestamp(
                self.invalid_since_ms / 1000, tz=dt.UTC))
        return apns.SendResult(self.verdict, reap)


class FakeFcm:
    """The Android sibling of `FakeApns`, at the same layer and with the same
    rule: it returns the REAL module's result type, so a divergence between the
    two transports' contracts fails the suite rather than hiding in a union.

    Its signature is deliberately NARROWER than FakeApns's — no `token_kind`, no
    environment. FCM has one registry and one endpoint per project, and a
    parameter the transport cannot honour is worse than its absence.
    """

    def __init__(self, verdict=None):
        from aiko_gateway.domain.push_result import Verdict
        self.verdict = verdict if verdict is not None else Verdict.DELIVERED
        self.reap: ReapOrder | None = None
        self.sent: list[tuple[str, object, str | None]] = []

    async def __call__(self, device_token, payload, *, collapse_key=None):
        from aiko_gateway.domain.push_result import SendResult, Verdict
        self.sent.append((device_token, payload, collapse_key))
        reap = self.reap
        if reap is None and self.verdict is Verdict.DEAD_TOKEN:
            # FCM's UNREGISTERED carries NO timestamp — the shared abstraction
            # has a genuinely weaker arm on this side, and the fake must not
            # invent evidence the protocol cannot supply.
            reap = ReapOrder(None)
        return SendResult(self.verdict, reap)


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
    monkeypatch.setattr(settings, "fcm_service_account_json", "", raising=False)
    apns.reset_for_tests()
    yield
    apns.reset_for_tests()


@pytest.fixture
def fcm_configured(monkeypatch):
    """An island WITH working FCM credentials. Composable with `configured` or
    used alone — an FCM-only island is a legitimate, bootable deployment, which
    is the whole of the gate-1 regression below."""
    monkeypatch.setattr(settings, "fcm_service_account_json", FCM_CREDENTIAL,
                        raising=False)
    fcm.reset_for_tests()
    yield
    fcm.reset_for_tests()


@pytest.fixture
def apns_unconfigured(monkeypatch):
    for k in ("apns_key_id", "apns_team_id", "apns_topic", "apns_private_key"):
        monkeypatch.setattr(settings, k, "", raising=False)


@pytest.fixture
def fake_apns(monkeypatch):
    fake = FakeApns()
    monkeypatch.setattr(apns, "send", fake)
    return fake


@pytest.fixture
def fake_fcm(monkeypatch):
    fake = FakeFcm()
    monkeypatch.setattr(fcm, "send", fake)
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
    # THE DOCTRINE IS NOW STRUCTURAL, not merely asserted. `WakePayload` has ONE
    # field, so there is nowhere for a future "improvement" to put a caller's
    # name — it would have to change the type, which is the difference between a
    # commitment and a comment. The per-transport envelopes are rendered BELOW the
    # boundary (`apns._render`, `fcm.build_message`) from exactly this.
    assert [f.name for f in dataclasses.fields(payload)] == ["channel_id"]


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


@pytest.mark.asyncio
async def test_an_fcm_dead_token_is_reaped_on_the_compare_and_delete_alone(
    session, dm, configured, fcm_configured, fake_fcm
):
    """FCM REAPS ON `UNREGISTERED` ONLY, WITH NO DATE ARM — and the reversibility
    rests entirely on the observation triple.

    `(id, token, updated_at-observed-at-send-time)` is itself a compare-and-swap:
    `register_device` sets `updated_at` explicitly on every reassign, so a device
    re-registering between our send and our reap fails the equality and survives.
    Its mirror arm is `test_an_fcm_row_re_registered_during_the_send_is_not_reaped`.
    """
    from aiko_gateway.domain.push_result import Verdict

    alice, bob = dm
    await session.execute(DeviceToken.__table__.delete())
    session.add(DeviceToken(user_id=bob.id, platform="fcm", token="f" * 100))
    await session.commit()
    fake_fcm.verdict = Verdict.DEAD_TOKEN

    await _wake(sender_id=alice.id)
    remaining = (await session.execute(
        DeviceToken.__table__.select().where(DeviceToken.user_id == bob.id)
    )).all()
    assert remaining == []


@pytest.mark.asyncio
async def test_an_fcm_row_re_registered_during_the_send_is_not_reaped(
    session, dm, configured, fcm_configured, monkeypatch, caplog
):
    """THE ARM THAT CARRIES THE WHOLE FCM REAPER, because there is no date arm to
    fall back on. The fake mutates the row MID-SEND — the window itself, not a
    simulation of it.

    Verify by mutation: removing the `updated_at` equality from the conditional
    DELETE turns this red, which is what makes it the proof rather than a
    reassurance.
    """
    from aiko_gateway.domain.push_result import ReapOrder, SendResult, Verdict

    alice, bob = dm
    await session.execute(DeviceToken.__table__.delete())
    session.add(DeviceToken(user_id=bob.id, platform="fcm", token="f" * 100))
    await session.commit()

    async def _send_then_reregister(device_token, payload, *, collapse_key=None):
        await session.execute(
            DeviceToken.__table__.update()
            .where(DeviceToken.token == device_token)
            .values(updated_at=dt.datetime.now(dt.UTC)))
        await session.commit()
        return SendResult(Verdict.DEAD_TOKEN, ReapOrder(None))

    monkeypatch.setattr(fcm, "send", _send_then_reregister)
    with caplog.at_level(logging.WARNING, logger="aiko_gateway.push"):
        await _wake(sender_id=alice.id)

    survivors = (await session.execute(
        DeviceToken.__table__.select().where(DeviceToken.user_id == bob.id)
    )).all()
    assert len(survivors) == 1, (
        "an Android device that re-registered during the send was reaped")
    assert any("row_changed_since_send" in r.message for r in caplog.records), (
        "the row survived, but the tripwire that would reveal a wrong assumption "
        "about FCM token re-issue never fired")


def test_verdict_mapping_is_narrow():
    """The mapping itself, at the unit level — the reaping rule stated once.

    Its FCM sibling lives in `test_fcm.py` (`test_unregistered_is_the_only_
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
async def test_an_android_row_goes_to_fcm_and_never_to_apple(
    session, dm, configured, fcm_configured, fake_apns, fake_fcm
):
    """RE-AUTHORED from `test_android_row_is_skipped_not_sent_to_apple`.

    The skip half INVERTS — FCM is built now, so an Android row must be sent, not
    logged and dropped. The never-handed-to-Apple half is the SECURITY property
    and survives verbatim: an FCM token against APNs is a guaranteed rejection
    and (before the narrow reaping rule) was a candidate for deletion.
    """
    alice, bob = dm
    session.add(DeviceToken(user_id=bob.id, platform="fcm", token="f" * 100))
    await session.commit()
    await _wake(sender_id=alice.id)
    assert [t for t, _, _ in fake_apns.sent] == ["b" * 64]
    assert [t for t, _, _ in fake_fcm.sent] == ["f" * 100]


@pytest.mark.asyncio
async def test_an_fcm_only_island_still_wakes_its_android_devices(
    session, dm, apns_unconfigured, fcm_configured, fake_apns, fake_fcm
):
    """THE GATE-1 REGRESSION TEST, and the highest-leverage assertion in the change.

    `apns.is_configured()` used to guard `schedule_wake` ITSELF, so on an island
    with FCM credentials and no APNs credentials the function returned before any
    task was created — gates 2-8 never ran and no Android handset was ever woken,
    however correct the FCM transport was. `config.py`'s all-or-none guard makes
    an APNs-less island a legitimate, bootable, supported deployment.

    NOTHING IN THE SUITE COULD HAVE CAUGHT IT: the `configured` fixture always
    set all four APNs settings, so the FCM-only island was a state no test could
    reach.
    """
    alice, bob = dm
    await session.execute(DeviceToken.__table__.delete())
    session.add(DeviceToken(user_id=bob.id, platform="fcm", token="f" * 100))
    await session.commit()

    await _wake(sender_id=alice.id)
    assert [t for t, _, _ in fake_fcm.sent] == ["f" * 100]
    assert fake_apns.sent == [], "an APNs-less island tried to reach Apple"


@pytest.mark.asyncio
async def test_an_island_with_no_transport_at_all_sends_nothing(
    session, dm, apns_unconfigured, fake_apns, fake_fcm, monkeypatch
):
    """THE CONTROL FOR THE TEST ABOVE. "Any transport configured" must not
    degrade into "always configured" — an island with neither credential set is
    the normal state for most deployments and must still be silent and total."""
    monkeypatch.setattr(settings, "fcm_service_account_json", "", raising=False)
    alice, bob = dm
    session.add(DeviceToken(user_id=bob.id, platform="fcm", token="f" * 100))
    await session.commit()
    await _wake(sender_id=alice.id)
    assert fake_apns.sent == [] and fake_fcm.sent == []


@pytest.mark.asyncio
async def test_an_apns_failure_does_not_suppress_the_fcm_send(
    session, dm, configured, fcm_configured, fake_fcm, monkeypatch
):
    """The per-device boundary, now ACROSS transports. One raising transport must
    not abandon the other — entropy localizes only where you build the boundary."""
    alice, bob = dm
    session.add(DeviceToken(user_id=bob.id, platform="fcm", token="f" * 100))
    await session.commit()

    async def _explode(*a, **kw):
        raise RuntimeError("apple is down")

    monkeypatch.setattr(apns, "send", _explode)
    await _wake(sender_id=alice.id)
    assert [t for t, _, _ in fake_fcm.sent] == ["f" * 100]


@pytest.mark.asyncio
async def test_an_fcm_failure_does_not_suppress_the_apns_send(
    session, dm, configured, fcm_configured, fake_apns, monkeypatch
):
    """THE MIRROR. A boundary tested in one direction only is half a boundary."""
    alice, bob = dm
    session.add(DeviceToken(user_id=bob.id, platform="fcm", token="f" * 100))
    await session.commit()

    async def _explode(*a, **kw):
        raise RuntimeError("google is down")

    monkeypatch.setattr(fcm, "send", _explode)
    await _wake(sender_id=alice.id)
    assert [t for t, _, _ in fake_apns.sent] == ["b" * 64]


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
    with caplog.at_level(logging.ERROR, logger="aiko_gateway.push"):
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
async def test_a_recipient_whose_only_transport_is_unconfigured_burns_no_budget(
    session, dm, configured, fake_apns, fake_fcm, monkeypatch
):
    """RE-AUTHORED from `test_an_fcm_only_recipient_does_not_burn_the_apns_budget`,
    which is guaranteed red the moment FCM sends — its MECHANISM inverted, its
    PROPERTY did not, and the property was a cage-match finding.

    A ROW IS NOT A SENDABLE ROW (cage-match #139 round 2, Carnot). Round 1
    charged the budget once the recipient was known to have *a device*, so a
    recipient holding only an unsendable token burned a wake slot on every call —
    and an iPhone registered later in the same minute could find its first real
    wake already throttled.

    The LIVE instance of "a row that is not sendable" is now a row whose
    transport is UNCONFIGURED on this island, which is the honest successor: it
    is the state both live boxes are in for their Android rows today.

    The arm that makes this meaningful is the second half: after N+1 unsendable
    calls, a freshly registered iPhone must STILL be wakeable.
    """
    alice, bob = dm
    monkeypatch.setattr(settings, "apns_wake_per_recipient_per_minute", 2,
                        raising=False)
    monkeypatch.setattr(settings, "fcm_service_account_json", "", raising=False)
    await session.execute(DeviceToken.__table__.delete())
    session.add(DeviceToken(user_id=bob.id, platform="fcm", token="f" * 100))
    await session.commit()

    for _ in range(5):            # would exhaust a 2/min budget if charged
        await _wake(sender_id=alice.id)
    assert fake_apns.sent == [] and fake_fcm.sent == []

    session.add(DeviceToken(user_id=bob.id, platform="apns", token="b" * 64))
    await session.commit()
    await _wake(sender_id=alice.id)
    assert len(fake_apns.sent) == 1, "the new iPhone was throttled by unsendable rows"


@pytest.mark.asyncio
async def test_an_fcm_only_recipient_on_a_configured_island_burns_exactly_one_slot(
    session, dm, configured, fcm_configured, fake_apns, fake_fcm, monkeypatch
):
    """THE INVERSE ARM THE RE-AUTHORING OWES. FCM sends now, so it now CHARGES —
    once per fanout, out of the SAME per-person budget.

    One budget, one key, one charge. A second `"fcm_wake"` bucket would hand a
    peer holding both an iPhone and an Android twice the ring budget, which
    contradicts this module's own doctrine that the budget protects the person
    being interrupted rather than the transport.
    """
    alice, bob = dm
    monkeypatch.setattr(settings, "apns_wake_per_recipient_per_minute", 2,
                        raising=False)
    await session.execute(DeviceToken.__table__.delete())
    session.add(DeviceToken(user_id=bob.id, platform="fcm", token="f" * 100))
    await session.commit()

    for _ in range(5):
        await _wake(sender_id=alice.id)
    assert len(fake_fcm.sent) == 2, "the FCM fanout ignored the per-person budget"
    assert fake_apns.sent == []


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
async def test_a_stalled_fcm_send_does_not_delay_the_apns_ring(
    session, dm, configured, fcm_configured, monkeypatch
):
    """RED-PROVEN against the serial loop: Apple was reached at 1.01s."""
    alice, bob = dm
    session.add(DeviceToken(user_id=bob.id, platform="fcm", token="f" * 100))
    await session.commit()
    await _reinsert_apns_row_last(session, bob.id, "b" * 64)

    reached: dict[str, float] = {}
    t0 = time.monotonic()

    async def _stalled_fcm(*a, **kw):
        await asyncio.sleep(1.0)
        raise RuntimeError("google is blackholed")

    async def _timed_apns(*a, **kw):
        reached["at"] = time.monotonic() - t0
        return SendResult(verdict=Verdict.DELIVERED)

    monkeypatch.setattr(fcm, "send", _stalled_fcm)
    monkeypatch.setattr(apns, "send", _timed_apns)
    await _wake(sender_id=alice.id)

    assert "at" in reached, "the APNs send never happened at all"
    assert reached["at"] < 0.2, (
        f"the APNs ring waited {reached['at']:.2f}s on the stalled FCM send — "
        "the fanout has regressed to serial dispatch. A ring that arrives after "
        "the ring is over is a missed call, not a degraded one."
    )


@pytest.mark.asyncio
async def test_a_stalled_apns_send_does_not_delay_the_fcm_wake(
    session, dm, configured, fcm_configured, fake_fcm, monkeypatch
):
    """THE MIRROR. A boundary tested in one direction only is half a boundary —
    the same sentence the exception-direction test above was written under."""
    alice, bob = dm
    # bob's APNs row already exists and is FIRST; the FCM row goes in after it,
    # so the stalled transport is reached first without any reordering.
    session.add(DeviceToken(user_id=bob.id, platform="fcm", token="f" * 100))
    await session.commit()

    reached: dict[str, float] = {}
    t0 = time.monotonic()

    async def _stalled_apns(*a, **kw):
        await asyncio.sleep(1.0)
        raise RuntimeError("apple is blackholed")

    async def _timed_fcm(*a, **kw):
        reached["at"] = time.monotonic() - t0
        return SendResult(verdict=Verdict.DELIVERED)

    monkeypatch.setattr(apns, "send", _stalled_apns)
    monkeypatch.setattr(fcm, "send", _timed_fcm)
    await _wake(sender_id=alice.id)

    assert "at" in reached, "the FCM send never happened at all"
    assert reached["at"] < 0.2, (
        f"the FCM wake waited {reached['at']:.2f}s on the stalled APNs send"
    )
