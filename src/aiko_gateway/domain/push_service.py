"""Push wake — the single door through which a handset gets woken (#3267 inc 2).

The island already reaches you when the app is open: a call invitation is an
ordinary signed message and the live WebSocket fanout delivers it. This module
exists for the case the fanout cannot serve — a CLOSED app, which holds no
socket and therefore learns nothing. Without it a call to a closed phone is
missed silently and permanently, which is the whole of claude-tasks#3253.

WHY A SINGLE DOOR. Everything security-relevant about waking a device is a
decision about WHO may cause it, and those decisions are worthless if a second
send path can skip them. So the route, any in-process caller and the tests all
enter through `schedule_wake`; `apns.py` underneath is pure transport and holds
no policy at all.

THE GATES, and the order they run in:

  1. **Configured.** No credentials → this island never pushes. Silent and total.
  2. **A call invitation in a DM.** Not every message wakes a phone. Today the
     ONLY trigger is the pinned call-invite sentinel in a `kind='dm'` channel.
  3. **Downstream of an accepted message.** This is the load-bearing one and it
     is structural rather than a check written here — see below.
  4. **Not a blocked pair.** Belt-and-braces over gate 3.
  5. **A per-recipient wake budget.** Waking is louder than sending.

GATE 3, STATED PROPERLY, BECAUSE IT IS WHY THE BLOCK RULES TRAVERSE FOR FREE.
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
import datetime as dt
import logging
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from . import apns, moderation_service
from .models import Channel, ChannelKind, DeviceToken, Membership, Platform, User
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


def should_wake(channel_kind: ChannelKindStr, body: str) -> bool:
    """The shared domain predicate for "does this message wake a handset?".

    Lives here, next to the sender, rather than being re-derived at each call
    site — the same discipline as `messages_service.should_federate`, and for the
    same reason: a second send path must not be able to forget the gate.

    Compares against `ChannelKind.DM`, never a bare "dm" literal (cage-match
    #139, Carnot): the closed set already exists and drives the DB CHECK on
    `channels.kind`, so a magic string here would be entropy injected at exactly
    the policy gate — and a rename of the enum member would leave this predicate
    silently matching nothing, i.e. push quietly switching itself off.
    """
    return channel_kind == ChannelKind.DM.value and is_call_invite(body)


def _payload(channel_id: str) -> dict:
    """The wake payload — DELIBERATELY OPAQUE, and the opacity is the feature.

    APNs is an intermediary we cannot remove, and it can read everything we send
    it. A payload saying "Alice is calling you" would tell Apple who calls whom,
    on a product whose entire thesis is that such facts stay with the operator.
    So the push carries a wake and a destination, never an identity: Apple learns
    that a device was woken and when — timing and frequency — but not by whom.

    The cost, stated honestly rather than hidden: the notification the user sees
    on the lock screen cannot name the caller either, because the app has not yet
    spoken to the island when iOS renders it. Naming the caller would require
    either putting the name in this payload (the thing we are refusing) or a
    Notification Service Extension that fetches it on-device before display
    (real, but out of scope here). Until then a wake reads "Incoming call".

    `channel_id` is the one identifier included. It is what makes the tap land in
    the right conversation, and it is stable — so Apple can correlate repeated
    wakes for the same conversation over time. That is a genuine residual, judged
    worth the deep link; it is not a claim that the payload leaks nothing.
    """
    return {
        "aps": {
            "alert": {"title": "Incoming call", "body": "Tap to join"},
            "sound": "default",
        },
        # Short key: the payload has a 4KB ceiling and this is the only custom
        # field, so there is no reason to spend bytes on a long name.
        "c": channel_id,
    }


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
    peer_ids = (await session.execute(
        select(Membership.user_id).where(
            Membership.channel_id == channel_id,
            Membership.user_id != sender_id,
        )
    )).scalars().all()
    if len(peer_ids) != 1:
        log.warning("wake refused channel=%s reason=not_two_party peers=%d",
                    channel_id, len(peer_ids))
        return []

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


async def _wake_user(session: AsyncSession, user_id: str, payload: dict,
                     collapse_id: str) -> None:
    """Push to every device this user has registered, reaping the dead ones."""
    # A ROW IS NOT A SENDABLE ROW (cage-match #139 round 2, Carnot). Round 1 moved
    # the budget charge below a fetch of ALL this user's device rows and claimed
    # the budget was then only spent when there was "something to spend it on" —
    # but a recipient holding only an Android/FCM token would still burn wake
    # slots on every call, so an iPhone registered later in the same minute could
    # find its first real wake already throttled. Prose and behaviour had drifted
    # apart inside one function, the same defect class as the reaper's extra arm.
    #
    # Partitioned in PYTHON rather than filtered in the query, deliberately.
    # Filtering in SQL fixes the budget but silently discards the reason the
    # unsupported rows were ever visited: an Android device that registered
    # successfully and is never woken would become indistinguishable from a
    # delivery bug. Both properties are wanted, so keep both — one query, an
    # early return that the budget never sees, and the skip still says why.
    rows = (await session.execute(
        select(DeviceToken).where(DeviceToken.user_id == user_id)
    )).scalars().all()
    tokens = [r for r in rows if r.platform == Platform.APNS.value]
    for r in rows:
        if r.platform != Platform.APNS.value:
            # FCM (Android) is a separate transport behind this same door and is
            # NOT built. Loud, not silent.
            log.info("wake skipped user=%s platform=%s reason=transport_not_built",
                     user_id, r.platform)
    if not tokens:
        # Not an error: a user with no APNs-sendable device cannot be woken by
        # this island today. Debug because it is the normal state for every
        # account that has not yet run a build with push wired in.
        log.debug("wake skipped user=%s reason=no_apns_devices", user_id)
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
    # SCOPE (Carnot): this counter is PER-PROCESS. The gateway is single-worker
    # by construction (worker_guard), so per-process is the whole population
    # today — but `GATEWAY_ALLOW_MULTIWORKER=true` or any horizontal scaling
    # multiplies this budget by the worker count. Waking a handset is louder than
    # delivering a message, so that limitation is worth stating rather than
    # inheriting silently: a shared-storage counter is the fix if this ever scales.
    allowed, _ = limiter.hit("apns_wake", user_id,
                             settings.apns_wake_per_recipient_per_minute, 60.0)
    if not allowed:
        log.warning("wake throttled user=%s", user_id)
        return

    # (row_id, token, updated_at) AS OBSERVED AT SEND TIME — not just the id.
    # See the conditional DELETE below for why all three are carried.
    dead: list[tuple[str, str, object, int | None]] = []
    for row in tokens:
        observed = (row.id, row.token, row.updated_at)
        try:
            result = await apns.send(row.token, payload, collapse_id=collapse_id)
        except Exception:
            # PER-DEVICE BOUNDARY (cage-match #139 round 6, Carnot). `apns.send`
            # swallows httpx errors itself, but it can still raise from provider-
            # token signing, client construction, or any future transport defect —
            # and the only other catch is OUTSIDE this whole loop, so one bad row
            # or environment edge would abandon every remaining device AND every
            # remaining recipient. Entropy localizes only where you build the
            # boundary. Treated as transient: log, skip, keep going, never reap.
            log.exception("wake failed for one device user=%s", user_id)
            continue
        if result.verdict is apns.Verdict.DEAD_TOKEN:
            dead.append((*observed, result.invalid_since_ms))

    for row_id, token, updated_at, invalid_since_ms in dead:
        # COMPARE-AND-DELETE, because there is a real TOCTOU window here and this
        # is the only irreversible operation in the module (cage-match #139 round
        # 3, Carnot).
        #
        # `apns.send` is an AWAITED network call. Between issuing it and acting on
        # its verdict, the device can re-register: `register_device` upserts keyed
        # on the globally-unique token, so the SAME row id can be refreshed, or
        # reassigned to a different account when a handset changes hands
        # (logout A → login B). Deleting by id alone acts on a verdict about the
        # row as it WAS, destroying a registration made while we were waiting —
        # and a destroyed device row cannot be re-derived from anything the island
        # holds. The user must reopen the app to be reachable again, which is
        # exactly what push exists to avoid needing.
        #
        # So the delete is CONDITIONAL on the row still being the one we sent to:
        # same token, and untouched since (`updated_at` is refreshed by
        # register_device's upsert via `onupdate`). If anything re-registered in
        # the window, the WHERE matches nothing and the row survives — a stale
        # token lingering costs one wasted request per send, which is the correct
        # side to err on for a reaper.
        #
        # This is the codebase's established SQLite-safe pattern: an atomic
        # conditional DELETE rather than a read-then-write (`FOR UPDATE` is inert
        # on SQLite — see the concurrency notes in memberships_service).
        conditions = [
            DeviceToken.id == row_id,
            DeviceToken.token == token,
            DeviceToken.updated_at == updated_at,
        ]
        if invalid_since_ms is None:
            # NO TIMESTAMP, NO REAP (cage-match #139 round 6, Carnot). Apple
            # documents `timestamp` on a 410, and it is the ONLY evidence that
            # distinguishes "this token is dead" from "this token WAS dead before
            # the user reinstalled and got the same token back". Without it the
            # equality guards cover only the network-await window, which leaves
            # real ambiguity on the one irreversible operation in the module.
            #
            # This module's stated posture is that failing safe for a reaper means
            # NOT deleting, and the cost of honouring it here is one wasted request
            # per send against a stale row — the same trade already accepted for
            # BadDeviceToken. Applying the doctrine consistently rather than only
            # where it was convenient. Logged at warning because a 410 without a
            # timestamp is unexpected: if it ever becomes common the reaper is
            # effectively off, and that should be visible rather than inferred.
            log.warning("reap skipped user=%s reason=410_without_timestamp", user_id)
            continue
        else:
            # APPLE'S OWN RULE, not just our race guard (cage-match #139 round 4,
            # Carnot). A 410 body carries the moment APNs confirmed the token
            # invalid, and Apple says to resume pushing if the app registered that
            # token AGAIN since. The equality checks above only cover the network
            # await; this covers a row that was ALREADY refreshed before the send,
            # whose 410 is simply stale. Keep the row when our registration is
            # newer than Apple's invalidation.
            invalid_since = dt.datetime.fromtimestamp(
                invalid_since_ms / 1000, tz=dt.UTC)
            conditions.append(DeviceToken.updated_at <= invalid_since)
        # Reached ONLY with a timestamp in hand: the None arm above `continue`s.
        # (This comment previously said the equality guards decided on their own
        # when no timestamp arrived — true before round 6, stale the moment the
        # fail-safe arm landed. Left corrected rather than deleted because the
        # drift is the point: it is the third time in this review that prose and
        # behaviour separated inside one function.)
        outcome = await session.execute(delete(DeviceToken).where(*conditions))
        if outcome.rowcount:
            log.info("reaped dead device row user=%s", user_id)
        else:
            # Not an error — the row changed under us, which is precisely the
            # case this guard exists to protect. Logged so a reaper that never
            # reaps is diagnosable rather than mysterious.
            log.info("reap skipped user=%s reason=row_changed_since_send", user_id)
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
    if not apns.is_configured():
        return
    if not should_wake(channel_kind, body):
        return

    try:
        async with SessionLocal() as session:
            recipients = await _recipients(
                session, channel_id=channel_id, sender_id=sender_id,
                exclude_user_ids=exclude_user_ids)
            payload = _payload(channel_id)
            for user_id in recipients:
                # The per-recipient budget is charged inside _wake_user, once the
                # recipient is known to have a device worth waking.
                await _wake_user(session, user_id, payload, collapse_id=channel_id)
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

    Cheap short-circuit before scheduling anything: on an island with no APNs
    credentials (every island today) this is a predicate call and no task at all.

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
    if not apns.is_configured() or not should_wake(channel_kind, body):
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
    """Drain in-flight wakes, then let the transport close. Call BEFORE
    ``apns.aclose()``.

    A FIX-INTERACTION DEFECT, found by two reviewers independently (cage-match
    #139, Maxwell + Carnot). `_in_flight` and `apns.aclose()` are each correct in
    isolation and collided: `_in_flight` exists so the GC cannot eat a live wake,
    and `apns.aclose()` exists so the pooled HTTP/2 connection is not leaked — but
    closing the shared client while a task is mid-`send()` tears the connection out
    from under it. The task then dies inside `wake_for_message`'s broad `except`
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
