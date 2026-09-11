"""APNs transport — the wire to Apple, and nothing else (#3267 increment 2).

This module knows how to hand ONE payload to APNs for ONE device token and report
what Apple said. It holds no policy: who may be woken, what the payload may
contain, and whether a dead row should be deleted are `push_service`'s decisions.
Splitting them this way keeps the security-relevant reasoning in one file and the
protocol chores in another.

WHAT APNs IS, IN THIS SYSTEM'S TERMS. Apple is the only party that can reach a
suspended iOS app — there is no third-party push on the platform. So APNs is a
mandatory intermediary that buys exactly one thing: REACH to a handset we cannot
otherwise address. That is the same shape as an SFU relaying for a browser behind
a NAT, and it is worth naming because it bounds what we should send: an
intermediary we cannot remove should learn as little as possible (see
`push_service` for the payload's deliberate opacity).

AUTHENTICATION is a short-lived ES256 JWT signed by the .p8 provider key —
`iss` = Team ID, `kid` = Key ID, and a bare `iat`. Note there is NO `exp`: Apple
validates the token's age itself and rejects one older than an hour. Apple also
rejects a provider that mints them TOO OFTEN, so the token is cached and reused
across sends (see `_TOKEN_REFRESH_SECONDS`) rather than signed per push.

HTTP/2 IS NOT OPTIONAL. APNs speaks only HTTP/2; httpx negotiates it via the `h2`
extra, which is why the dependency is `httpx[http2]`. A missing extra is exactly
the kind of thing that survives CI on a warm venv and dies on a fresh deploy, and
its symptom here would be "calls stopped ringing" — so `_client()` checks for it
explicitly and raises a message that names the cause.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
from typing import assert_never

import httpx
import jwt

from ..config import settings
from .models import ApnsEnvironment, TokenKind
# A `from X import Y` binding, deliberately: `apns.SendResult` and `apns.Verdict`
# keep working and keep referring to the SAME objects, so every existing caller
# and test is untouched by the move (`is` comparisons hold). The types themselves
# moved to `push_result` when FCM arrived — a shared vocabulary living inside one
# of its two speakers is not shared.
from .push_result import (ReapOrder, SendResult, Verdict, WakeKind,
                          WakePayload)

log = logging.getLogger("aiko_gateway.apns")

_PROD_HOST = "https://api.push.apple.com"
_SANDBOX_HOST = "https://api.sandbox.push.apple.com"

# A device token is a CREDENTIAL, and APNs puts it in the URL PATH — so httpx's
# own request logging writes the whole thing at INFO on every send. We never wrote
# a `log.` call containing a token; it arrives through a dependency's logger, which
# is exactly why it went unnoticed until someone read a real log (claude-tasks#3586).
#
# DELIBERATELY A REDACTION, NOT A SILENCE. Setting the httpx logger to WARNING would
# remove the leak and the observability together — and that line is the ONLY direct
# evidence of which Apple host a given row was sent to. It is what witnessed
# `_host()` routing per row on 2026-08-29, the first production proof of #3386's
# central claim. push_service also logs its own row-keyed line now, but this one
# stays legible on purpose.
#
# MATCHES ANY LONG HEX RUN, not the `/3/device/<token>` path specifically. Anchoring
# to today's APNs URL shape would let a renamed path leak straight through, and the
# generic form also covers any other secret-shaped hex a future httpx call might
# carry. Over-redaction risk is accepted: a >=32-char hex run in a URL is a token
# far more often than it is anything a reader needs whole.
_LONG_HEX = re.compile(r"\b([0-9a-fA-F]{12})[0-9a-fA-F]{20,}\b")


class _RedactLongHex(logging.Filter):
    """Trim any >=32-char hex run in a log record to its first 12 chars + '...'.

    12 hex is 48 bits — nowhere near enough to reconstruct a 256-bit device token,
    and empirically plenty to correlate a log line with a DB row (`substr(token,1,12)`
    disambiguated instantly against the live table while debugging #3386).

    Works on the FORMATTED message and then clears `args`, because httpx logs with
    %-args rather than a pre-built string; redacting `record.msg` alone would leave
    the token sitting in `record.args` for any other handler to format back out.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken record must not kill a send
            return True
        redacted = _LONG_HEX.sub(r"\1...", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True  # never drop the record; this filter only rewrites


def _already_filtered(target) -> bool:
    """Idempotence (cage-match PR#148, Carnot LOW). `addFilter` is called at import
    and again from `install_log_redaction`; a module reload in a test would stack
    duplicates. Harmless in effect — the first pass shortens the token below the
    threshold — but a function that mutates GLOBAL logger state should be safe to
    call twice, not merely survivable."""
    return any(isinstance(f, _RedactLongHex) for f in target.filters)


def install_log_redaction() -> None:
    """Attach the redaction to the ROOT HANDLERS, which is the version that holds.

    A filter on a LOGGER only runs for records logged DIRECTLY to it — verified, not
    assumed: a filter on "demo" does not see "demo.child". httpx today logs under
    exactly `getLogger("httpx")` (one call site, `_client.py`), so the import-time
    attachment below works — but it guards THE LEAK WE FOUND rather than the class,
    which is precisely the shape of the bug it exists to fix. This one leaked in
    through a dependency's logger nobody had thought about; the next one will too.

    A filter on a HANDLER sees every record that reaches it, whatever logger emitted
    it (also verified). So this covers a future `httpx.client` submodule logger, and
    any other library that ever puts a credential in a URL.

    Called from main.py AFTER `logging.basicConfig`, because the root handler does
    not exist before then. The import-time attachment stays as well: it costs nothing
    and keeps any entrypoint that imports this module without going through main.py
    (a script, a worker, the test suite) covered.
    """
    for handler in logging.getLogger().handlers:
        if not _already_filtered(handler):
            handler.addFilter(_RedactLongHex())


# Installed at IMPORT rather than at client construction: the filter has to be in
# place before anything can log, and `_client()` is built lazily inside the first
# send. See `install_log_redaction` for why this is the narrower of the two layers.
if not _already_filtered(logging.getLogger("httpx")):
    logging.getLogger("httpx").addFilter(_RedactLongHex())

# Apple rejects a provider JWT older than 1 hour, AND rejects a provider that mints
# them more often than every 20 minutes. 50 minutes sits inside both bounds with
# room for clock skew in either direction — the window is genuinely two-sided, so a
# "refresh every request" implementation is not merely wasteful, it is rejected.
_TOKEN_REFRESH_SECONDS = 50 * 60

# The VoIP topic is the bare topic plus this suffix — an Apple PROTOCOL FACT, not
# an inference: `<bundle>.voip` is one of a small set of suffixed namespaces on a
# single app record (`.voip`, `.complication`, `.pushkit.fileprovider`,
# `.location-query`), selected per request by the header. Not a second app, not a
# second App ID, not a second credential.
#
# RULED 2026-09-10 (Nick): the VoIP topic is a STATED setting, not a derived
# one — see `_topic_for` and `config.apns_voip_topic`.

# How long APNs may keep trying to deliver an ALERT wake. A call is PERISHABLE in a
# way an ordinary notification is not: a ring that surfaces ten minutes late is
# worse than no ring at all, because the recipient reaches for a call that has
# already ended and cannot tell that from a call they fumbled. So we let APNs
# DISCARD rather than store-and-forward.
#
# SCOPED TO THE ALERT WORLD, and the scoping is the point (12a-MEASURED M4). The
# rationale that follows was written as a general rule and is not one: 60s is
# deliberately longer than the app's 10s ring-freshness gate because in the ALERT
# world the two clocks answer different questions — that one decides whether to
# RING, this one decides whether the wake is still worth delivering at all — and a
# late alert wake still usefully says "you just missed something in here". Under
# VoIP that same sentence describes a PHANTOM RING, so the constant forks rather
# than being inherited.
_ALERT_EXPIRATION_SECONDS = 60

# THE RING LEASE — Nick's ruling of 2026-09-09 (claude-tasks#3744), and the
# mechanism of record for it.
#
# The island owns the 30s ring ceiling. A CallKit ring is system UI drawn before
# Dart exists and does not self-expire; the app is suspendable the instant the
# report completes, so a client-side timer had no home. What keeps Decision 1's
# boundary intact ("the island never INFERS an end") is that this is not a claim
# about the call at all — the island expires ITS OWN PUSH, which is a fact about
# our delivery.
#
# 30 EQUALS the app's `kCallRingDuration`, and the equality IS the derivation the
# 12a answer asks for ("the lease and the ring should be derived from one another
# rather than picked"). A lease longer than the ring stores a push that reports a
# call already over; a lease shorter than the ring stops the ring reaching a phone
# that is still supposed to be ringing.
#
# NOT ZERO, though `apns-expiration: 0` (deliver-once-or-discard) is the obvious
# way to make a phantom ring structurally impossible. Zero DELETES the lease, and
# with it the ceiling mechanism the ruling names — and it converts a three-second
# tunnel into a permanently missed call, against this module's own doctrine that a
# duplicate notification is a blemish while a missed call is the bug.
#
# COUPLED HALF, OPEN AND NOT OURS TO CLOSE: the app's `admitRing` freshness gate is
# 10s, narrower than this lease. A stored VoIP push delivered at t=15s is admitted
# by APNs, MUST be reported to CallKit (design 12 Decision 4 — there is no
# on-device window in which to reconsider), and would then be refused by that gate:
# a report-and-end, which is the ratio Apple polices. The three clocks (this lease,
# the 30s ring, the 10s freshness) still have no stated relationship, which the
# ruling explicitly left open.
_VOIP_LEASE_SECONDS = 30

# THE END WAKE DOES NOT GET THE RING LEASE, and the asymmetry is the point
# (claude-tasks#4254, cage-match PR#176 r1). Every sentence of the comment above is
# about a RING — "phantom ring", "the lease", "the ceiling mechanism the ruling
# names". A hangup inherited that number because `token_kind` used to be the only
# axis that decided a push's lifetime, and as of the end wake it is not.
#
# THE TWO DIRECTIONS ARE NOT SYMMETRIC. Discarding a late INVITE is CORRECT: the
# recipient reaches for a call that already ended and cannot tell that from a call
# they fumbled. Discarding a late END is never correct — ending an already-ended
# call is idempotent and free, and the thing it fails to stop is a CallKit ring
# that design 12 states DOES NOT SELF-EXPIRE. At 30s the failure is ordinary:
# caller hangs up, the callee's handset is in a lift for thirty-one seconds, APNs
# discards the stop, the device returns to a ring nothing can end. That is the
# phantom ring this whole feature exists to prevent, re-entering through the
# expiration header instead of the missing sentinel.
#
# WHY 300 AND NOT UNBOUNDED. A stop must still plausibly refer to a ring the user
# is looking at. Unbounded store-and-forward would let a stop from an hour ago
# arrive and force a report-and-end — and that ratio is the one Apple polices
# (see `apns-push-type` below). Five minutes covers the real failure (a tunnel, a
# lift, a flapping connection) by an order of magnitude over the lease, and stops
# well short of ancient stops driving the ratio.
#
# THIS IS A FOURTH CLOCK AND IT IS NOT SETTLED HERE. The lease, the 30s ring and
# the app's 10s freshness gate already "have no stated relationship, which the
# ruling explicitly left open" — that is claude-tasks#4233, and it is where the
# four of them get reconciled. This constant is chosen to FAIL IN THE SAFE
# DIRECTION until then, not to be the answer.
_VOIP_END_EXPIRATION_SECONDS = 300

# The provider token, cached across sends: (jwt, issued_at_monotonic).
_cached_token: tuple[str, float] | None = None
_client_singleton: httpx.AsyncClient | None = None


class ApnsNotConfigured(RuntimeError):
    """This island has no APNs credentials — push is not enabled on this
    deployment. An expected operator state, never a bug: like LiveKit, an
    unconfigured optional capability is simply off."""


def is_configured() -> bool:
    """True iff every APNs credential is present. Settings enforces all-or-none at
    boot, so in practice this is all-FIVE-or-zero; the `all()` is still written out
    rather than testing one field, because a future partial-config bug should turn
    push OFF rather than half-on.

    `apns_voip_topic` IS ONE OF THE FIVE (Carnot, cage-match PR#172 r1). It was
    added to the settings all-or-none group and not to this predicate, so the
    docstring said "every APNs credential" while the tuple checked four of them —
    the summary drifting from the set it claims to summarise. No reachable state
    changes, because the boot validator already refuses four-of-five; that is
    exactly why the omission was invisible, and exactly why the written-out `all()`
    exists rather than a single-field test. A predicate defended by a guard
    elsewhere is still wrong when read on its own."""
    return all((settings.apns_key_id, settings.apns_team_id,
                settings.apns_topic, settings.apns_private_key,
                settings.apns_voip_topic))


def reset_for_tests() -> None:
    """Drop the cached provider TOKEN. Tests mutate settings between cases, and a
    token signed by the previous key would outlive them.

    Deliberately does NOT reset the pooled client. It once did, and that was a
    leak: nulling the reference abandons a live HTTP/2 connection to Apple
    without closing it (this function is sync and cannot await `aclose`), which
    showed up as a two-minute hang at interpreter exit rather than as any failing
    test. The reset was also unnecessary — the sandbox/production choice lives in
    the per-send URL, not in the client — so one pooled client stays valid across
    every settings change a test can make. Removing the coupling beats closing
    the window.
    """
    global _cached_token
    _cached_token = None


def _provider_token() -> str:
    """The cached ES256 provider JWT, minted on first use and every ~50 minutes.

    Signing is CPU-cheap but not free, and the refresh floor above means a
    per-request mint is actively wrong, not just wasteful."""
    global _cached_token
    now = time.monotonic()
    if _cached_token is not None and now - _cached_token[1] < _TOKEN_REFRESH_SECONDS:
        return _cached_token[0]
    if not is_configured():
        raise ApnsNotConfigured("APNs credentials are not set on this island")
    token = jwt.encode(
        # `iat` is wall-clock seconds — Apple compares it to ITS clock, so
        # time.time() is correct here even though the cache above uses monotonic
        # (which is correct for measuring OUR elapsed interval). Two clocks, two
        # jobs; using either for both is a bug in one direction or the other.
        {"iss": settings.apns_team_id, "iat": int(time.time())},
        settings.apns_private_key,
        algorithm="ES256",
        headers={"kid": settings.apns_key_id, "alg": "ES256"},
    )
    _cached_token = (token, now)
    return token


def _host(apns_environment: ApnsEnvironment) -> str:
    """The APNs host for ONE token's environment (#3386).

    Reads the TOKEN's environment, never the island's `apns_use_sandbox` — that
    flag is now only the default applied at REGISTRATION (see
    devices_service.default_apns_environment) and has no say at send time. A box
    can therefore serve a debug build and a TestFlight build at once, which a
    single global switch made impossible.

    Takes the ENUM, not a str (cage-match, Carnot MEDIUM): a `str` signature lets
    every caller pass 'prod' or 'production ' and pushes the whole closed set back
    onto a runtime check. The conversion from the stored column happens once, at
    the push_service call site (the ORM edge), so the invariant is established in
    one place instead of re-defended at each use.

    The `case _` arm still raises rather than falling back to a default. It is
    reachable only via a new ApnsEnvironment member added without teaching this
    function about it — the corrupted-row path now fails earlier, at the enum
    conversion. Both are bugs, and guessing a host would hand a live credential to
    the wrong world. push_service treats a raising send as transient-and-skip, so
    neither can take down a fanout.
    """
    match apns_environment:
        case ApnsEnvironment.SANDBOX:
            return _SANDBOX_HOST
        case ApnsEnvironment.PRODUCTION:
            return _PROD_HOST
        case _:
            raise ValueError(
                f"unknown APNs apns_environment: {apns_environment!r}")


def _topic_for(token_kind: TokenKind) -> str:
    """The APNs topic for ONE token's kind — both STATED, neither inferred.

    STATED, per design 12 Decision 3 and Nick's ruling of 2026-09-10. An earlier
    revision derived the VoIP topic as `apns_topic + ".voip"`, on the argument that
    the suffix is Apple's definition rather than our guess. That is true and it was
    not the deciding fact: `config.py` already says of `apns_topic` that a device
    token "is only valid for the topic it was issued under, so a wrong topic is a
    silent 400 for every send, and it must be stated, not inferred" — and that
    sentence applies to a VoIP token unchanged. A value an operator can read is a
    value an operator can fix; a derived one is only visible in this file.

    The cost of stating it was real and is paid rather than dodged: the field joins
    the all-or-none guard in `config.py`, so an existing box carrying four of five
    keys would refuse to boot. `deploy/preflight-apns.sh` gained the same key in the
    same change, which turns that into a deploy that aborts before touching the
    running stack. The guard and its preflight are one mechanism in two files and
    must always move together.
    """
    match token_kind:
        case TokenKind.ALERT:
            return settings.apns_topic
        case TokenKind.VOIP:
            return settings.apns_voip_topic
        case _:
            assert_never(token_kind)


def _render(payload: WakePayload) -> dict:
    """The APNs envelope for a wake. BELOW the transport boundary, because the
    envelope is Apple's shape and only Apple's — `{"aps": {...}}` means nothing to
    FCM, and a policy layer that builds one transport's schema is a policy layer
    that will need a switch the day a second one arrives.

    What crosses the boundary is `WakePayload`, which carries the REFUSAL (a wake
    and a destination, never an identity) and no provider schema at all.

    SHORT KEYS — `"c"`, `"k"` rather than `"channel_id"`, `"kind"`: an APNs alert
    payload has a 4KB ceiling (VoIP is 5KB) and these are the only custom fields,
    so there is no reason to spend bytes on long names. That reasoning is
    APNs-specific and stays here with the renderer; FCM's own ceiling is a
    different number about a different envelope.

    `"k"` IS ON EVERY WAKE, BOTH VALUES EXPLICIT — never "absent means invite".
    An absent key and a key whose value is the default are different epistemic
    states that an absence-default collapses into one: the client could not tell
    "this island predates the end wake" from "this island sent a ring", and the
    two want opposite handling. Explicit on both means a MISSING `"k"` is a
    detectable defect rather than a silently inherited default.

    THE ALERT COPY IS THE INVITE'S, AND ONLY VoIP ROWS EVER SEE A `CALL_END`.
    A VoIP push is delivered to the app and never displayed, so `aps.alert` is
    inert for it — which is why a hangup does not need its own wording here. The
    routing decision that keeps it that way lives in `push_service.plan_deliveries`
    (`end_wake_needs_voip`), NOT in this function; if that ever changes, this copy
    becomes a lie on screen and this paragraph is the note saying so.
    """
    return {
        "aps": {
            "alert": {"title": "Incoming call", "body": "Tap to join"},
            "sound": "default",
        },
        "c": payload.channel_id,
        "k": payload.kind.value,
    }


def _client() -> httpx.AsyncClient:
    global _client_singleton
    if _client_singleton is None:
        # FAIL LOUDLY AND EARLY if the http2 extra is missing. `httpx.AsyncClient(
        # http2=True)` raises ImportError itself, but only on CONSTRUCTION deep in a
        # background wake task where the traceback goes to a log nobody reads, and
        # the symptom is "calls don't ring" — a silent capability loss. Naming it
        # here makes the deploy-time cause legible from the message alone.
        try:
            import h2  # noqa: F401
        except ImportError as ex:  # pragma: no cover - depends on install shape
            raise RuntimeError(
                "APNs requires HTTP/2 (httpx[http2] -> h2), which is not installed. "
                "Push is configured on this island but cannot send. Reinstall "
                "dependencies: the extra is declared in pyproject."
            ) from ex
        # A connection pool is worth keeping: APNs rewards a long-lived HTTP/2
        # connection and penalises churn.
        _client_singleton = httpx.AsyncClient(http2=True, timeout=10.0)
    return _client_singleton


async def aclose() -> None:
    """Close the pooled client. Called from the app lifespan on shutdown."""
    global _client_singleton
    if _client_singleton is not None:
        await _client_singleton.aclose()
        _client_singleton = None


def _verdict(status: int, reason: str) -> Verdict:
    """Map an APNs response to the one decision the caller must make.

    THE REAPING RULE, AND WHY IT IS NARROWER THAN IT LOOKS.

    The obvious implementation deletes a device row on `400 BadDeviceToken` as
    well as `410 Unregistered`, because both sound like "this token is no good".
    That is a live footgun, and it fires at the worst possible moment.

    `BadDeviceToken` is ALSO what Apple returns when the token is perfectly valid
    but was issued for the OTHER environment — a development-build token sent to
    the production host, or vice versa. A token carries no marking that says which
    environment it belongs to; the two are indistinguishable by inspection. So an
    operator who flips `apns_use_sandbox` the wrong way would, on the very first
    ring, receive `BadDeviceToken` for EVERY registered device and a naive reaper
    would delete the entire table. The recovery is not a config fix — every device
    must re-register, which requires every user to reopen the app, which is
    precisely what push exists to avoid needing.

    So: reap on `410 Unregistered` ONLY, where Apple is making a positive claim
    about the DEVICE ("no longer active for this topic") rather than a claim about
    a request that we may well have malformed. A token that is genuinely stale but
    never returns 410 costs one wasted HTTP request per send — a rounding error
    against deleting a live user's only path to being reached.

    This is the fail-safe direction for a REAPER specifically: a reaper that runs
    too eagerly destroys state, and destroyed state cannot be re-derived from
    anything the island holds. Failing closed here means NOT deleting.

    A 410 IS TOPIC-SCOPED, NOT DEVICE-SCOPED, and that matters now that one
    handset can hold two rows. "No longer active for this topic" is a claim about
    `<bundle>.voip` or about the bare bundle, never about the phone — so a 410 on
    a VoIP send reaps the VoIP row ONLY and says nothing about that handset's
    alert row. Already correct as built, because the reaper is row-id-scoped with
    a compare-and-delete; stated because a reader would otherwise assume device
    scope and "tidy" the reaper into deleting both.

    THE KIND/TOPIC MISMATCHES ALL LAND IN `REJECTED`, WHICH NEVER REAPS. An alert
    token sent to `.voip`, a VoIP token sent to the bare topic, a misspelled push
    type — every one is a 400, so a fork bug refuses every push and deletes
    nothing. That is the correct fail-safe direction, and it is also exactly why
    such a bug is SILENT: hence the ERROR level and the `kind=` field on the
    refusal log line in `send`.
    """
    if status == 200:
        return Verdict.DELIVERED
    if status == 410:
        # 410 Unregistered — the ONLY reaping condition, and the reason string is
        # deliberately not consulted: the status alone is Apple's positive claim.
        #
        # An earlier revision also reaped `400` carrying reason "Unregistered", as
        # "belt-and-braces for the same claim arriving with a different status".
        # Carnot killed it (cage-match #139) and was right on two counts. First it
        # CONTRADICTED THE DOCSTRING DIRECTLY ABOVE IT, which says 410 only — the
        # code and its own stated rule had drifted inside one function, which is
        # the worst place for a reader to have to adjudicate. Second, the
        # combination is undocumented by Apple and was untested, so it was an
        # UNVERIFIED WIDENING of the single operation in this module that destroys
        # state irrecoverably. Belt-and-braces is a fine instinct for a guard and a
        # bad one for a reaper: extra arms on a guard cost a false refusal, extra
        # arms on a reaper cost a deleted row that nothing can rebuild.
        return Verdict.DEAD_TOKEN
    if status == 429 or status >= 500:
        return Verdict.TRANSIENT
    return Verdict.REJECTED


async def send(device_token: str, payload: WakePayload, *,
               apns_environment: ApnsEnvironment,
               token_kind: TokenKind,
               collapse_id: str | None = None) -> SendResult:
    """Push one wake to one device, in THAT DEVICE's APNs environment and for
    THAT TOKEN's kind. Returns a [SendResult]; never raises for a protocol-level
    refusal — a failed push must not be able to fail the message send that
    triggered it (see `push_service.wake_for_message`).

    BOTH `apns_environment` AND `token_kind` ARE REQUIRED, KEYWORD-ONLY, WITH NO
    DEFAULT. The first was made that way by #3386 "so no caller can silently fall
    back to a global switch"; the second for the identical reason one axis over.
    A `token_kind=ALERT` default would let a caller that forgets the argument send
    an alert push to a VoIP-only handset, which is a 400 DeviceTokenNotForTopic →
    REJECTED → never reaped → one WARNING line → no ring. The two axes are
    ORTHOGONAL, not alternatives: a VoIP token has its own sandbox/production
    split and does not escape the environment question.

    Raises [ApnsNotConfigured] only if called on an island with no credentials,
    which is a caller bug: `push_service` gates on `is_configured()` first.
    """
    if not is_configured():
        raise ApnsNotConfigured("APNs credentials are not set on this island")

    # ONE MATCH BINDING FOUR FACTS THAT MUST NEVER DRIFT APART. Topic, push type,
    # lifetime and collapse-eligibility are not four independent settings; they
    # are one decision about what kind of push this is, and every mismatched pair
    # is a distinct silent failure (a voip type on a bare topic, an alert type on
    # a `.voip` topic, an alert lifetime on a ring). Deciding them in one place
    # means a future kind cannot be half-taught.
    # TWO AXES NOW, NOT ONE. The comment above used to say these four facts "are one
    # decision about what kind of push this is" — true while every VoIP push was an
    # invite, false as of the end wake (cage-match PR#176 r1). Topic, push type and
    # collapse-eligibility really are `token_kind`'s alone: they are facts about the
    # TRANSPORT. Lifetime is not — it is a fact about WHAT THE PUSH IS FOR, and a
    # stop and a start want opposite answers. Matching on the pair keeps the binding
    # the comment promises while letting the one genuinely two-axis fact vary.
    # EVERY CELL NAMED; NO WILDCARD ON A WAKE KIND (Carnot, cage-match PR#176 r2).
    # The first draft of this two-axis match wrote `(TokenKind.ALERT, _)` and
    # `(TokenKind.VOIP, _)`, which reads as tidy and is the exact silent-inheritance
    # hole the router two files up was rebuilt to close. A third `WakeKind` would
    # have inherited an alert push's 60s lifetime and collapse-eligibility here
    # WITHOUT ANYONE DECIDING THAT — the policy layer forcing a decision while the
    # transport quietly supplies a default. The finding is sharper than it looks
    # because this fix CREATED it: lifetime only became a decision worth guarding
    # when it stopped being a function of `token_kind` alone, one commit ago.
    #
    # NOT `assert_never` on the pair: tuple narrowing is unreliable and there is no
    # type checker in CI, so the fall-through has to be a REAL runtime arm — the
    # same reasoning `plan_deliveries` states for its own `case _`. Raising is safe
    # here: `_send_one` wraps each device in its own boundary, so an unrouted pair
    # costs that one device a logged skip rather than the fanout.
    match (token_kind, payload.kind):
        case (TokenKind.ALERT, WakeKind.CALL_INVITE):
            expires_in, may_collapse = _ALERT_EXPIRATION_SECONDS, True
        case (TokenKind.VOIP, WakeKind.CALL_INVITE):
            expires_in, may_collapse = _VOIP_LEASE_SECONDS, False
        case (TokenKind.VOIP, WakeKind.CALL_END):
            expires_in, may_collapse = _VOIP_END_EXPIRATION_SECONDS, False
        case (TokenKind.ALERT, WakeKind.CALL_END):
            # UNREACHABLE VIA THE DOOR, AND STILL WRITTEN OUT. `plan_deliveries`
            # skips this cell as `end_wake_needs_voip`, so the only way here is a
            # caller reaching past the router. Named rather than folded into the
            # refusal below because the reason is specific and worth reading: an
            # alert push runs no app code, so it cannot end a CallKit ring.
            raise ValueError(
                "an end wake cannot be sent to an alert token — an alert push runs "
                "no app code and cannot end a CallKit ring; see "
                "push_service.plan_deliveries (end_wake_needs_voip)")
        case _:
            raise ValueError(
                f"unrouted push: token_kind={token_kind} wake={payload.kind}. A new "
                "WakeKind must be given an explicit lifetime and collapse decision "
                "here, not inherit one.")

    headers = {
        "authorization": f"bearer {_provider_token()}",
        "apns-topic": _topic_for(token_kind),
        # THE PUSH TYPE, and the ONE non-derivable constraint that binds what this
        # island may send it for.
        #
        # Since iOS 13 Apple REQUIRES a VoIP push to be reported to CallKit before
        # the delivery handler returns, and documents that the system terminates an
        # app that does not — and that repeated violations stop VoIP delivery to
        # that installation ENTIRELY, while APNs keeps returning 200. An
        # island-side mistake would therefore produce a permanently deaf handset
        # with a green log line, invisible to `SendResult` forever.
        #
        # THAT PENALTY HAS NEVER BEEN OBSERVED HERE, and the honest statement of
        # why matters (12a-MEASURED M8). A four-push flagrant-violation arm went
        # unpunished on a real handset, but the negative control never fired — the
        # app was foregrounded by the launch harness, and must-report governs
        # waking a SUSPENDED app — so the result is VOID, not a licence.
        # `CSDVoIPApplicationKillCounts` in `com.apple.TelephonyUtilities` is the
        # per-app kill ledger that would make it readable (M10). What IS proven is
        # M7: CallKit rang from a VoIP push with no Dart alive, on a real handset.
        #
        # So the island emits VoIP ONLY for a genuine call invite — enforced
        # upstream by `push_service.should_wake`'s exact-sentinel match and by the
        # router accepting a VoIP delivery only from a WakeKind that gate produced.
        # The discipline does not rest on a measured penalty: we do not spend an
        # UNMEASURED budget.
        "apns-push-type": token_kind.value,
        # 10 = deliver immediately, correct for BOTH kinds. The alternative (5)
        # permits Apple to hold the push to save power, which for a perishable
        # ring is the wrong trade. The only documented hard priority coupling is
        # the inverse one: push-type `background` MUST be priority 5.
        "apns-priority": "10",
        "apns-expiration": str(int(time.time()) + expires_in),
    }
    if may_collapse and collapse_id is not None:
        # Two rings for the same conversation should REPLACE, not stack: the second
        # notification is not new information, and a lock screen holding four
        # identical wakes reads as a malfunction. Apple caps this at 64 bytes.
        #
        # THE TRANSPORT DECIDES, not the caller. Collapse identity is user-visible-
        # notification coalescing and a VoIP push displays nothing, so the header
        # is meaningless there at best. Whether APNs can REPLACE a QUEUED VoIP push
        # on the strength of it is UNVERIFIED, and a silently dropped ring is the
        # one cost this design cannot pay — CallKit's own call UUID de-duplicates
        # anyway. Keeping the decision here means one place knows the rule rather
        # than every call site having to remember it.
        headers["apns-collapse-id"] = collapse_id[:64]

    # Resolved BEFORE the request: an unknown environment must fail here, not send.
    url = f"{_host(apns_environment)}/3/device/{device_token}"
    try:
        response = await _client().post(url, json=_render(payload), headers=headers)
    except httpx.HTTPError as ex:
        # The device is not implicated by OUR network failing.
        log.warning("apns send failed transport=%s", type(ex).__name__)
        return SendResult(Verdict.TRANSIENT)

    if response.status_code == 200:
        return SendResult(Verdict.DELIVERED)

    invalid_since_ms: int | None = None
    try:
        body = response.json()
        reason = body.get("reason", "")
        # Apple sends `timestamp` in MILLISECONDS since the epoch on a 410.
        # Accepted only when it is genuinely an int: a malformed value must read
        # as "no evidence" (None) rather than coercing to a number that would
        # then authorise a delete. Fail toward keeping the row.
        ts = body.get("timestamp")
        if isinstance(ts, int) and not isinstance(ts, bool):
            invalid_since_ms = ts
    except ValueError:
        reason = ""
    verdict = _verdict(response.status_code, reason)
    # Log the reason but NEVER the device token (it is a device-held secret whose
    # confidentiality is the boundary protecting push routing — see the DeviceToken
    # model note) and never the provider key. `kind=` is carried because REJECTED
    # is the quietest failure in this system and every kind/topic mismatch lands
    # here: without it, telling a wrong-suffix bug from a wrong-environment bug
    # needs a packet capture.
    log.log(logging.ERROR if verdict is Verdict.REJECTED else logging.WARNING,
            "apns refused status=%s reason=%s kind=%s verdict=%s",
            response.status_code, reason, token_kind.value, verdict.value)
    return SendResult(verdict, _reap_order(verdict, invalid_since_ms))


def _reap_order(verdict: Verdict, invalid_since_ms: int | None) -> ReapOrder | None:
    """Whether Apple's answer PERMITS deleting the row, and under what condition.

    APPLE'S OWN RULE, not just our race guard (cage-match #139 round 4, Carnot).
    A 410 body carries the moment APNs confirmed the token invalid, and Apple says
    to resume pushing if the app registered that token AGAIN since. Without that
    timestamp there is no evidence separating "this token is dead" from "this
    token WAS dead before the user reinstalled and got the same token back", so
    the order is WITHHELD entirely — failing safe for a reaper means not deleting.

    This lives HERE, in the APNs transport, rather than in the shared reaper. The
    rule is a fact about Apple's protocol; shared, it silently governed a
    transport whose protocol cannot feed it (FCM's UNREGISTERED carries no
    timestamp), building a reaper that could never fire.
    """
    if verdict is not Verdict.DEAD_TOKEN or invalid_since_ms is None:
        return None
    return ReapOrder(dt.datetime.fromtimestamp(invalid_since_ms / 1000, tz=dt.UTC))
