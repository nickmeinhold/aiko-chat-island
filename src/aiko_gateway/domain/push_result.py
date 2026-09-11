"""The vocabulary BOTH push transports speak — verdicts, reap orders, payloads.

Extracted from `apns.py` when FCM arrived, because a shared vocabulary that lives
inside one of its speakers is not shared: the second transport would either import
the first (breaking the "each transport is pure and knows nothing of the other"
layering) or grow a parallel result type that `push_service` would have to union.

DELIBERATELY DEPENDENCY-FREE. Stdlib only — no `settings`, no ORM, no `httpx`. It
is imported by both transports and by the policy layer, so anything it drags in
is dragged into all three; and the suite's isolation invariant (`import
aiko_gateway.main` must work with `aiko_services` absent) is cheapest to keep by
a module that cannot import anything interesting in the first place.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import enum


class Verdict(enum.Enum):
    """What the push service said, reduced to what the CALLER can act on.

    Deliberately coarser than either provider's reason strings, and the
    coarsening is the point: the only decision downstream is "delete this row or
    keep it", and every extra distinction is a chance to delete a row we should
    have kept.
    """

    DELIVERED = "delivered"
    # The device is GONE — the app was uninstalled or the token permanently
    # invalidated. Each transport states this positively and NARROWLY (APNs:
    # 410 Unregistered; FCM: an FcmError detail of UNREGISTERED). Safe to reap.
    DEAD_TOKEN = "dead_token"
    # The provider refused, but for a reason that may well be OURS (bad topic,
    # bad provider key, wrong environment, wrong Firebase project, a malformed
    # message). NEVER reap on this — see each transport's `_verdict`.
    REJECTED = "rejected"
    # Network error, 429, or a 5xx. The device may be perfectly fine.
    #
    # DROPPED, DELIBERATELY — there is no retry, and that is a decision rather
    # than an omission (cage-match #139: the member read as though it fed a retry
    # loop that does not exist, sending the next reader looking for one). A wake
    # expires within a minute because a ring that surfaces late is worse than no
    # ring; a retry that outlives that window delivers nothing, and one inside it
    # would have to fire within seconds of a failure the provider is already
    # rate-limiting — Google documents a MINIMUM 10s initial backoff, which
    # cannot land inside a perishable ring window at all. So a transient failure
    # means this particular call does not ring; the message itself is durable and
    # the recipient still sees it on next open. If retry is ever added it belongs
    # here with a deadline derived from the expiration constants, not a generic
    # backoff.
    TRANSIENT = "transient"


@dataclasses.dataclass(frozen=True, slots=True)
class ReapOrder:
    """PERMISSION TO DELETE ONE ROW, issued by the transport that observed the
    death. Its ABSENCE (`SendResult.reap is None`) is the refusal.

    WHY THE EVIDENCE MOVED DOWN HERE. The reaper used to hold `invalid_since_ms`
    and refuse to delete when it was None. That refusal encodes APPLE's
    documented rule — a 410 carries the moment APNs confirmed the token invalid,
    and Apple says to resume pushing if the app registered that token AGAIN since
    — so it is a fact about ONE provider sitting in shared code. Left there, FCM
    inherits a rule its protocol cannot feed (an UNREGISTERED response carries no
    timestamp at all) and its reaper could never fire. A true sentence, filed
    against the wrong owner.

    So each transport now decides whether the death is provable and hands over an
    order, or does not. The reaper's remaining question is the one it can answer
    for everybody: is this still the row we sent to?

    `not_reregistered_since=None` inside an order means "this transport dates
    nothing; the compare-and-delete on (id, token, updated_at) carries the
    reversibility alone". That is weaker than the APNs case and the weakness is
    stated rather than hidden.
    """

    # NO DEFAULT, DELIBERATELY (Tesla, cage-match PR#172 r5). This field used to
    # default to None — and None here means "delete with NO date arm", the
    # DESTRUCTIVE reading. So `ReapOrder()` constructed permission to delete
    # unboundedly out of silence, on the one irreversible operation in the module,
    # and was byte-identical to the deliberate `ReapOrder(None)`. No fixture could
    # tell a caller who MEANT no date from one who FORGOT to pass one.
    #
    # An earlier round named that residual in a docstring and left it there. This
    # PR is the proof that naming is not gating: `fcm.py` carried a HARD GATE in
    # capitals and the credential was provisioned on both islands anyway, hours
    # later, by someone who had read it. Same lesson one layer down — the default
    # is gone, `ReapOrder()` is a TypeError, and a dateless order must be written
    # `ReapOrder(None)` on purpose. That is exactly the intent worth requiring.
    not_reregistered_since: dt.datetime | None


@dataclasses.dataclass(frozen=True, slots=True)
class SendResult:
    """What one transport said about one device, plus its reaping decision.

    For the one irreversible operation in the push path, lost metadata is lost
    reversibility — which is why `reap` is a whole object rather than a bool: the
    difference between "delete it" and "delete it only if it has not been
    re-registered since 12:04:31Z" is exactly the difference between a reaper and
    an outage.
    """

    verdict: Verdict
    reap: ReapOrder | None = None


class WakeKind(enum.Enum):
    """WHAT KIND OF WAKE this is — the thing `push_service.should_wake` decides,
    carried as a value instead of re-derived four hundred lines away.

    A plain `Enum`, not a `StrEnum`: in this codebase a StrEnum means "persisted in
    a COLUMN, and drives a DB CHECK via `_in_check`". This is not.

    THAT IS A STATEMENT ABOUT WHICH MECHANISM ENFORCES THE SET, NOT ABOUT A
    LIGHTER COMPATIBILITY BURDEN (Kelvin, cage-match PR#176 r1: "the wire IS a
    persistence layer, its state is just frozen somewhere else"). He is right, and
    the burden here is arguably HEAVIER than a column's: a bad column value is one
    island's migration, while a bad wire value is already inside handsets we cannot
    reach. No `_in_check` guards this one — the paragraph below is the enforcement,
    and it is prose, so read it as a warning rather than a fence.

    IT NOW CROSSES THE WIRE, WHICH IT DID NOT BEFORE, and that is a deliberate
    change rather than a drift — it is why this type moved out of `push_service`
    and into the module both transports already speak. `.value` is rendered into
    the push envelope as `"k"`, so **these strings are a compatibility surface**:
    editing a member's value silently changes what a handset receives, the same
    one-way-door property `CALL_INVITE_BODY` has. Add members; never edit values.

    WHY THIS IS NOT CEREMONY. "Every push this module can emit is a call invite"
    was true only because `is_call_invite` — an EXACT equality against the pinned
    sentinel — gated both entry points, far from the header that decides
    `apns-push-type`. Threading the kind makes it a DATA-FLOW fact: the router
    emits a VoIP delivery only from a `WakeKind` it was handed, and the only
    supply is that gate.
    """

    CALL_INVITE = "call_invite"
    # design 12 Decision 5, claude-tasks#4254. The caller hung up. Routed to VoIP
    # rows ONLY — see `push_service.plan_deliveries` for why an alert row is a
    # skip and not a send.
    CALL_END = "call_end"

    # THE CLIENT'S TOTAL FUNCTION ON `"k"` — a SHARED invariant, written down on
    # this side too rather than left as the app's implementation detail (app tab,
    # 2026-09-11, and it is their specification, not ours):
    #
    #   "call_invite"        -> report to CallKit, verify, sustain or end
    #   "call_end"           -> report (must-report is unconditional), then end
    #   unknown OR missing   -> report, then IMMEDIATELY end. NEVER sustain.
    #
    # THAT LAST LINE IS WHY A THIRD MEMBER IS SAFE TO ADD. Without it, an island
    # running ahead of a handset could turn any new wake kind into a ring that
    # nothing stops on the old build. With it, an unrecognised kind degrades to a
    # momentary CallKit cell — a malformed-input failure mode rather than a
    # destination — so this enum can grow without the wire needing a version.
    #
    # It lives HERE, at the definition of the values it ranges over, because a
    # rule recorded only in the repo that implements it is a rule the other repo
    # can silently stop honouring.


@dataclasses.dataclass(frozen=True, slots=True)
class WakePayload:
    """The wake, as POLICY states it — DELIBERATELY OPAQUE, and the opacity is
    the feature.

    A push provider is an intermediary we cannot remove, and it can read
    everything we send it. A payload saying "Alice is calling you" would tell
    Apple or Google who calls whom, on a product whose entire thesis is that such
    facts stay with the operator. So the push carries a wake and a destination,
    never an identity: the provider learns that a device was woken and when —
    timing and frequency — but not by whom.

    The cost, stated honestly rather than hidden: the notification the user sees
    on the lock screen cannot name the caller either, because the app has not yet
    spoken to the island when the OS renders it. Naming the caller would require
    either putting the name in this payload (the thing we are refusing) or
    resolving it on-device before display (real, and design 12 Decision 6's
    Swift-readable cache is exactly that — client-side, and the island owes
    nothing for it).

    `channel_id` is the one identifier included. It is what makes the tap land in
    the right conversation, and it is stable — so a provider can correlate
    repeated wakes for the same conversation over time. That is a genuine
    residual, judged worth the deep link; it is not a claim that the payload
    leaks nothing.

    NO FIELD HERE IS AN IDENTITY. The doctrine above used to be a docstring on a
    dict-builder; it is now the shape of the type, which is the difference
    between a commitment and a comment. Each transport RENDERS this into its own
    envelope (`apns._render`, `fcm.build_message`) — the envelope is
    provider-specific and belongs below the boundary; the refusal is policy and
    belongs here.

    IT SAID "ONE FIELD" UNTIL 2026-09-11, and the second field is `kind`. The
    count was never the commitment — it was a true description of the type on a
    day when one field sufficed, and the paragraph below already warned that
    reading it as the commitment forecloses more than the decision does. The
    commitment is the READER property, unchanged.

    WHY `kind` DOES NOT SPEND IT (claude-tasks#4254, design 12 Decision 5). The
    end wake exists so a hangup can stop a ring on a locked handset. Every
    PushKit push must be reported to CallKit before the handler returns, so a
    stop that arrives in the same shape as a start IS a start — the app has no
    way to tell them apart and rings again. The field is what makes the stop
    stoppable.

    And it tells Apple nothing the sequence did not already tell them: the moment
    an end wake is sent at all, they see two wakes for the same channel forty
    seconds apart and can read the call duration off the timing. `"k"` names what
    was already legible. It still names no person and no direction.

    WHAT THAT LAST SENTENCE DEFENDS, SAID EXPLICITLY, because its phrasing can
    foreclose more than the decision behind it does. "Nowhere to put an identity"
    is a true description of the type as it stands today and a MECHANISM, not the
    property. The property is the paragraph at the top and it is about a READER:
    the provider must not learn who calls whom. Design 12 Decision 6 states it
    the same way — "nothing about who-calls-whom ON APPLE'S WIRE".

    The difference matters for one live question (claude-tasks#4254, and the app
    tab's design 20). An envelope SEALED TO THE CALLEE would put bytes in this
    payload that are an identity to the recipient and ciphertext to Apple. Under
    the structural reading that is excluded by wording; under the property it is
    not excluded at all, because the provider learns nothing it did not already
    learn from the wake itself. A future reader should not conclude from "ONE
    FIELD" that the question is already settled — it is open, it belongs to Nick,
    and the crypto in it needs a specialist (claude-tasks#4185).

    Nothing here is a decision to carry such an envelope. It is a statement of
    which property this type is protecting, so that whoever decides is deciding
    the real thing rather than arguing with a sentence.
    """

    channel_id: str
    # REQUIRED, WITH NO DEFAULT, and that is the whole safety argument. A default
    # of CALL_INVITE would let a caller that forgets the argument render a hangup
    # as a ring — the exact "stop becomes a start" failure this field exists to
    # prevent, reintroduced as a silent fallback. The type makes forgetting a
    # TypeError instead.
    kind: WakeKind
