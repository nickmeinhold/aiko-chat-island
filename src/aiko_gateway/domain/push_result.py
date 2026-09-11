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

    ONE FIELD, so there is nowhere to put an identity. The doctrine above used to
    be a docstring on a dict-builder; it is now the shape of the type, which is
    the difference between a commitment and a comment. Each transport RENDERS
    this into its own envelope (`apns._render`, `fcm.build_message`) — the
    envelope is provider-specific and belongs below the boundary; the refusal is
    policy and belongs here.

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
