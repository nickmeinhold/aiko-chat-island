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

import dataclasses
import enum
import logging
import time

import httpx
import jwt

from ..config import settings
from .models import PushEnvironment

log = logging.getLogger("aiko_gateway.apns")

_PROD_HOST = "https://api.push.apple.com"
_SANDBOX_HOST = "https://api.sandbox.push.apple.com"

# Apple rejects a provider JWT older than 1 hour, AND rejects a provider that mints
# them more often than every 20 minutes. 50 minutes sits inside both bounds with
# room for clock skew in either direction — the window is genuinely two-sided, so a
# "refresh every request" implementation is not merely wasteful, it is rejected.
_TOKEN_REFRESH_SECONDS = 50 * 60

# How long APNs may keep trying to deliver. A call is PERISHABLE in a way an
# ordinary notification is not: a ring that surfaces ten minutes late is worse than
# no ring at all, because the recipient reaches for a call that has already ended
# and cannot tell that from a call they fumbled. So we let APNs DISCARD rather than
# store-and-forward. 60s is deliberately longer than the app's 10s ring-freshness
# gate: the two clocks answer different questions (that one decides whether to RING,
# this one decides whether the wake is still worth delivering at all), and a wake
# arriving at 30s still usefully says "you just missed something in here".
_EXPIRATION_SECONDS = 60

# The provider token, cached across sends: (jwt, issued_at_monotonic).
_cached_token: tuple[str, float] | None = None
_client_singleton: httpx.AsyncClient | None = None


class ApnsNotConfigured(RuntimeError):
    """This island has no APNs credentials — push is not enabled on this
    deployment. An expected operator state, never a bug: like LiveKit, an
    unconfigured optional capability is simply off."""


@dataclasses.dataclass(frozen=True, slots=True)
class SendResult:
    """What Apple said, plus the ONE piece of metadata the caller cannot re-derive.

    `verdict` alone was the original return type. Carnot killed that in cage-match
    #139 round 4: a 410 body carries a `timestamp` — the moment APNs confirmed the
    token was no longer valid — and Apple's documented rule is to resume pushing
    if the app has registered that token AGAIN since. Reducing the response to an
    enum threw that away, leaving the reaper unable to distinguish "this token is
    dead" from "this token WAS dead before the user reinstalled".

    For the one irreversible operation in the module, lost metadata is lost
    reversibility. `invalid_since_ms` is None for every verdict but DEAD_TOKEN,
    and may be None even then if Apple omits it — the caller must treat None as
    "no timestamp evidence", never as "invalid since the epoch".
    """

    verdict: "Verdict"
    invalid_since_ms: int | None = None


class Verdict(enum.Enum):
    """What Apple said, reduced to what the CALLER can act on.

    Deliberately coarser than APNs' reason strings, and the coarsening is the
    point: the only decision downstream is "delete this row or keep it", and
    every extra distinction is a chance to delete a row we should have kept.
    """

    DELIVERED = "delivered"
    # The device is GONE — the app was uninstalled or the token permanently
    # invalidated. Apple states this positively (410 Unregistered). Safe to reap.
    DEAD_TOKEN = "dead_token"
    # Apple refused, but for a reason that may well be OURS (bad topic, bad
    # provider key, wrong environment). NEVER reap on this — see `_verdict`.
    REJECTED = "rejected"
    # Network error, 429, or a 5xx. The device may be perfectly fine.
    #
    # DROPPED, DELIBERATELY — there is no retry, and that is a decision rather
    # than an omission (cage-match #139: the member read as though it fed a retry
    # loop that does not exist, sending the next reader looking for one). A wake
    # carries `apns-expiration` of 60s because a ring that surfaces late is worse
    # than no ring; a retry that outlives that window delivers nothing, and one
    # inside it would have to fire within seconds of a failure Apple is already
    # rate-limiting. So a transient failure means this particular call does not
    # ring — the message itself is durable and the recipient still sees it on
    # next open. If retry is ever added it belongs here with a deadline derived
    # from the same expiration constant, not a generic backoff.
    TRANSIENT = "transient"


