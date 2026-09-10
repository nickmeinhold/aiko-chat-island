"""Push wake — the single door through which a handset gets woken (#3267 inc 2).

The island already reaches you when the app is open: a call invitation is an
ordinary signed message and the live WebSocket fanout delivers it. This module
exists for the case the fanout cannot serve — a CLOSED app, which holds no
socket and therefore learns nothing. Without it a call to a closed phone is
missed silently and permanently, which is the whole of claude-tasks#3253.

WHY A SINGLE DOOR. Everything security-relevant about waking a device is a
decision about WHO may cause it, and those decisions are worthless if a second
send path can skip them. So the route, any in-process caller and the tests all
enter through `schedule_wake`; `apns.py` and `fcm.py` underneath are pure
transports and hold no policy at all. There are two transports and still ONE
door: they are siblings below the boundary, never a second entrance, and neither
knows the other exists.

THE GATES, IN THE ORDER THEY ACTUALLY RUN. This list is kept exhaustive on
purpose: it listed FIVE while the code ran EIGHT, which is the same
prose-states-an-invariant-the-code-does-not defect that this review found four
separate times inside individual functions (see the notes at each site). A gate
list is the most tempting place for it, because nothing reads a list.

  0. **Downstream of an accepted message.** Structural, not a check written here
     — it is why the block and idempotency rules traverse for free. See below.
  1. **Configured — ANY transport.** No credentials for ANY transport → this
     island never pushes. Silent and total. Deliberately not "APNs configured":
     an island with FCM credentials and no APNs credentials is a legitimate,
     bootable deployment, and gating the door on one transport made every gate
     below unreachable for it.
  2. **The pinned sentinel, in a channel the CALLER calls a DM** (`should_wake`).
     Cheap, and deliberately not trusted on its own — gate 3 re-reads it.
  3. **The channel row really is a private DM**, read from the DB, because a
     caller could otherwise name a public room and wake all of it.
  4. **Exactly one peer**, counted on the RAW membership graph before any
     eligibility filter — structure first, so a 3-member "DM" cannot slip through
     by having its extra member filtered out.
  5. **The peer is not banned.** Ban is an auth-INGRESS gate; nothing between
     `create_outbound` and here consults it.
  6. **Not a blocked pair** — the caller's fanout set UNIONED with this service's
     own read, so neither is trusted alone.
  7. **The peer has a device this island can actually send to.** No sendable
     device, no budget spent. "Sendable" is a join over (platform x token_kind x
     what this wake needs) — see `plan_deliveries`.
  8. **Within the per-recipient wake budget.** Waking is louder than sending.
     ONE budget for the person, not one per transport.

THE LOUDNESS CONTRACT. Every skip and refusal names a `reason=`, and the LEVEL is
part of the contract rather than a matter of taste — this module has twice
rediscovered that silence reads as success, and once that a warning firing on
healthy boxes is the same silence in a high-vis vest.

  wake skipped device=%s reason=transport_not_configured   INFO
  wake skipped device=%s reason=unroutable_row             ERROR
  wake skipped user=%s reason=no_sendable_devices          DEBUG
  apns sent device=%s env=%s kind=%s verdict=%s            INFO
  fcm sent device=%s verdict=%s                            INFO
  wake delivered_to=0 user=%s devices=%d                   ERROR
  reap skipped device=%s reason=dead_without_reap_order    WARNING
  reap skipped device=%s reason=row_changed_since_send     WARNING

`transport_not_configured` IS INFO, NOT ERROR, and the reasoning is about what
the operator will actually see: both live boxes are APNs-configured and hold FCM
rows TODAY, so ERROR-per-row-per-wake would mean an ERROR on every ring for every
Android user until credentials land — manufacturing the very warning-nobody-reads
that `warn_if_unreachable` exists to avoid. The POPULATION signal belongs to the
boot warning and to `reachability`, which fire once.

`delivered_to=0` IS THE ALARM NOTHING ELSE HAS. A recipient was selected, sends
were attempted, and not one came back DELIVERED. Before it, a total failure to
ring produced only per-device lines and no statement anywhere that the ring
failed.

NEVER LOG A TOKEN — always the row's ULID. `apns._LONG_HEX` redacts hex runs and
can never cover a base64url FCM token; it does not need to, because FCM v1 carries
the token in the request BODY (httpx's request log cannot contain it) rather than
in the URL path the way APNs does. Stated so nobody "fixes" the gap by widening a
regex that guards nothing on that path.

GATE 0, STATED PROPERLY, BECAUSE IT IS WHY THE BLOCK RULES TRAVERSE FOR FREE.
A wake can only ever be scheduled AFTER `messages_service.create_outbound`
returned `created=True` for the triggering message. That mutator already refuses
a DM send between blocked parties (`BlockedDmSend`) and is idempotent on
`(channel, client_msg_id)`. So:

  * a blocked peer cannot wake you, because they cannot get the message written;
  * a RESEND cannot wake you twice, because a resend returns `created=False`;
  * anything the message layer refuses, the push layer refuses by construction.

That is the correct dependency direction — push is strictly downstream of an
accepted write, never a parallel capability with its own authorization story to
keep in sync. A future caller that wakes a device WITHOUT an accepted message
behind it would break this property and needs its own gate map.

KNOWN GAP, NAMED RATHER THAN ABSORBED: **the island has no per-conversation MUTE
state.** The app suppresses a ring for a muted conversation (`admitRing`'s
`conversationMuted`), but that decision happens on the handset AFTER the push has
already arrived — and you cannot un-ring a phone. So a muted DM will still wake
the device today. This is a real defect in the "waking is louder than sending"
argument, not a cosmetic one, and closing it needs mute to become island-side
state that this gate can read. Filed rather than silently accepted.

    THAT GAP CHANGED SEVERITY CLASS WHEN VoIP LANDED, AND MUST NOT CARRY ITS OLD
    SEVERITY FORWARD. Under the alert world the cost was a banner the recipient
    resents — one annoyed person. Under CallKit the same un-suppressed wake is a
    push the client MUST report to CallKit and then end, because there is no
    on-device window in which to decline (design 12 Decision 4). The island's
    stated VoIP discipline — "emits VoIP ONLY for a genuine call invite", enforced
    by `should_wake`'s exact-sentinel match — asks *is this a real invite*, never
    *may this sender ring this person*. Those are different questions and only the
    second bounds a report-and-end ratio.

    The island cannot answer the second one and BY RULING must not: ring consent is
    per-conversation and device-local (`ring_allowlist_store.dart`; Nick 2026-09-01),
    the island learns nothing, and `grep -rn friend src/` returns zero. Any
    authenticated user may open a DM with any user id and send the sentinel.

    THE COST, STATED AT ITS MEASURED STRENGTH AND NO HIGHER. Apple's
    `PKPushRegistryDelegate` documentation: failing to report terminates the app,
    and repeatedly failing "MAY cause the system to stop delivering any more VoIP
    push notifications to your app". That is PER-DEVICE DENIAL, not a fleet-wide
    revocation of a privilege — an earlier draft of this comment and of design 12a
    said the stronger thing and it was wrong (corrected 2026-09-10). It is also
    worse to operate with than the dramatic version: per-device denial accumulates
    silently on the handsets taking the MOST calls, so calling quietly stops working
    for the heaviest users with nothing surfacing anywhere.
    `CSDVoIPApplicationKillCounts` in the device-local `com.apple.TelephonyUtilities`
    domain is the ledger that makes it observable rather than inferred.

    NOT CLOSED HERE, AND NOT OURS TO CLOSE ALONE. Either the app accepts and bounds
    a non-zero report-and-end ratio and someone owns measuring it, or ring capability
    needs an island-visible signal — which collides head-on with both the
    no-broker-door ruling and the sender-anonymity ruling. That is a fork for Nick
    and the app tab, not a patch to this routing code.

A CONNECTED SOCKET DOES NOT SUPPRESS THE PUSH — a decision, not an oversight
(cage-match #139 round 2, Carnot). A recipient who is live on the WebSocket gets
BOTH the in-app ring and a push banner, and the obvious optimisation is to skip
the wake for anyone the hub currently holds a connection for. We deliberately do
not, because the two failure directions are not symmetric:

  * Suppressing on a STALE socket means a genuinely unreachable person is never
    rung — the exact failure this module exists to remove, reintroduced by an
    optimisation, and invisible because a missed call looks like no call.
  * Not suppressing on a LIVE socket means a duplicate banner.

Socket presence is also not the question being asked. It answers "is a connection
open", while the thing that matters is "is this person looking at their phone" —
and the server cannot observe that. The layer that CAN is the handset: iOS hands
a foregrounded app `userNotificationCenter(_:willPresent:)` and lets it decline to
display a banner it is already showing as a live ring. So the duplicate is
suppressible exactly where the truth lives, and is app-repo work (#3297). A
duplicate notification is a blemish; a missed call is the bug.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import enum
import logging
from collections.abc import Callable, Sequence
from typing import Literal, assert_never

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from . import apns, fcm, moderation_service
from .models import (Channel, ChannelKind, DeviceToken, Membership, Platform,
                     ApnsEnvironment, TokenKind, User)
from .push_result import ReapOrder, SendResult, Verdict, WakePayload
from .rate_limit import limiter

log = logging.getLogger("aiko_gateway.push")

# THE PINNED CALL-INVITATION SENTINEL — a WIRE CONTRACT, not a display string.
#
# The app signs this exact body and the island must recognise the exact same
# bytes; the two halves live in different repos and cannot import from each
# other, so this is a duplicated constant held in sync by a test on each side
# (here: `test_push_service.py::test_sentinel_is_pinned`, app-side:
# `call_invite_test.dart`). Copied verbatim from `aiko_chat_app`'s
# `lib/features/call/domain/call_invite.dart`.
#
# It is a ONE-WAY DOOR. The string is inside signatures already sent to both live
# islands and stored in permanent history, so it can be added to but never
# edited: changing it is a v2 with a compatibility branch. The middle character
# is U+00B7 MIDDLE DOT and the emoji is U+1F4DE — a look-alike substitution here
# would silently stop every ring, and would do so with no error anywhere.
CALL_INVITE_BODY = "aiko:call/1 · 📞 started a call"

# WHICH TRANSPORTS THIS ISLAND CAN ACTUALLY SEND ON — a CONFIG-PROBE registry, and
# nothing else.
#
# It exists because gate 1 used to be `apns.is_configured()` and guarded
# `schedule_wake` ITSELF. On an island with FCM credentials and no APNs
# credentials — which `config.py`'s all-or-none guard makes a legitimate,
# bootable, supported deployment — that returned False and no task was created at
# all, so gates 2-8 never ran and no Android handset was ever woken however
# correct the FCM transport was. The failure was invisible to the suite too,
# because the `configured` fixture always set all four APNs settings.
#
# THE REGISTRY LIVES HERE, IN THE POLICY LAYER, so neither transport learns the
# other exists. And it is a CONFIG PROBE ONLY: it must NEVER be iterated to SEND.
# Iterating a registry to dispatch would be a second door wearing a dict — sending
# goes through the single `match` on the Delivery union in `_wake_user` and
# nowhere else.
_CONFIG_PROBES: dict[Platform, Callable[[], bool]] = {
    Platform.APNS: apns.is_configured,
    Platform.FCM: fcm.is_configured,
}

# TOTALITY AT IMPORT, not at first ring. A `Platform` member added without a probe
# would otherwise be silently treated as unconfigured — every device on that
# transport skipped forever, with a reason that reads like the operator's fault.
# Boot is where an operator is watching; a wake is not.
if set(_CONFIG_PROBES) != set(Platform):
    raise RuntimeError(
        "no push config probe for platform(s): "
        f"{sorted(p.value for p in set(Platform) - set(_CONFIG_PROBES))}")


def _configured_platforms() -> frozenset[Platform]:
    """The transports this island holds credentials for, read fresh each time —
    settings are monkeypatched by tests and could in principle be reloaded."""
    return frozenset(p for p, probe in _CONFIG_PROBES.items() if probe())


def any_transport_configured() -> bool:
    """Gate 1. True if this island can push on ANY transport.

    Each transport still gates itself individually below the door (`plan_deliveries`
    skips a row whose platform is unconfigured, and each `send` raises if called
    unconfigured), so this is the cheap short-circuit rather than the enforcement.
    """
    return bool(_configured_platforms())


# In-flight wake tasks, held so the event loop cannot garbage-collect them.
# `asyncio.create_task` returns the ONLY strong reference to a task; drop it and
# a mid-flight wake may simply vanish, which would present as an intermittently
# missed call and would be very hard to attribute. Discard on completion.
_in_flight: set[asyncio.Task] = set()


# The channel-kind vocabulary as it crosses THIS module's boundary. `Channel.kind`
# is a `String` column (the DB CHECK is the enforcement, driven by ChannelKind), so
# callers hand us a str — but the policy gate should not advertise that it accepts
# any string at all (cage-match #139 round 2, Carnot: the interface still admitted
# invalid states even after the magic literal became `ChannelKind.DM.value`). The
# alias narrows the SIGNATURE to the closed set without pretending the column is an
# enum it is not, so a type-checker rejects a bare "dm" typo at the call site while
# the runtime comparison stays the same one the DB constrains.
#
# DUPLICATED, AND HELD IN SYNC BY A TEST — `Literal` needs literal values, so it
# cannot be derived from the enum at type-check time. That makes this the second
# hand-copied closed set in this module (the first being the invite sentinel), and
# the first draft of this line got it WRONG: it carried a fifth member,
# "authenticated", which belongs to a different `kind` field elsewhere in the
# codebase and is not a ChannelKind at all. Nothing at runtime would have
# complained. `test_channel_kind_literal_matches_the_enum` is what catches it.
ChannelKindStr = Literal["standard", "llm", "robot", "dm"]


def is_call_invite(body: str) -> bool:
    """Exact match, never `startswith`/`in`. A prefix test would let any message
    beginning with the sentinel wake a device, which hands an attacker a wake
    primitive with arbitrary trailing content. Mirrors the app's
    `isCallInviteBody`, which is exact for the same reason."""
    return body == CALL_INVITE_BODY


class WakeKind(enum.Enum):
    """WHAT KIND OF WAKE this is — the thing `should_wake` decides, carried as a
    value instead of re-derived four hundred lines away.

    A plain `Enum`, not a `StrEnum`: in this codebase a StrEnum means "persisted,
    and drives a DB CHECK via `_in_check`". This is never persisted and never
    crosses the wire.

    WHY THIS IS NOT CEREMONY. "Every push this module can emit is a call invite"
    is true today only because `is_call_invite` — an EXACT equality against the
    pinned sentinel — gates both entry points, far from the header that decides
    `apns-push-type`. Threading the kind makes it a DATA-FLOW fact: the router
    emits a VoIP delivery only from a `WakeKind` it was handed, and the only
    supply is that gate. When design 12 Decision 5's cancel wake lands (named
    there as "the island's real blocker"), adding `WakeKind.CALL_END` makes the
    router's match non-exhaustive — which is exactly the moment somebody must
    DECIDE whether a cancel rings, rather than a non-call silently inheriting a
    VoIP push whose penalty is invisible to `SendResult` forever.
    """

    CALL_INVITE = "call_invite"


def should_wake(channel_kind: ChannelKindStr, body: str) -> WakeKind | None:
    """The shared domain predicate for "does this message wake a handset, and as
    what?". `None` means it does not.

    Lives here, next to the sender, rather than being re-derived at each call
    site — the same discipline as `messages_service.should_federate`, and for the
    same reason: a second send path must not be able to forget the gate.

    Compares against `ChannelKind.DM`, never a bare "dm" literal (cage-match
    #139, Carnot): the closed set already exists and drives the DB CHECK on
    `channels.kind`, so a magic string here would be entropy injected at exactly
    the policy gate — and a rename of the enum member would leave this predicate
    silently matching nothing, i.e. push quietly switching itself off.

    CALLERS MUST TEST `is None`, NEVER `not wake`. The single member is truthy
    today, so truthiness works by coincidence; a future member with a falsy value
    would turn the gate off with no error anywhere.
    """
    if channel_kind == ChannelKind.DM.value and is_call_invite(body):
        return WakeKind.CALL_INVITE
    return None


@dataclasses.dataclass(frozen=True, slots=True)
class ApnsDelivery:
    """One push to send to Apple, as PLAIN VALUES snapshotted from a row.

    `(row_id, token, updated_at)` is the reaper's observation triple, captured
    before any await: after planning, the send loop holds no ORM instance at all,
    which is the same discipline `wake_for_message`'s docstring already commits to
    (a detached instance raises on first attribute access).
    """

    row_id: str
    token: str
    updated_at: dt.datetime
    apns_environment: ApnsEnvironment
    token_kind: TokenKind


@dataclasses.dataclass(frozen=True, slots=True)
class FcmDelivery:
    """One push to send to Google. NO environment and NO token kind: FCM has one
    registry and one endpoint per project, and carrying a field the transport
    cannot honour would read as a routing decision nothing downstream makes."""

    row_id: str
    token: str
    updated_at: dt.datetime


Delivery = ApnsDelivery | FcmDelivery


def plan_deliveries(
    rows: Sequence[DeviceToken], *, wake: WakeKind,
    configured: frozenset[Platform],
) -> tuple[list[Delivery], list[tuple[str, str]]]:
    """Decide what to send where. PURE, TOTAL, and it MUST NEVER RAISE.

    No session, no settings read, no I/O, no await — `configured` is passed in
    precisely so the whole routing table stays table-testable against
    `itertools.product(Platform, TokenKind, WakeKind)`. That sweep is the real
    merge gate here: CI runs `pytest` and `secrets-integrity` and nothing else, so
    the `assert_never` calls below buy editor-time errors and nothing in CI.

    NEVER RAISING IS A HARD REQUIREMENT, not tidiness. `_wake_user` is called from
    a loop inside `wake_for_message`'s single broad `try`, so an exception here
    abandons every remaining RECIPIENT — not one device.

    Returns `(deliveries, skips)`, where each skip is `(row_id, reason)` and the
    caller logs it at the level the loudness contract specifies. Nothing is
    dropped without a reason: an earlier design partitioned on RAW STRINGS, so a
    corrupted kind fell out of every bucket and vanished with no line at all —
    the one silent cell in a design named for having none.

    ARM (B): EVERY SELECTED ROW GETS A PUSH. Both an alert row and a voip row for
    one handset is the NORMAL state (design 12 Decision 2), and rows carry NO
    device identity — the two tokens are unrelated strings and Decision 2a
    explicitly refuses to infer pairing. So "one push per handset" is NOT
    COMPUTABLE, which makes this a REPRESENTATION gap rather than a tuning
    choice, and no selection rule can be correct. The two arms cost:

      (A) voip-preferred — an alert-only SECOND Apple device (an iPad, an older
          phone) never rings while any voip row exists: a MISSED CALL.
      (B) send to every selected row — a dual-registered iPhone gets a CallKit
          ring AND a redundant banner: a BLEMISH.

    (B), for the reason this module already gives in its own words: a duplicate
    notification is a blemish; a missed call is the bug. It is also the only arm
    with no silent non-delivery, and it makes the deploy trivially safe — every
    row on both live islands is `token_kind='alert'` by server_default today and
    the alert ring is proven on real handsets, so it must keep ringing. Closing
    the residual properly needs a device/install identifier on the registration
    wire and per-group selection; that is filed, not built here.
    """
    deliveries: list[Delivery] = []
    skips: list[tuple[str, str]] = []
    for row in rows:
        try:
            # THE ORM EDGE — every closed-set column becomes its enum HERE, in one
            # place, inside one guard. The columns are `Mapped[str]` like every
            # other closed set in this codebase (claude-tasks#3400 tracks the
            # convention corpus-wide), and this is the boundary where the string
            # becomes the set again. A corrupted row is skipped with a named
            # reason instead of raising into a fanout.
            platform = Platform(row.platform)
            kind = TokenKind(row.token_kind)
            if platform not in configured:
                # OPERATOR-FIXABLE, and loud enough to be findable without being
                # the alarm. The property it preserves: an Android device that
                # registered successfully and is never woken must not become
                # indistinguishable from a delivery bug. The reason names
                # something the operator can actually act on.
                skips.append((row.id, "transport_not_configured"))
                continue
            match platform:
                case Platform.FCM:
                    # REGARDLESS OF `token_kind`. FCM has one registry (design 12
                    # Decision 2.4) and ring-ness is a property of the MESSAGE
                    # (data-only at HIGH priority), not of the token. A naive
                    # "prefer voip" rule applied across both platforms would
                    # deselect every Android row — the zero-ring failure.
                    #
                    # BUT NOT REGARDLESS OF `wake` (Tesla, cage-match PR#172 r2).
                    # This arm appended for ANY WakeKind while the APNs arm below
                    # matches on it with a real fall-through. `WakeKind` was forged
                    # precisely so that adding a member — a CALL_END cancel is the
                    # named candidate — cannot silently inherit ring behaviour. That
                    # protection existed on ONE platform: the day the enum grows,
                    # iOS would skip pending a decision while Android sent a HIGH
                    # data ring for a cancel, and `test_every_platform_token_kind_
                    # wake_combination_is_routed` sweeps the full product and
                    # REQUIRES a delivery for every member, so the instrument built
                    # to force the decision would bless the asymmetry instead.
                    #
                    # Behaviour today is unchanged — CALL_INVITE is the only member.
                    # What changes is what happens to the NEXT one: both platforms
                    # now stop and ask.
                    match wake:
                        case WakeKind.CALL_INVITE:
                            deliveries.append(
                                FcmDelivery(row.id, row.token, row.updated_at))
                        case _:
                            skips.append((row.id, "wake_kind_not_routed_for_fcm"))
                case Platform.APNS:
                    match (kind, wake):
                        case (TokenKind.VOIP, WakeKind.CALL_INVITE):
                            pass
                        case (TokenKind.ALERT, WakeKind.CALL_INVITE):
                            pass
                        case _:
                            # NOT `assert_never`: tuple narrowing is unreliable
                            # and there is no type checker in CI, so the
                            # fall-through has to be a real runtime arm. It is
                            # caught by this function's own handler, which is what
                            # keeps the never-raise contract.
                            raise ValueError(
                                f"unrouted apns wake kind={kind} wake={wake}")
                    deliveries.append(ApnsDelivery(
                        row.id, row.token, row.updated_at,
                        ApnsEnvironment(row.apns_environment), kind))
                case _:
                    assert_never(platform)
        except ValueError:
            skips.append((row.id, "unroutable_row"))
    return deliveries, skips


async def _recipients(session: AsyncSession, *, channel_id: str, sender_id: str,
                      exclude_user_ids: set[str]) -> list[str]:
    """Who to wake: the DM's other member(s), read from RAW `Membership` rows.

    Ground truth, NOT `list_members` — that is the visibility-shaped @-mention
    roster, and a safety gate reading a social projection fails OPEN the moment
    the projection starts hiding people (the same reasoning that put raw rows in
    the video-token path, cage-match #122 rd8).

    THE BLOCK SET IS READ HERE, NOT TRUSTED FROM THE CALLER (cage-match #139
    round 4, Carnot). It used to arrive as `exclude_user_ids` — the same set the
    WS route computes for fanout — which made the "single door" claim weaker than
    it read: a second caller that forgot the argument, or passed a stale one,
    would silently lose the block gate on a capability strictly louder than a
    message. A door whose lock is supplied by whoever knocks is not a door.

    So the service computes its own, and the caller's set is UNIONED in rather
    than replaced: the route's set is still authoritative for fanout consistency,
    and the service's own read is the floor no caller can drop below. Removing
    the coupling beats remembering to honour it.

    BANNED ACCOUNTS ARE EXCLUDED (cage-match #139, Maxwell). A suspended user
    keeps their membership row — ban is an auth-ingress gate, not a membership
    teardown — so a raw-rows read would happily wake the handset of an account
    that is not permitted to act on this island. The block layer traverses the
    push path structurally; the BAN layer had no such luck, because nothing
    between `create_outbound` and here consults it. `banned_at IS NULL` is the
    same condition `users_service.is_banned` tests, applied in the join rather
    than after it so a banned peer is never even a candidate.
    """
    # THE DM GATE, READ FROM THE CHANNEL ROW — not from the caller's word for it
    # (cage-match #139 round 6, Carnot). `channel_kind` arrives as an argument, so
    # a future caller could pass "dm" alongside a NON-DM channel_id and the
    # sentinel, and wake every member of a public room. The single-door claim was
    # stronger than the code: the lock was being carried in by whoever knocked.
    #
    # This is the THIRD instance of one pattern in this review — caller-supplied
    # facts standing in for gates the service claims to own (round 4: the block
    # set; round 5: detached ORM attributes; here: the channel kind). Swept as a
    # class rather than patched again, and aligned with the ESTABLISHED pattern
    # from the video-token path (rest/livekit.py), which is the other capability
    # gated on "this is really a DM". That path checks three things, and so does
    # this one now — including the cardinality assertion nobody flagged:
    #
    #   1. kind == 'dm'
    #   2. AND is_private — DEFENCE IN DEPTH, and stated honestly: the schema
    #      ALREADY guarantees this (`ck_channels_dm_private`: kind != 'dm' OR
    #      is_private, migration 0020), so the state is unrepresentable and this
    #      branch is unreachable through the DB. It is kept because it costs one
    #      comparison and a future writer path or a relaxed constraint would make
    #      it reachable — but it gets NO test, because a test that cannot create
    #      the failure cannot clear it. (Note: rest/livekit.py's equivalent check
    #      carries a now-stale comment claiming kind is NOT DB-constrained to
    #      is_private; it was true when written and the constraint landed later.)
    #   3. AND exactly one peer — DM safety rests on the room being {sender, one
    #      peer}. A malformed 3-member kind='dm' channel would otherwise wake
    #      everyone in it, which is precisely the unbounded-fanout case DM-only
    #      exists to prevent.
    channel = (await session.execute(
        select(Channel).where(Channel.id == channel_id)
    )).scalar_one_or_none()
    if channel is None or channel.kind != ChannelKind.DM.value or not channel.is_private:
        log.warning("wake refused channel=%s reason=not_a_private_dm", channel_id)
        return []

    # CARDINALITY ON THE RAW MEMBERSHIP GRAPH — no join, no filters (cage-match
    # #139 round 7, Carnot). The previous revision counted rows that had ALREADY
    # been ban-filtered, so a malformed THREE-member DM with one banned peer
    # counted as two members, passed the two-party assertion, and woke the
    # remaining peer. The channel was still structurally not a DM; only the
    # sendable-recipient set happened to look like one.
    #
    # And the comment sitting right here CLAIMED the count was "from GROUND TRUTH,
    # asserted before any exclusion is applied" while the query above it applied
    # one. That is the FOURTH time in this review that prose and behaviour
    # separated inside a single function — and this instance was written LAST
    # ROUND, in the fix that swept this very class. Worth leaving on the record:
    # the drift is not carelessness about comments, it is that a comment states
    # the invariant you INTENDED and nothing checks it against the code beside it.
    #
    # The invariant is MEMBERSHIP cardinality, not sendable-recipient cardinality.
    member_ids = set((await session.execute(
        select(Membership.user_id).where(Membership.channel_id == channel_id)
    )).scalars().all())
    if sender_id not in member_ids or len(member_ids) != 2:
        # The invariant STATED ONCE, instead of approximated (cage-match #139
        # round 8, Carnot). The previous revision counted non-sender members and
        # accepted exactly one — which never proved the SENDER was a member at
        # all. A malformed one-member private DM containing only Bob, plus a
        # caller-supplied sender_id of someone outside the channel, yielded
        # exactly one "peer" and woke Bob for a stranger.
        #
        # The real invariant is that the membership set IS {sender, peer} — and
        # writing it that way makes both halves fall out of one comparison rather
        # than requiring two checks that have to agree. Note this is the SAME
        # invariant the last three rounds kept circling: round 6 added a
        # cardinality check, round 7 fixed WHAT it counted, round 8 fixed WHOM it
        # counted. Three rounds to say one sentence correctly.
        log.warning("wake refused channel=%s reason=not_two_party members=%d "
                    "sender_is_member=%s",
                    channel_id, len(member_ids), sender_id in member_ids)
        return []
    peer_ids = list(member_ids - {sender_id})

    # ONLY NOW filter for who may actually be woken. Order matters and is the
    # whole finding: structure first, eligibility second.
    #   * banned — a suspended account keeps its membership row, and ban is an
    #     auth-INGRESS gate that nothing between create_outbound and here consults.
    #   * blocked — the caller's fanout set UNIONED with the service's own read,
    #     so neither is trusted alone.
    live = (await session.execute(
        select(User.id).where(User.id.in_(peer_ids), User.banned_at.is_(None))
    )).scalars().all()
    blocked = await moderation_service.blocked_pair_user_ids(session, sender_id)
    excluded = set(exclude_user_ids) | blocked
    return [uid for uid in live if uid not in excluded]


async def reachability(session: AsyncSession) -> dict:
    """Can this island actually reach the devices it is storing? (#3397)

    Gate 1 of the send path declines EVERY wake when no transport is configured,
    and that decline is silent and total — correctly, because an operator who
    never set up push should not get a crash. But an island holding registered
    tokens it cannot send to is DEAF while every other signal reads healthy:
    registration returns 201, the message persists, /health says ok, and the
    recipient never hears anything. A push has no user-visible success, so there
    is nothing for anyone to notice the absence of.

    The COUNT is what makes this actionable. "Push is off" is a shrug; "push is
    off and 2 devices are registered to it" is a bug with an owner.

    PER-PLATFORM, BECAUSE THE OLD REPORT LIED IN BOTH DIRECTIONS. It counted
    EVERY row with no platform predicate while `configured` was an APNs-only
    fact, so an APNs island reported its Android rows REACHABLE (the #3397
    failure in a new direction) and an FCM-only island would report every row
    unreachable — firing the boot warning on every boot of a healthy box, which
    is the warning-nobody-reads this surface exists to avoid becoming.

    NO PER-ENVIRONMENT BREAKDOWN, deliberately, and that argument does NOT extend
    to platforms. An APNs auth key (.p8) is environment-AGNOSTIC — the same key
    authenticates against both hosts (proven 2026-08-23: identical key,
    BadDeviceToken from both, i.e. auth accepted at each) — so a per-environment
    field would describe a state that cannot occur. Two transports with two
    independent credential sets is a different question with a real answer.

    THE THREE ORIGINAL KEYS KEEP THEIR NAMES so `/health`'s contract and its tests
    are untouched. `unreachable_by_platform` is additive and is read only by
    `warn_if_unreachable`.
    """
    configured = _configured_platforms()
    counts = (await session.execute(
        select(DeviceToken.platform, func.count())
        .group_by(DeviceToken.platform))).all()
    unreachable_by_platform: dict[str, int] = {}
    for platform_value, count in counts:
        try:
            reachable = Platform(platform_value) in configured
        except ValueError:
            # FAIL CLOSED. A platform string outside the enum can only come from a
            # corrupted row or a member added without a config probe; either way
            # nothing can send to it, and calling it reachable would hide the one
            # device class that is guaranteed unreachable.
            reachable = False
        if not reachable:
            unreachable_by_platform[platform_value] = count
    return {
        "configured": bool(configured),
        "registered_devices": sum(count for _, count in counts),
        # Kept as its own field rather than left for the reader to derive: this is
        # the number an operator acts on, and a signal you have to compute is one
        # you skip.
        "unreachable_devices": sum(unreachable_by_platform.values()),
        "unreachable_by_platform": unreachable_by_platform,
    }


# What an operator must set to make each transport reachable. Keyed by the STORED
# platform string rather than the enum, so a corrupted row still gets a sentence.
_UNREACHABLE_REMEDY = {
    # ALL FIVE, and the fifth is not decoration (Tesla, cage-match PR#172 r1). This
    # string is INSTRUCTIONS AN OPERATOR FOLLOWS. Naming four of the five members of
    # config.py's all-or-none group tells them to build precisely the half-set that
    # turns the next restart under `restart: always` into a boot refusal — a remedy
    # that causes the outage it is printed to prevent. The credential set has one
    # definition; every place that enumerates it has to agree with that definition,
    # and the places a HUMAN reads are the ones where disagreeing costs most.
    Platform.APNS.value: ("Set APNS_KEY_ID / APNS_TEAM_ID / APNS_TOPIC / "
                          "APNS_VOIP_TOPIC / APNS_PRIVATE_KEY"),
    # DO NOT SAY "Set FCM_SERVICE_ACCOUNT_JSON" (Tesla, cage-match PR#172 r4).
    # config.py REFUSES TO BOOT on a present credential until the Android receive
    # half exists. Both live islands already hold Android device rows, so this line
    # prints on every boot today — and an operator who obeyed it would write the
    # var, pull, and crash-loop under `restart: always` with the island already
    # down. There is no FCM preflight to catch it on the way in.
    #
    # This is the APNs four-of-five remedy defect committed a second time, one
    # transport over, in the same change that fixed the first: an operator-facing
    # sentence that builds exactly the state the guard refuses. The suite pinned
    # both halves in isolation — the warning must name FCM, a present blob must
    # refuse to boot — and never collided them, so a full green could not see it.
    Platform.FCM.value: ("Android push is not available on this island yet: the "
                         "client has no receive half, so the credential is "
                         "refused at boot. Do NOT set FCM_SERVICE_ACCOUNT_JSON"),
}


async def warn_if_unreachable(session: AsyncSession) -> None:
    """Say it once at boot, loudly, and ONLY when there is something to say.

    Called from the lifespan after verify_schema. On the enspyr incident this
    would have printed the moment the process came up and ended a four-hour
    investigation before it started.

    ONE WARNING PER UNCONFIGURED-BUT-POPULATED PLATFORM, each silent when its own
    arm is healthy. An island serving Android with no Apple credentials is not
    broken and must not be told it is.

    SILENT ON THE HEALTHY CASES, and that is the load-bearing half. Push simply
    not being configured is a legitimate, intended state for most islands; an
    unconfigured island with zero tokens has nothing wrong with it. A warning
    that fires on healthy boxes is one every operator learns to scroll past,
    which is the original silence wearing a high-vis vest.

    EVERY MESSAGE NAMES #2301, and that clause is the only mitigation available
    for the one deploy gap nothing mechanical closes: `deploy/update.sh` pulls the
    IMAGE and never syncs the box's `docker-compose.yml`, so a variable the
    operator has set in `.env` can be inert in the container with every other
    signal reading healthy. It turns a four-hour investigation into a grep.
    """
    report = await reachability(session)
    for platform_value, count in sorted(report["unreachable_by_platform"].items()):
        remedy = _UNREACHABLE_REMEDY.get(
            platform_value,
            f"No transport exists for platform={platform_value!r} — this is a "
            "corrupted row or a code/data mismatch")
        log.warning(
            "%d device token(s) registered on platform=%s but that transport is "
            "NOT configured on this island — those devices are UNREACHABLE and "
            "every wake for them will be silently declined. %s, or unregister "
            "them — and check this box's docker-compose.yml actually forwards it "
            "(#2301: update.sh pulls the image, it does NOT sync compose).",
            count, platform_value, remedy)


async def _wake_user(session: AsyncSession, user_id: str, *, wake: WakeKind,
                     payload: WakePayload, collapse_id: str) -> None:
    """Push to every device this user has registered that this island can reach,
    reaping the ones a transport positively declares dead."""
    # A ROW IS NOT A SENDABLE ROW (cage-match #139 round 2, Carnot). Round 1 moved
    # the budget charge below a fetch of ALL this user's device rows and claimed
    # the budget was then only spent when there was "something to spend it on" —
    # but a recipient holding only an unsendable token would still burn wake slots
    # on every call, so a phone registered later in the same minute could find its
    # first real wake already throttled.
    #
    # Partitioned in PYTHON rather than filtered in the query, deliberately, and
    # the reason survived the arrival of a second transport: filtering in SQL
    # fixes the budget but silently discards the reason the unsendable rows were
    # ever visited — a device that registered successfully and is never woken
    # would become indistinguishable from a delivery bug. One query, a planner
    # that names every skip, an early return the budget never sees.
    rows = (await session.execute(
        select(DeviceToken).where(DeviceToken.user_id == user_id)
    )).scalars().all()
    deliveries, skips = plan_deliveries(
        rows, wake=wake, configured=_configured_platforms())
    for row_id, reason in skips:
        if reason == "unroutable_row":
            # ERROR: a row the island cannot classify at all is a data or code
            # defect, not an operator setting.
            log.error("wake skipped device=%s reason=unroutable_row", row_id)
        else:
            log.info("wake skipped device=%s reason=%s", row_id, reason)
    if not deliveries:
        # Not an error: a user with no device this island can send to cannot be
        # woken. Debug because it is the normal state for every account that has
        # not yet run a build with push wired in.
        log.debug("wake skipped user=%s reason=no_sendable_devices", user_id)
        return

    # BUDGET IS SPENT HERE, after we know there is something to spend it on
    # (cage-match #139, Maxwell). Charging it in the caller metered *attempts to
    # wake an unwakeable user* — every call invitation to a peer with no
    # registered device burned a slot — which is not what the setting says it
    # meters, and would have throttled the first real wake of a user who had
    # been called a few times before installing a push-capable build.
    #
    # Keyed on the RECIPIENT: waking interrupts a person wherever they are, so
    # the budget protects the person being interrupted rather than throttling per
    # sender, which a second sender would simply route around.
    #
    # ONE BUCKET FOR EVERY TRANSPORT, charged ONCE per fanout. A per-transport
    # bucket would hand a peer holding both an iPhone and an Android twice the
    # ring budget, which contradicts the doctrine in the paragraph above — the
    # budget is about the person, not the wire. The bucket name is transport-
    # neutral on purpose, so a reader adding a transport has nowhere natural to
    # put a second counter. `settings.apns_wake_per_recipient_per_minute` keeps
    # its name for the reason written at its definition.
    #
    # SCOPE (Carnot): this counter is PER-PROCESS. The gateway is single-worker
    # by construction (worker_guard), so per-process is the whole population
    # today — but `GATEWAY_ALLOW_MULTIWORKER=true` or any horizontal scaling
    # multiplies this budget by the worker count. Waking a handset is louder than
    # delivering a message, so that limitation is worth stating rather than
    # inheriting silently: a shared-storage counter is the fix if this ever scales.
    allowed, _ = limiter.hit("push_wake", user_id,
                             settings.apns_wake_per_recipient_per_minute, 60.0)
    if not allowed:
        log.warning("wake throttled user=%s", user_id)
        return

    # (row_id, token, updated_at) AS OBSERVED AT SEND TIME, plus the transport's
    # reaping decision. See the conditional DELETE below for why all three are
    # carried.
    dead: list[tuple[str, str, object, ReapOrder | None]] = []
    delivered = 0

    async def _send_one(delivery: Delivery) -> SendResult | None:
        """ONE device's send, with the per-device boundary INSIDE it.

        The boundary was already an exception boundary (cage-match #139 round 6,
        Carnot) and its comment already claimed to be a cross-transport one:
        "Apple being down must not cost the Android half of a fanout, or the
        reverse." That claim was FALSE while this ran as a serial `for` loop, and
        the falsehood was invisible to the tests that existed — the only
        cross-transport test inserted its APNs row first, so Apple was reached
        first by rowid accident and the assertion never looked at WHEN.

        The real coupling was TIME, not exceptions. Both clients carry a 10s
        httpx timeout and an FCM OAuth transport failure is deliberately not
        negative-cached, so a blackholed Google cost the iPhone in the SAME
        fanout up to ~10s per Android row — inside the 30s ring lease and
        outside the app's admission window. A ring that arrives after the ring
        is over is not a degraded ring, it is a missed call.

        So the sends now run CONCURRENTLY and the boundary is per-coroutine.
        Nothing here needs ordering: the session is untouched until the reap
        loop below, and the budget was charged before any send. `gather`
        preserves input order, so the `dead` list is still deterministic.
        """
        try:
            # THE ONLY DISPATCH IN THE MODULE. One match on the Delivery union —
            # never an iteration over a transport registry, which would be a
            # second door wearing a dict.
            match delivery:
                case ApnsDelivery():
                    result = await apns.send(
                        delivery.token, payload,
                        apns_environment=delivery.apns_environment,
                        token_kind=delivery.token_kind,
                        collapse_id=collapse_id)
                    # THE SEMANTIC RECORD OF A SEND, keyed by ROW ID rather than by
                    # token (claude-tasks#3586). httpx knows only the URL, so the
                    # best it can do is a redacted token prefix; the row id is a
                    # non-secret ULID that correlates EXACTLY with the table and
                    # leaks nothing. Logged for EVERY outcome, not just failures:
                    # before this line a successful send wrote nothing of our own,
                    # so "did the push go out?" was answerable only by the ABSENCE
                    # of a failure line — the silence-reads-as-success trap this
                    # repo has a standing rule against.
                    log.info("apns sent device=%s env=%s kind=%s verdict=%s",
                             delivery.row_id, delivery.apns_environment.value,
                             delivery.token_kind.value, result.verdict.value)
                case FcmDelivery():
                    result = await fcm.send(delivery.token, payload,
                                            collapse_key=collapse_id)
                    log.info("fcm sent device=%s verdict=%s",
                             delivery.row_id, result.verdict.value)
                case _:
                    assert_never(delivery)
        except Exception:
            # Each transport swallows its own protocol errors, but either can
            # still raise from credential handling, client construction, or a
            # future defect. Serially that abandoned every remaining device AND
            # every remaining recipient; concurrently it would poison the gather.
            # Treated as transient: log, skip, never reap.
            log.exception("wake failed for one device user=%s", user_id)
            return None
        return result

    # `return_exceptions` is deliberately NOT set: `_send_one` already catches
    # everything and returns None, so an exception escaping to here would be a
    # defect in that guard and should be loud rather than silently collected.
    results = await asyncio.gather(*(_send_one(d) for d in deliveries))

    for delivery, result in zip(deliveries, results, strict=True):
        if result is None:
            continue
        if result.verdict is Verdict.DELIVERED:
            delivered += 1
        if result.verdict is Verdict.DEAD_TOKEN:
            dead.append((delivery.row_id, delivery.token, delivery.updated_at,
                         result.reap))

    if not delivered:
        # THE ALARM NOTHING ELSE IN THIS SYSTEM HAS. Every gate passed, a recipient
        # was selected, sends were attempted, and not one came back DELIVERED. The
        # per-device lines say what each transport answered; this says the ring
        # failed, which is the sentence an operator is actually looking for.
        #
        # EXCEPT WHEN THE ONLY ANSWER WAS "DEAD, BUT I CANNOT PROVE IT" (Carnot,
        # cage-match PR#172 r1). A `DEAD_TOKEN` carrying no `reap` order is the
        # reaper DELIBERATELY withholding authority to delete — an APNs 410 with no
        # timestamp, say. The row is retained on purpose, so it answers the same way
        # on every future wake, so this ERROR fires FOREVER for a state the system
        # is correctly holding. That is precisely the warning-nobody-reads this
        # module's own `warn_if_unreachable` note argues against, manufactured by
        # the alarm meant to be the one signal worth trusting.
        #
        # The distinction is not severity-shading, it is a DIFFERENT FACT: "the ring
        # failed and I do not know why" versus "every device I could reach is dead
        # and I am not permitted to reap it". Only the first is an operator's
        # emergency; the second is a cleanup backlog, and it names itself so nobody
        # chases a ring outage that is not happening.
        answered = [r for r in results if r is not None]
        unreapable_dead = (
            bool(answered)
            and len(answered) == len(deliveries)
            and all(r.verdict is Verdict.DEAD_TOKEN and r.reap is None
                    for r in answered))
        if unreapable_dead:
            log.warning(
                "wake delivered_to=0 user=%s devices=%d reason=dead_unreapable",
                user_id, len(deliveries))
        else:
            log.error("wake delivered_to=0 user=%s devices=%d",
                      user_id, len(deliveries))

    for row_id, token, updated_at, order in dead:
        # COMPARE-AND-DELETE, because there is a real TOCTOU window here and this
        # is the only irreversible operation in the module (cage-match #139 round
        # 3, Carnot).
        #
        # A transport's `send` is an AWAITED network call. Between issuing it and
        # acting on its verdict, the device can re-register: `register_device`
        # upserts keyed on the globally-unique token, so the SAME row id can be
        # refreshed, or reassigned to a different account when a handset changes
        # hands (logout A -> login B). Deleting by id alone acts on a verdict about
        # the row as it WAS, destroying a registration made while we were waiting —
        # and a destroyed device row cannot be re-derived from anything the island
        # holds. The user must reopen the app to be reachable again, which is
        # exactly what push exists to avoid needing.
        #
        # So the delete is CONDITIONAL on the row still being the one we sent to:
        # same token, and untouched since (`updated_at` is refreshed by
        # register_device's upsert, explicitly). If anything re-registered in the
        # window, the WHERE matches nothing and the row survives — a stale token
        # lingering costs one wasted request per send, which is the correct side
        # to err on for a reaper.
        #
        # This is the codebase's established SQLite-safe pattern: an atomic
        # conditional DELETE rather than a read-then-write (`FOR UPDATE` is inert
        # on SQLite — see the concurrency notes in memberships_service).
        #
        # THE REAPER HOLDS NO TRANSPORT NAME AND NO PLATFORM BRANCH, and that is
        # the generalisation: it was made to serve two transports by REMOVING
        # knowledge, not by adding a branch. What evidence proves a death is a fact
        # about each provider's protocol and now lives with it (`apns._reap_order`,
        # `fcm._reap_order_for`); what remains here is the question this layer can
        # answer for everybody — is this still the row we sent to?
        if order is None:
            # NO ORDER, NO REAP (cage-match #139 round 6, Carnot, generalised). The
            # transport observed a death it cannot prove is current — for APNs, a
            # 410 with no `timestamp`, the only evidence separating "this token is
            # dead" from "this token WAS dead before the user reinstalled and got
            # the same token back". Failing safe for a reaper means NOT deleting,
            # at a cost of one wasted request per send against a stale row: the
            # same trade already accepted for BadDeviceToken.
            #
            # WARNING because it is unexpected: if it ever becomes common the
            # reaper is effectively off, and that should be visible rather than
            # inferred.
            log.warning("reap skipped user=%s device=%s "
                        "reason=dead_without_reap_order", user_id, row_id)
            continue
        conditions = [
            DeviceToken.id == row_id,
            DeviceToken.token == token,
            DeviceToken.updated_at == updated_at,
        ]
        if order.not_reregistered_since is not None:
            # APPLE'S OWN RULE, not just our race guard (cage-match #139 round 4,
            # Carnot). The equality checks above only cover the network await;
            # this covers a row that was ALREADY refreshed before the send, whose
            # death notice is simply stale. Keep the row when our registration is
            # newer than the provider's invalidation. FCM supplies no such date —
            # see `fcm._reap_order_for` for what carries the reversibility there,
            # and for the falsifier if that reasoning is wrong.
            conditions.append(
                DeviceToken.updated_at <= order.not_reregistered_since)
        outcome = await session.execute(delete(DeviceToken).where(*conditions))
        if outcome.rowcount:
            log.info("reaped dead device row user=%s device=%s", user_id, row_id)
        else:
            # Not an error — the row changed under us, which is precisely the case
            # this guard exists to protect. WARNING rather than INFO because it is
            # also the TRIPWIRE for the FCM reaper's one unverified assumption
            # (that a refreshed FCM token is always a NEW string): if that is
            # wrong, it shows up here as volume.
            log.warning("reap skipped user=%s device=%s "
                        "reason=row_changed_since_send", user_id, row_id)
    if dead:
        await session.commit()


async def wake_for_message(*, channel_id: str, channel_kind: ChannelKindStr, sender_id: str,
                           body: str, exclude_user_ids: set[str]) -> None:
    """Wake the other DM member's devices for an accepted call invitation.

    Takes PLAIN VALUES, never ORM instances. The caller's session is already
    closed by the time this runs, and a detached instance would raise on the
    first attribute access — so the boundary is ids and strings, which cannot
    carry a session with them.

    Never raises: a push failure must not be able to affect the message send that
    triggered it. The message is the durable, authoritative thing; the push is a
    hint that one arrived.
    """
    if not any_transport_configured():
        return
    # RE-DERIVED HERE, NOT PASSED IN. `schedule_wake` computes the same value for
    # its cheap short-circuit, but handing it down as an argument would make the
    # WakeKind a caller-supplied fact — and a caller could then hand this function
    # CALL_INVITE alongside a body that is not one, which is exactly the
    # caller-supplied-lock defect rounds 4, 5 and 6 of cage-match #139 swept out of
    # this module three times (the block set, the ORM attributes, the channel
    # kind). The gate is the ONLY supply of a WakeKind; recomputing it is free.
    #
    # `is None`, never `not wake`: see `should_wake`.
    wake = should_wake(channel_kind, body)
    if wake is None:
        return

    try:
        async with SessionLocal() as session:
            recipients = await _recipients(
                session, channel_id=channel_id, sender_id=sender_id,
                exclude_user_ids=exclude_user_ids)
            payload = WakePayload(channel_id=channel_id)
            for user_id in recipients:
                # The per-recipient budget is charged inside _wake_user, once the
                # recipient is known to have a device worth waking.
                await _wake_user(session, user_id, wake=wake, payload=payload,
                                 collapse_id=channel_id)
    except Exception:
        # Deliberately broad. This runs detached in a background task, where an
        # escaping exception is logged by asyncio at GC time (or lost) rather than
        # surfacing anywhere useful — and there is nothing above to handle it.
        log.exception("wake failed channel=%s", channel_id)


def schedule_wake(*, channel_id: str, channel_kind: ChannelKindStr, sender_id: str,
                  body: str, exclude_user_ids: set[str]) -> None:
    """Fire-and-forget the wake. THE SEND PATH MUST NOT WAIT ON APPLE.

    A push is a round trip to Apple's servers. Awaiting it inline would put that
    latency — and its failure modes — between the sender pressing call and their
    own client's acknowledgement, making the caller's experience hostage to the
    callee's notification transport. So the wake runs after the message is
    already durable and already fanned out, on its own task and its own session.

    Cheap short-circuit before scheduling anything: on an island with no push
    credentials at all this is a predicate call and no task at all. ANY transport,
    not APNs specifically — gating this on one transport meant an FCM-configured,
    APNs-less island created no task and was silently, totally deaf.

    NEVER RAISES — and the guard is the point (cage-match #139, Maxwell+Carnot).
    `wake_for_message` protects the send path from a push that FAILS, but this
    function is where the push is *scheduled*, and scheduling has its own failure
    mode: `asyncio.create_task` raises `RuntimeError` when there is no running
    loop or the loop is closing. Unguarded, that propagates out of `_handle_send`
    — so a client disconnecting during shutdown could take down the very message
    path this module swears it cannot touch. The doctrine has to cover the
    scheduling, not only the sending.

    (Carnot's related note: this needs a running event loop, so a future
    synchronous or off-loop caller gets the same RuntimeError. The guard turns
    that from a crash into a logged no-op, which is the right failure for an
    optional capability, but such a caller should pass a loop rather than rely
    on it.)
    """
    if not any_transport_configured() or should_wake(channel_kind, body) is None:
        return
    coro = wake_for_message(
        channel_id=channel_id, channel_kind=channel_kind, sender_id=sender_id,
        body=body, exclude_user_ids=exclude_user_ids)
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # No running loop / loop closing. The message is already durable and
        # already fanned out; only the wake is lost.
        #
        # close() the orphan explicitly: a coroutine created but never awaited
        # emits `RuntimeWarning: coroutine was never awaited` at GC time. That
        # warning would surface on exactly the shutdown path this guard exists to
        # make quiet, turning a handled condition back into log noise that reads
        # like a bug.
        coro.close()
        log.warning("wake not scheduled channel=%s reason=no_running_loop", channel_id)
        return
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)


async def aclose(timeout: float = 5.0) -> None:
    """Drain in-flight wakes, then let the transports close. Call BEFORE
    ``apns.aclose()`` and ``fcm.aclose()`` — drain first, then close EVERY
    transport.

    A FIX-INTERACTION DEFECT, found by two reviewers independently (cage-match
    #139, Maxwell + Carnot). `_in_flight` and a transport's `aclose()` are each
    correct in isolation and collided: `_in_flight` exists so the GC cannot eat a
    live wake, and `aclose()` exists so the pooled connection is not leaked — but
    closing a shared client while a task is mid-`send()` tears the connection out
    from under it. Adding a second transport does not change the ordering, it
    only means there are now two clients that must not close early. The task then dies inside `wake_for_message`'s broad `except`
    and logs "wake failed", which is a misleading epitaph for an orderly-shutdown
    bug: it reads as Apple's fault forever.

    Holding a strong reference is not ownership (Carnot). Ownership is draining.

    BOUNDED, not unbounded: shutdown must not hang on an unreachable Apple. Wakes
    still running after `timeout` are cancelled — a lost wake during shutdown is
    the correct trade against a gateway that will not stop.
    """
    if not _in_flight:
        return
    pending = set(_in_flight)
    done, still_running = await asyncio.wait(pending, timeout=timeout)
    for task in still_running:
        task.cancel()
    if still_running:
        # Let the cancellations actually land before the caller closes the client.
        await asyncio.gather(*still_running, return_exceptions=True)
        log.warning("cancelled %d in-flight wake(s) at shutdown", len(still_running))
