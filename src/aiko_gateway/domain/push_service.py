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
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from . import apns
from .models import DeviceToken, Membership, Platform
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


def is_call_invite(body: str) -> bool:
    """Exact match, never `startswith`/`in`. A prefix test would let any message
    beginning with the sentinel wake a device, which hands an attacker a wake
    primitive with arbitrary trailing content. Mirrors the app's
    `isCallInviteBody`, which is exact for the same reason."""
    return body == CALL_INVITE_BODY


def should_wake(channel_kind: str, body: str) -> bool:
    """The shared domain predicate for "does this message wake a handset?".

    Lives here, next to the sender, rather than being re-derived at each call
    site — the same discipline as `messages_service.should_federate`, and for the
    same reason: a second send path must not be able to forget the gate.
    """
    return channel_kind == "dm" and is_call_invite(body)


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
    """
    rows = (await session.execute(
        select(Membership.user_id).where(
            Membership.channel_id == channel_id,
            Membership.user_id != sender_id,
        )
    )).scalars().all()
    return [uid for uid in rows if uid not in exclude_user_ids]


async def _wake_user(session: AsyncSession, user_id: str, payload: dict,
                     collapse_id: str) -> None:
    """Push to every device this user has registered, reaping the dead ones."""
    tokens = (await session.execute(
        select(DeviceToken).where(DeviceToken.user_id == user_id)
    )).scalars().all()
    if not tokens:
        # Not an error: a user with no registered device simply cannot be woken.
        # Logged at debug because it is the normal state for every account that
        # has not yet run a build with push wired in.
        log.debug("wake skipped user=%s reason=no_devices", user_id)
        return

    dead: list[str] = []
    for row in tokens:
        if row.platform != Platform.APNS.value:
            # FCM (Android) is a separate transport behind this same door and is
            # NOT built. Skipping loudly rather than silently: an Android device
            # that registered successfully and is never woken would otherwise be
            # indistinguishable from a delivery bug.
            log.info("wake skipped user=%s platform=%s reason=transport_not_built",
                     user_id, row.platform)
            continue
        verdict = await apns.send(row.token, payload, collapse_id=collapse_id)
        if verdict is apns.Verdict.DEAD_TOKEN:
            dead.append(row.id)

    if dead:
        # Reap only what Apple positively declared dead — see `apns._verdict` for
        # why this set is narrower than it first appears.
        await session.execute(delete(DeviceToken).where(DeviceToken.id.in_(dead)))
        await session.commit()
        log.info("reaped dead device rows user=%s count=%d", user_id, len(dead))


async def wake_for_message(*, channel_id: str, channel_kind: str, sender_id: str,
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
                allowed, _ = limiter.hit(
                    "apns_wake", user_id,
                    settings.apns_wake_per_recipient_per_minute, 60.0)
                if not allowed:
                    # Keyed on the RECIPIENT, so the budget protects the person
                    # being interrupted rather than throttling per sender — which
                    # a second sender would simply route around.
                    log.warning("wake throttled user=%s channel=%s", user_id, channel_id)
                    continue
                await _wake_user(session, user_id, payload, collapse_id=channel_id)
    except Exception:
        # Deliberately broad. This runs detached in a background task, where an
        # escaping exception is logged by asyncio at GC time (or lost) rather than
        # surfacing anywhere useful — and there is nothing above to handle it.
        log.exception("wake failed channel=%s", channel_id)


def schedule_wake(*, channel_id: str, channel_kind: str, sender_id: str,
                  body: str, exclude_user_ids: set[str]) -> None:
    """Fire-and-forget the wake. THE SEND PATH MUST NOT WAIT ON APPLE.

    A push is a round trip to Apple's servers. Awaiting it inline would put that
    latency — and its failure modes — between the sender pressing call and their
    own client's acknowledgement, making the caller's experience hostage to the
    callee's notification transport. So the wake runs after the message is
    already durable and already fanned out, on its own task and its own session.

    Cheap short-circuit before scheduling anything: on an island with no APNs
    credentials (every island today) this is a predicate call and no task at all.
    """
    if not apns.is_configured() or not should_wake(channel_kind, body):
        return
    task = asyncio.create_task(wake_for_message(
        channel_id=channel_id, channel_kind=channel_kind, sender_id=sender_id,
        body=body, exclude_user_ids=exclude_user_ids))
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)