def is_configured() -> bool:
    """True iff every APNs credential is present. Settings enforces all-or-none at
    boot, so in practice this is all-four-or-zero; the `all()` is still written out
    rather than testing one field, because a future partial-config bug should turn
    push OFF rather than half-on."""
    return all((settings.apns_key_id, settings.apns_team_id,
                settings.apns_topic, settings.apns_private_key))


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


def _host(push_environment: PushEnvironment) -> str:
    """The APNs host for ONE token's environment (#3386).

    Reads the TOKEN's environment, never the island's `apns_use_sandbox` — that
    flag is now only the default applied at REGISTRATION (see
    devices_service.default_push_environment) and has no say at send time. A box
    can therefore serve a debug build and a TestFlight build at once, which a
    single global switch made impossible.

    Takes the ENUM, not a str (cage-match, Carnot MEDIUM): a `str` signature lets
    every caller pass 'prod' or 'production ' and pushes the whole closed set back
    onto a runtime check. The conversion from the stored column happens once, at
    the push_service call site (the ORM edge), so the invariant is established in
    one place instead of re-defended at each use.

    The `case _` arm still raises rather than falling back to a default. It is
    reachable only via a new PushEnvironment member added without teaching this
    function about it — the corrupted-row path now fails earlier, at the enum
    conversion. Both are bugs, and guessing a host would hand a live credential to
    the wrong world. push_service treats a raising send as transient-and-skip, so
    neither can take down a fanout.
    """
    match push_environment:
        case PushEnvironment.SANDBOX:
            return _SANDBOX_HOST
        case PushEnvironment.PRODUCTION:
            return _PROD_HOST
        case _:
            raise ValueError(
                f"unknown APNs push_environment: {push_environment!r}")


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


async def send(device_token: str, payload: dict, *,
               push_environment: PushEnvironment,
               collapse_id: str | None = None) -> SendResult:
    """Push one payload to one device, in THAT DEVICE's APNs environment (#3386 —
    ``push_environment`` is required, with no default, so no caller can silently
    fall back to a global switch). Returns a [SendResult]; never raises for a
    protocol-level refusal — a failed push must not be able to fail the message
    send that triggered it (see `push_service.wake`).

    Returns a [SendResult]. Raises [ApnsNotConfigured] only if called on an island with no credentials,
    which is a caller bug: `push_service` gates on `is_configured()` first.
    """
    if not is_configured():
        raise ApnsNotConfigured("APNs credentials are not set on this island")

    headers = {
        "authorization": f"bearer {_provider_token()}",
        "apns-topic": settings.apns_topic,
        # `alert` (not `voip`): a VoIP push on iOS 13+ MUST synchronously report an
        # incoming call to CallKit or the system kills the app and eventually stops
        # delivering VoIP pushes entirely. Taking PushKit means taking mandatory
        # CallKit with it. Apple's own documented alternative is exactly this — a
        # UserNotifications alert — and it fits "a call is a gathering" better than
        # a ring does: a gathering has a door that stays open and needs no 30-second
        # synchronous window. See claude-tasks#3267.
        "apns-push-type": "alert",
        # 10 = deliver immediately. The alternative (5) permits Apple to hold the
        # push to save power, which for a perishable ring is the wrong trade.
        "apns-priority": "10",
        "apns-expiration": str(int(time.time()) + _EXPIRATION_SECONDS),
    }
    if collapse_id is not None:
        # Two rings for the same conversation should REPLACE, not stack: the second
        # notification is not new information, and a lock screen holding four
        # identical wakes reads as a malfunction. Apple caps this at 64 bytes.
        headers["apns-collapse-id"] = collapse_id[:64]

    # Resolved BEFORE the request: an unknown environment must fail here, not send.
    url = f"{_host(push_environment)}/3/device/{device_token}"
    try:
        response = await _client().post(url, json=payload, headers=headers)
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
    # model note) and never the provider key.
    log.warning("apns refused status=%s reason=%s verdict=%s",
                response.status_code, reason, verdict.value)
    return SendResult(verdict, invalid_since_ms)
