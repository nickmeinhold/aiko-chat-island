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
import logging
import datetime as dt

import pytest
import pytest_asyncio

from aiko_gateway.config import settings
from aiko_gateway.domain import apns, push_service, users_service
import sqlalchemy as sa

from aiko_gateway.domain.models import (
    Channel, ChannelKind, DeviceToken, Membership,
)

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
        # Apple's 410 timestamp, in ms. None = "Apple sent no timestamp", which
        # the reaper must treat as NO EVIDENCE rather than as the epoch.
        self.invalid_since_ms: int | None = None
        self.sent: list[tuple[str, dict, str | None]] = []

    async def __call__(self, device_token, payload, *, collapse_id=None):
        self.sent.append((device_token, payload, collapse_id))
        # Returns the SAME shape the real transport returns. A fake whose
        # contract has drifted from the real API tests a system that does not
        # exist — this one drifted once already, when send() grew SendResult, and
        # the suite caught it immediately because every test goes through here.
        return apns.SendResult(self.verdict, self.invalid_since_ms)


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

    async def _send_then_reregister(device_token, payload, *, collapse_id=None):
        # The device comes back to life while APNs is still answering.
        await session.execute(
            DeviceToken.__table__.update()
            .where(DeviceToken.token == device_token)
            .values(updated_at=dt.datetime.now(dt.UTC))
        )
        await session.commit()
        return apns.SendResult(apns.Verdict.DEAD_TOKEN)

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

    async def _explode_on_first(device_token, payload, *, collapse_id=None):
        if device_token.startswith("b"):
            raise RuntimeError("provider token signing blew up")
        reached.append(device_token)
        return apns.SendResult(apns.Verdict.DELIVERED)

    monkeypatch.setattr(apns, "send", _explode_on_first)
    await _wake(sender_id=alice.id)
    assert reached == ["z" * 64], "a raising device aborted the rest of the batch"


@pytest.mark.asyncio
async def test_a_410_without_a_timestamp_does_not_reap(
    session, dm, configured, fake_apns, caplog
):
    """NO TIMESTAMP, NO REAP (cage-match #139 round 6, Carnot). The timestamp is
    the only evidence distinguishing "dead" from "was dead before the reinstall".
    This module's posture is that failing safe for a reaper means NOT deleting —
    applied consistently, not only where it was convenient.

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
    assert any("410_without_timestamp" in r.message for r in caplog.records), (
        "the row survived, but not via the no-timestamp guard"
    )
    assert not any(r.exc_info for r in caplog.records), (
        "the row survived because something threw, not because the guard fired"
    )


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
async def test_an_fcm_only_recipient_does_not_burn_the_apns_budget(
    session, dm, configured, fake_apns, monkeypatch
):
    """A ROW IS NOT A SENDABLE ROW (cage-match #139 round 2, Carnot).

    Round 1 charged the budget once the recipient was known to have *a device*.
    A recipient holding only an Android/FCM token therefore burned an APNs wake
    slot on every call — so an iPhone registered later in the same minute could
    find its first real wake already throttled. Budget is now charged only when
    there is an APNs-sendable row.

    The arm that makes this meaningful: after N+1 FCM-only calls, a freshly
    registered iPhone must STILL be wakeable. A naive implementation throttles it.
    """
    alice, bob = dm
    monkeypatch.setattr(settings, "apns_wake_per_recipient_per_minute", 2,
                        raising=False)
    await session.execute(DeviceToken.__table__.delete())
    session.add(DeviceToken(user_id=bob.id, platform="fcm", token="f" * 100))
    await session.commit()

    for _ in range(5):            # would exhaust a 2/min budget if charged
        await _wake(sender_id=alice.id)
    assert fake_apns.sent == []   # nothing sendable, nothing sent

    session.add(DeviceToken(user_id=bob.id, platform="apns", token="b" * 64))
    await session.commit()
    await _wake(sender_id=alice.id)
    assert len(fake_apns.sent) == 1, "the new iPhone was throttled by FCM-only calls"


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
