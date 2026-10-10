"""FCM HTTP v1 transport — the wire to Google, and nothing else.

The Android sibling of `apns.py`, at EXACTLY the same layer: it knows how to hand
ONE wake to ONE registration token and report what Google said. It holds no
policy, and it never imports `push_service`, `models` or `apns` — the two
transports are siblings, not a chain. Its public silhouette mirrors `apns.py`
name for name so a reader of one can read the other, and every DIVERGENCE is
stated in a comment naming its APNs counterpart.

WHAT FCM IS, IN THIS SYSTEM'S TERMS. Google is the only party that can reach a
Doze-suspended Android app, so — exactly like APNs — it is a mandatory
intermediary bought for REACH and nothing else, and it should learn as little as
possible (see `push_result.WakePayload`).

THE THREE DIVERGENCES THAT MATTER, each of which is a silent failure if crossed
wrong:

  1. AUTHENTICATION IS A NETWORK EXCHANGE, not local signing. APNs signs an ES256
     JWT and sends THAT as the bearer; here an RS256 assertion is exchanged at
     Google's token endpoint for an opaque access token. That puts a second
     network dependency inside the send path, which can fail independently of the
     send — so it must never raise, and a hard rejection is negative-cached.
  2. `android.ttl` IS RELATIVE where `apns-expiration` IS ABSOLUTE. Reusing the
     APNs value emits a LEGAL ~56,000-year duration, clamped to Google's four-week
     ceiling, with no error anywhere.
  3. REAPING KEYS ON THE `FcmError` DETAIL, never the HTTP status. A wrong
     PROJECT_ID is a 404 for EVERY device on the island.

A CALL WAKE IS DATA-ONLY, AND THAT IS A CORRECTNESS PROPERTY. See `build_message`.

HTTP/2 IS NOT REQUIRED HERE. That is an APNs constraint, not a shared one — do
not copy `apns._client`'s `import h2` probe across; plain HTTP/1.1 is fine.

THE TOKEN-REDACTION FILTER DOES NOT COVER FCM TOKENS AND DOES NOT NEED TO.
`apns._LONG_HEX` matches pure-hex runs and an FCM registration token is base64url
(`:`, `-`, `_`), so it never matches. But the leak class it was built for
(claude-tasks#3586 — the token in the URL PATH, logged at INFO by httpx) does not
exist on this path at all: FCM v1 carries the token in the request BODY, which
httpx does not log. Stated so nobody "fixes" the gap by widening a regex that
guards nothing here. The protection is the same discipline as APNs: log the row's
ULID, never the token — which `push_service` does, and which is why this module
logs no identifier of its own.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Literal, Union

import httpx
import jwt

from ..config import settings
from .push_result import (END_WAKE_EXPIRY_SECONDS, RING_CEILING_SECONDS,
                          ReapOrder, SendResult, Verdict, WakeKind,
                          WakePayload)

log = logging.getLogger("aiko_gateway.fcm")

_FCM_HOST = "https://fcm.googleapis.com"
# Used only when the credential omits `token_uri`, which Google's own generated
# files never do. Present so a hand-trimmed blob degrades to the documented
# endpoint rather than to a KeyError inside a background task.
_TOKEN_URI_FALLBACK = "https://oauth2.googleapis.com/token"
# The NARROW scope. `.../auth/cloud-platform` also works and is far broader —
# this credential should be able to send a message and do nothing else.
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
# Google's maximum assertion lifetime.
_ASSERTION_TTL_SECONDS = 3600
# Refresh this long before the access token expires. UNLIKE APNs the window is
# ONE-SIDED: Google documents no minimum mint interval and no
# TooManyProviderTokenUpdates equivalent, so `apns._TOKEN_REFRESH_SECONDS`'s
# two-sided reasoning does NOT transfer and must not be copied here.
_ACCESS_TOKEN_SKEW_SECONDS = 300
# THE BACKOFF CLOCKS (design 17 v4). Without them one bad credential is one POST
# to Google's rate-limited token endpoint PER DEVICE PER RING, plus an ERROR line
# each, which is a misconfiguration that generates its own rate-limit incident.
#
# Flat for a credential that cannot be parsed or signed, or a token endpoint that
# answered in a shape we cannot read: neither changes with retrying.
_OAUTH_FAILURE_BACKOFF_SECONDS = 60
# GROWING for `invalid_grant` and for send-side PERMISSION_DENIED, from a base up to
# one cap: `min(base * 2**(strikes-1), cap)`. `invalid_grant` covers a key minted
# seconds ago (propagation, measured at <=30s on 2026-10-08), a deleted or disabled
# key, and a skewed clock. The short base lets a fresh key recover fast; the
# growth decays a dead one to one POST per cap, rather than one every 10s forever
# (Kelvin, design 17 temper r3).
_GRANT_BACKOFF_BASE_SECONDS = 10
_DENIED_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 900
# How long FCM may keep trying to deliver, as a protobuf Duration STRING.
#
# THE TRAP THIS CONSTANT EXISTS TO NAME: `apns-expiration` is an ABSOLUTE unix
# timestamp; `android.ttl` is a RELATIVE duration from receipt at FCM. Reusing the
# computed APNs value would emit "1789000000s" — a perfectly legal ~56,000-year
# duration, clamped to FCM's four-week ceiling — and the result is a wake that can
# ring a handset weeks after the call ended, with no error anywhere.
#
# PER WAKE KIND, and the invite's is THE RING CEILING (cage-match PR#192 r1).
# This was a flat 60, defended as "an Android data push gives the app code
# execution before anything rings, so a late delivery can be declined on-device".
# The receiver that was actually built (claude-tasks#4421) falsifies that premise:
# its native layer rings at +100ms and Dart warms at +3.3s, so the ring starts
# before any code can judge the invite's age — and the payload carries no send
# time to judge it by. A 60s TTL therefore rang a handset up to 30s after the
# island's ring ceiling, for a call already over.
#
# The end keeps a longer life for the opposite reason: a late end is harmless, an
# expired one leaves a ring running. Both values come from `push_result`, so they
# cannot drift from the APNs side.
_TTL_SECONDS: dict[WakeKind, int] = {
    WakeKind.CALL_INVITE: RING_CEILING_SECONDS,
    WakeKind.CALL_END: END_WAKE_EXPIRY_SECONDS,
}
_TIMEOUT_SECONDS = 10.0

# ---------------------------------------------------------------------------
# THE AUTH STATE MACHINE (docs/design/17-fcm-token-state-machine.md, v4).
#
# WHY IT IS A TABLE. PR#192's cage-match found one defect in this area in every
# round for three rounds, each a (state, event) pair nobody had written down: a
# 401 that never cleared the cache, a 503 negative-cached as a refusal, a 200 with
# `access_token: null` cached for an hour, a late 401 wiping a newer token. Three
# module globals with three writers, coordinated by comments, cannot be swept. So
# the state is ONE immutable value, every change goes through ONE pure function,
# and `tests/test_fcm_auth.py` sweeps that function over the product of every
# phase and every event, against a table written as data.
#
# THE DECISIVE MEASUREMENT (2026-10-08): IAM is checked at SEND, never at mint. A
# service account with no role mints a token fine and gets 403 PERMISSION_DENIED
# (no FcmError code) on send. Each island now has its own ring account, so revoking
# one is an intended operator move, and it arrives ONLY on the send path. That is
# `SendDenied`, and minting a fresh token must not end it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Empty:
    """No token, and no reason to stay quiet: the next caller mints."""


@dataclass(frozen=True)
class Cached:
    token: str
    expires_at: float  # monotonic


@dataclass(frozen=True)
class MintBackoff:
    """The token endpoint refused us in a way retrying will not fix soon."""
    until: float


@dataclass(frozen=True)
class SendDenied:
    """FCM said this ACCOUNT may not send. Island-wide; time alone ends it."""
    until: float


Phase = Union[Empty, Cached, MintBackoff, SendDenied]


@dataclass(frozen=True)
class AuthState:
    phase: Phase
    # Survives phase changes; reset ONLY by a delivered send. It is what makes
    # repeated failures back off further.
    strikes: int = 0
    # When the current phase was entered (monotonic). A `Denied` whose send began
    # before this is stale evidence from an earlier phase, and is a no-op.
    since: float = float("-inf")


# A RESTART WAKES HERE. There is no other reload: settings are read from the
# environment at boot, so a new credential means a new process. That is why no
# `config_reloaded` event exists (design 17 v4: nothing could fire one).
INITIAL = AuthState(Empty())


@dataclass(frozen=True)
class Minted:
    token: str
    expires_at: float


@dataclass(frozen=True)
class MintFailed:
    kind: Literal["credential", "unreadable", "grant"]


@dataclass(frozen=True)
class Blinked:
    """Transport error, 429 or 5xx, on either endpoint. Says nothing about us."""


@dataclass(frozen=True)
class Refused:
    """Send 401: this BEARER was refused. Compare-and-clear on it."""
    bearer: str


@dataclass(frozen=True)
class Denied:
    """Send 403 + PERMISSION_DENIED + no FcmError code: the ACCOUNT may not send."""
    sent_at: float


@dataclass(frozen=True)
class Delivered:
    pass


@dataclass(frozen=True)
class DeviceLocal:
    """A verdict about ONE device (UNREGISTERED, SENDER_ID_MISMATCH,
    INVALID_ARGUMENT). Never island-wide state."""


@dataclass(frozen=True)
class Tick:
    """Time passing. Applied before every read, so expiry is a transition too."""


@dataclass(frozen=True)
class Unclassified:
    """A response nobody anticipated. Logged loudly; NEVER routed to `Denied`, so
    one garbled per-device 403 cannot silence every Android ring."""
    status: int
    error_code: str


Event = Union[Minted, MintFailed, Blinked, Refused, Denied, Delivered,
              DeviceLocal, Tick, Unclassified]


def _grow(base: int, strikes: int) -> float:
    # The exponent is capped before the power: strikes only reset on a delivered
    # send, so a role that stays gone for months walks it without bound.
    return float(min(base * 2 ** min(max(strikes - 1, 0), 16), _BACKOFF_CAP_SECONDS))


def transition(state: AuthState, event: Event, now: float) -> AuthState:
    """THE ONLY WRITER. Pure: no I/O, no clock read, no logging.

    Every (phase, event) pair has an explicit answer; "keep" is a real answer and
    is tested as one. The table it implements is in design 17 v4, and the same
    table lives as data in `tests/test_fcm_auth.py`.
    """
    phase = state.phase

    def enter(new: Phase, strikes: int) -> AuthState:
        return AuthState(new, strikes, now)

    # Time first: a deadline that has passed is a transition whatever arrived.
    if isinstance(event, Tick):
        if isinstance(phase, Cached) and now >= phase.expires_at:
            return enter(Empty(), state.strikes)
        if isinstance(phase, (MintBackoff, SendDenied)) and now >= phase.until:
            return enter(Empty(), state.strikes)
        return state

    # A DENIAL IS ENDED BY TIME ONLY. A fresh token from an account that lost its
    # role 403s again, and every other event while closed is the same fact
    # restated or stale (concurrent 403s from one ring COALESCE here: one fact,
    # one strike).
    if isinstance(phase, SendDenied):
        return state

    if isinstance(event, Minted):
        return enter(Cached(event.token, event.expires_at), state.strikes)
    if isinstance(event, MintFailed):
        strikes = state.strikes + 1
        if event.kind == "grant":
            until = now + _grow(_GRANT_BACKOFF_BASE_SECONDS, strikes)
        else:
            until = now + _OAUTH_FAILURE_BACKOFF_SECONDS
        return enter(MintBackoff(until), strikes)
    if isinstance(event, Denied):
        if event.sent_at < state.since:
            return state  # stale: the send began before this phase did
        strikes = state.strikes + 1
        return enter(SendDenied(now + _grow(_DENIED_BACKOFF_BASE_SECONDS, strikes)),
                     strikes)
    if isinstance(event, Refused):
        if isinstance(phase, Cached) and phase.token == event.bearer:
            return enter(Empty(), state.strikes)
        return state
    if isinstance(event, Delivered):
        if isinstance(phase, (Empty, Cached)):
            return replace(state, strikes=0)
        return state
    # Blinked, DeviceLocal, Unclassified: keep. (Unclassified's loudness is the
    # orchestrator's job; this function does not log.)
    return state


@dataclass(frozen=True)
class Have:
    token: str


@dataclass(frozen=True)
class Mint:
    pass


@dataclass(frozen=True)
class Silent:
    pass


def get(state: AuthState) -> Have | Mint | Silent:
    """What a caller may do NOW. Apply `Tick` first; this does not read a clock.

    `Silent` means: send nothing and mint nothing. The device gets TRANSIENT,
    because it is not implicated by our auth. The fanout does not retry, so a ring
    during `Silent` is dropped, not parked. That is the stated cost of a window.
    """
    if isinstance(state.phase, Cached):
        return Have(state.phase.token)
    if isinstance(state.phase, Empty):
        return Mint()
    return Silent()


_cached_credential: dict | None = None
_state: AuthState = INITIAL
# SINGLE-FLIGHT. The fanout is a concurrent `asyncio.gather`, so a cold cache
# under a ring to N Android devices was N concurrent mints, not the "one wasted
# exchange" this module used to claim. Now there is one in-flight mint, awaited by
# every caller through `shield` (one cancelled waiter must not cancel the rest).
_mint_flight: asyncio.Task | None = None
_client_singleton: httpx.AsyncClient | None = None


class FcmNotConfigured(RuntimeError):
    """This island has no FCM credential — Android push is not enabled on this
    deployment. An expected operator state, never a bug: like APNs and LiveKit,
    an unconfigured optional capability is simply off."""


def is_configured() -> bool:
    """True iff this island holds an FCM service-account credential.

    A plain `bool(...)` rather than the written-out `all()` of
    `apns.is_configured` — and the difference is a real result, not an omission.
    ONE field means there is no half-configured state to fail closed against:
    `project_id`, `client_email`, `private_key` and `token_uri` all come out of
    the same blob, so they cannot disagree with each other.

    That is honest ONLY because the boot guard in `config.py` rejects a blob that
    is unparseable, wrong-shaped or carrying a non-RSA key. Without it, "present"
    would not imply "usable" and this predicate would be the half-on state one
    layer down.
    """
    return bool(settings.fcm_service_account_json)


def reset_for_tests() -> None:
    """Return the auth state to INITIAL and drop the cached credential. Tests mutate
    settings between cases, and a token minted for the previous credential would
    outlive them.

    Deliberately does NOT reset the pooled client, for the reason written out at
    `apns.reset_for_tests`: nulling the reference abandons a live connection
    without closing it (this function is sync and cannot await `aclose`). Nothing
    in the client depends on the credential anyway — the host is in the URL and
    the auth is in a per-send header.
    """
    global _cached_credential, _state, _mint_flight
    _cached_credential = None
    _state = INITIAL
    _mint_flight = None


def _credential() -> dict:
    """The parsed service-account blob, cached.

    PARSED LAZILY rather than at import: `config.py`'s boot ladder has already
    proven it parses, so this cannot be the first place a bad value is discovered
    — but keeping the parse out of module scope means importing this module costs
    nothing and cannot fail, which the suite's isolation invariant relies on.
    """
    global _cached_credential
    if _cached_credential is None:
        if not is_configured():
            raise FcmNotConfigured("FCM credentials are not set on this island")
        _cached_credential = json.loads(settings.fcm_service_account_json)
    return _cached_credential


def _project_id() -> str:
    """The Firebase project id, DERIVED from the credential — never a second
    setting.

    A `fcm_project_id` field would create a class where the key and the project
    disagree, and that class's symptom is a bare 404 for every device on the
    island (see `_verdict`). Deriving makes the state unrepresentable rather than
    guarded.
    """
    return _credential()["project_id"]


def _sign_assertion(credential: dict) -> str:
    """The RS256 JWT-bearer assertion Google exchanges for an access token.

    Two clocks, two jobs — the same note as `apns._provider_token`: `iat`/`exp`
    are WALL-CLOCK seconds because Google compares them to ITS clock, while the
    cache interval in `_access_token` is monotonic because it measures OUR elapsed
    time. Using either for both is a bug in one direction or the other.
    """
    now = int(time.time())
    return jwt.encode(
        {
            "iss": credential["client_email"],
            "scope": _SCOPE,
            "aud": credential.get("token_uri") or _TOKEN_URI_FALLBACK,
            "iat": now,
            "exp": now + _ASSERTION_TTL_SECONDS,
        },
        credential["private_key"],
        algorithm="RS256",
        # `kid` IS OPTIONAL HERE, AND MUST BE OMITTED RATHER THAN PASSED AS None.
        # Google identifies the signing key from `iss` (the service-account email);
        # `private_key_id` is a convenience, not part of the JWT-bearer contract. But
        # PyJWT hard-rejects a non-string kid ("Key ID header parameter must be a
        # string"), so passing `.get()` straight through turns a credential the boot
        # ladder BLESSES — it requires only project_id, client_email, private_key —
        # into an exception on every single send. Android goes totally deaf while
        # /health stays green, which is the exact failure the ladder exists to
        # prevent, one field over.
        headers=_assertion_headers(credential),
    )


def _assertion_headers(credential: dict) -> dict | None:
    """`{"kid": ...}` only when there is a real string to put in it."""
    kid = credential.get("private_key_id")
    return {"kid": kid} if isinstance(kid, str) and kid else None


def _apply(event: Event) -> None:
    """Feed one event to the one writer, and say so when the island's Android
    reach changes. Logging lives HERE, never in `transition`, so the table stays
    pure and the operator hears each change once rather than once per device."""
    global _state
    before = _state
    _state = transition(_state, event, time.monotonic())
    after = _state.phase
    if isinstance(event, Unclassified):
        # LOUD BY CONTRACT (design 17 v4): an unanticipated response must never
        # become a quiet REJECTED. The status and the FcmError code are fixed
        # vocabularies; the body is never logged.
        log.error("fcm unclassified response status=%s error_code=%s — the auth "
                  "table has no row for this; it changed nothing, report it",
                  event.status, event.error_code or "-")
    if type(after) is type(before.phase):
        return
    window = int(after.until - time.monotonic()) if isinstance(
        after, (MintBackoff, SendDenied)) else 0
    if isinstance(after, SendDenied):
        log.error("fcm send denied (PERMISSION_DENIED): this island's ring account "
                  "may not send. Check its role (islandRinger) and that the Cloud "
                  "Messaging API is enabled. Android is silent for %ds (strike %d)",
                  window, _state.strikes)
    elif isinstance(after, MintBackoff) and isinstance(event, MintFailed):
        log.error(_MINT_FAILURE_MESSAGE[event.kind] + " Android is silent for %ds "
                  "(strike %d)", window, _state.strikes)
    elif isinstance(after, Empty) and isinstance(before.phase,
                                                 (MintBackoff, SendDenied)):
        log.warning("fcm auth window lapsed — the next ring will try again")


# One sentence per LAYER (design 17 v4). The old single message told an operator
# to "check the service account's role" on a MINT failure, but minting never
# consults IAM: the role is checked at send. So the role belongs to `SendDenied`
# and appears nowhere here.
_MINT_FAILURE_MESSAGE = {
    "credential": ("fcm credential is unusable: FCM_SERVICE_ACCOUNT_JSON cannot be "
                   "parsed or signed with. The boot ladder accepts a blob this "
                   "signing step cannot use, so a green /health does not mean "
                   "Android can be reached."),
    "grant": ("fcm key refused (invalid_grant): it may be minutes old (key "
              "propagation), deleted or disabled, or this box's clock may be off."),
    "unreadable": ("fcm token endpoint answered in a shape this island cannot "
                   "read."),
}


def classify_mint(status: int, body: object, now: float) -> Event:
    """TOTAL over the token endpoint's responses. Pure."""
    if status == 429 or status >= 500:
        # GOOGLE BLINKED, NOT OUR KEY (Tesla, cage-match PR#192 r2). One backoff
        # bit used to mean both "this key will never sign" and "the endpoint had a
        # bad second", so a single 503 silenced Android for a minute.
        return Blinked()
    if status == 200:
        if not isinstance(body, dict):
            return MintFailed("unreadable")
        token = body.get("access_token")
        # A 200 carrying `"access_token": null` (or "", or a number) used to be
        # CACHED and replayed as "no token" for ~55 minutes (Tesla, PR#192 r2).
        # Only a non-empty string may become `Minted`.
        if not isinstance(token, str) or not token:
            return MintFailed("unreadable")
        try:
            expires_in = int(body.get("expires_in", _ASSERTION_TTL_SECONDS))
        except (TypeError, ValueError):
            return MintFailed("unreadable")
        # Never cache a token for longer than it lives; a skew larger than the
        # lifetime would otherwise produce a cache entry already in the past.
        return Minted(token, now + max(expires_in - _ACCESS_TOKEN_SKEW_SECONDS, 0))
    # RFC 6749 puts a fixed vocabulary in `error`; reading it is safe, unlike the
    # body, which can echo request material and is never logged.
    if (status == 400 and isinstance(body, dict)
            and body.get("error") == "invalid_grant"):
        return MintFailed("grant")
    return MintFailed("unreadable")


# The FcmError codes that are facts about ONE device. Anything not here and not
# matched below is `Unclassified`, never island-wide.
_DEVICE_LOCAL_CODES = frozenset({"UNREGISTERED", "SENDER_ID_MISMATCH",
                                 "INVALID_ARGUMENT"})


def classify_send(status: int, body: object, bearer: str, sent_at: float) -> Event:
    """TOTAL over the send endpoint's responses. Pure.

    THE ISLAND-WIDE 403 IS BARE, measured 2026-10-08: gRPC status
    `PERMISSION_DENIED` and NO FcmError detail. `SENDER_ID_MISMATCH` is the 403
    that DOES carry an FcmError code, and it is per-device. So `Denied` requires
    all three facts, and anything garbled falls to `Unclassified`, not to
    `Denied`: one misread per-device 403 must not silence every Android ring.
    """
    if status == 200:
        return Delivered()
    if status == 429 or status >= 500:
        return Blinked()
    if status == 401:
        return Refused(bearer)
    code = _fcm_error_code(body) if isinstance(body, dict) else ""
    if code in _DEVICE_LOCAL_CODES:
        return DeviceLocal()
    error = body.get("error") if isinstance(body, dict) else None
    rpc_status = error.get("status") if isinstance(error, dict) else None
    if status == 403 and rpc_status == "PERMISSION_DENIED" and code == "":
        return Denied(sent_at)
    return Unclassified(status, code)


async def _mint_once() -> str | None:
    """One exchange at the token endpoint. Runs as the single in-flight task;
    its event is applied exactly once, here, whatever the number of waiters.

    THE SIGNING IS INSIDE THE GUARD. `_access_token` promises it returns None
    rather than raising, and a credential defect that escaped as an exception
    would be a traceback per device per ring with the backoff unreachable.
    """
    try:
        credential = _credential()
        assertion = _sign_assertion(credential)
    except Exception:
        _apply(MintFailed("credential"))
        return None
    try:
        # BOUNDED HERE, NOT ONLY BY THE CLIENT. Every concurrent caller is waiting
        # on this one task, so a hung mint would silence the island for as long as
        # it hangs. The client's timeout already covers a real socket; this makes
        # the bound a property of the single-flight itself (Kelvin, temper r2).
        response = await asyncio.wait_for(
            _client().post(
                credential.get("token_uri") or _TOKEN_URI_FALLBACK,
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                      "assertion": assertion}),
            _TIMEOUT_SECONDS)
    except (httpx.HTTPError, asyncio.TimeoutError) as ex:
        # NOT negative-cached: a transport failure says nothing about the key.
        log.warning("fcm oauth failed transport=%s", type(ex).__name__)
        _apply(Blinked())
        return None
    try:
        body: object = response.json()
    except ValueError:
        body = None
    event = classify_mint(response.status_code, body, time.monotonic())
    if isinstance(event, Blinked):
        log.warning("fcm oauth unavailable status=%s — not backing off",
                    response.status_code)
    _apply(event)
    return event.token if isinstance(event, Minted) else None


async def _access_token() -> str | None:
    """A usable bearer, or None if this island must stay quiet right now.

    RETURNS None RATHER THAN RAISING: auth here is a NETWORK EXCHANGE that fails
    whenever Google is briefly unreachable, and `push_service`'s per-device
    `except Exception` must stay the exceptional path.
    """
    global _mint_flight
    _apply(Tick())
    action = get(_state)
    if isinstance(action, Have):
        return action.token
    if isinstance(action, Silent):
        return None
    # THE LOOP CHECK IS NOT DECORATION. A flight left pending on a loop that has
    # since closed is never `done()`, and awaiting it from another loop raises on
    # every call — Android silent forever, one traceback per send (Maxwell,
    # cage-match PR#192). One loop runs in production; tests and any future host
    # need not.
    if (_mint_flight is None or _mint_flight.done()
            or _mint_flight.get_loop() is not asyncio.get_running_loop()):
        _mint_flight = asyncio.ensure_future(_mint_once())
    try:
        await asyncio.shield(_mint_flight)
    except asyncio.CancelledError:
        raise
    except Exception:  # never-raise contract; _mint_once catches its own
        log.exception("fcm mint task failed unexpectedly")
        return None
    # ONE READING OF THE RESULT: the state, not the mint's return value (Tesla,
    # cage-match PR#192 r2). A `Minted` the table KEEPS (a denial opened while the
    # mint was in flight) must not hand its token to every waiter.
    after = get(_state)
    return after.token if isinstance(after, Have) else None


def auth_status() -> dict:
    """Android's auth phase for `/health`: a readiness fact, not a name beside a
    green check (Tesla, temper r3).

    It must NOT fail the container healthcheck, which is `curl -f` on /health and
    so reads only the HTTP status. Under `restart: always`, an IAM outage that
    failed the healthcheck would become a restart loop taking the working APNs
    transport down with it, the failure design 14's temper recorded for the boot
    guard. So this is body-only, and the status code never depends on it.
    """
    state = transition(_state, Tick(), time.monotonic())
    phase = state.phase
    names = {Empty: "empty", Cached: "cached", MintBackoff: "mint_backoff",
             SendDenied: "send_denied"}
    # TRI-STATE, because `Empty` is not evidence (Maxwell, cage-match PR#192).
    # Every restart and every idle hour (the token expires) lands in `Empty`;
    # reporting it as ready made "never tried" read exactly like "works", so a
    # ring account deleted overnight showed ready until the first real call.
    # True = a mint succeeded and nothing has refused us since. None = unknown.
    if isinstance(phase, Cached):
        ready: bool | None = True
    elif isinstance(phase, Empty):
        ready = None
    else:
        ready = False
    return {"ready": ready,
            "phase": names[type(phase)],
            "strikes": state.strikes}


def _client() -> httpx.AsyncClient:
    global _client_singleton
    if _client_singleton is None:
        # NO `http2=True` AND NO `import h2` PROBE. HTTP/2 is an APNs requirement
        # (Apple speaks nothing else); FCM v1 is ordinary HTTPS. Copying that probe
        # across would invent a deploy-time dependency this transport does not
        # have.
        #
        # THE EXPLICIT TIMEOUT IS MANDATORY: httpx's default is no timeout at all,
        # and a hung POST inside a per-device fanout stalls every remaining device
        # AND every remaining recipient behind it.
        _client_singleton = httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)
    return _client_singleton


async def aclose() -> None:
    """Close the pooled client. Called from the app lifespan on shutdown, AFTER
    `push_service.aclose()` has drained the in-flight wakes."""
    global _client_singleton
    if _client_singleton is not None:
        await _client_singleton.aclose()
        _client_singleton = None


def build_message(device_token: str, payload: WakePayload) -> dict:
    """The FCM v1 envelope for a wake. Module-level and pure, so the invariants
    below are testable with no network.

    THE CONTRACT IS THE APP TAB'S, AND IT IS HARDWARE-VERIFIED (claude-tasks#4421,
    2026-10-05). Until then this docstring carried a HARD GATE — "today's client
    consumes neither half of this shape" — because the app's only Android push
    consumer was a tray-tap source that a data-only message can never reach. That
    stopped being true when `aiko_chat_app` `feat/android-ring` landed a receiver.
    The contract it consumes, verbatim from #4421:

        data = {"c": <channel_id>, "k": "call_invite" | "call_end", "m": <call id>}
        data-only, NO `notification` block, android.priority "HIGH", short TTL.

    It was proven on a Pixel 4 (Android 13) with this exact envelope: process
    killed + dozing + secure keyguard -> ring at +100ms, full-screen over the
    keyguard at +400ms; `call_end` stopped the ring 1ms after delivery.

    `"k"` IS REQUIRED, AND ITS ABSENCE NEVER RINGS. The stranded first draft of
    this function sent only `{"c"}` — it predates `WakeKind`. The receiver runs the
    same total function as iOS's `CallKitRinger.handle`: `call_invite` rings
    call `m` unless `m` is spent, `call_end` stops call `m` if it is ringing
    and marks `m` SPENT either way (so an end delivered before its invite still
    wins; "spent" is the app's term since its #211 rename, formerly "tombstone"), and ANYTHING ELSE — a missing `k` included — is ignored. So `{"c"}` alone is a send FCM
    answers 200 to, `push_service` logs as delivered, and the handset drops on the
    floor: the exact structurally-invisible failure this paragraph used to warn
    about, one key over. Same "explicit on both values" rule as `apns._render`.

    `"m"` IS THE CALL ID, ON EVERY WAKE (design 12 Decision 1, bytes agreed
    2026-10-06). It is what lets the receiver tell the same invite delivered
    twice from a new call on the same channel. v1 bodies never wake (app design
    22 §v2.0, Nick 2026-10-06), so there is no `m`-less wake to decode.

    THE CREDENTIAL GATE IS NOW THE APP'S MERGE, NOT THIS CODE. The app tab asked
    that `FCM_SERVICE_ACCOUNT_JSON` stay unprovisioned until `feat/android-ring`
    clears its cage-match and merges (calling-off store builds ignore call wakes
    anyway). Nothing here enforces that — `is_configured()` is the switch, and the
    switch is the operator's .env. Provisioning is a deploy decision recorded on
    #4421, not a property of this module.

        DATA-ONLY, WITH NO `notification` BLOCK ANYWHERE — A CORRECTNESS PROPERTY, NOT
    A STYLE CHOICE. A message carrying `notification` is a DISPLAY message: when
    the app is backgrounded or killed the system tray renders it and the app gets
    NO CODE EXECUTION, so `onMessageReceived` never runs and the app cannot start
    a foreground service, post a full-screen intent, or enter Telecom. It cannot
    ring; it draws a banner. A data-only message at HIGH priority invokes
    `onMessageReceived` even out of Doze and earns Android 12+'s explicit
    background foreground-service-start exemption.

    The ring-capable choice and the payload-opacity choice are therefore THE SAME
    CHOICE here, which makes this decision free — unlike iOS, where PushKit's
    reach costs the mandatory CallKit report.

    `data` IS `map<string,string>`. A non-string value or a nested object is a
    hard 400 INVALID_ARGUMENT — which fires identically for every device on the
    island, not for one bad token.

    `"HIGH"` IS UPPERCASE. Protobuf JSON enum parsing is case-sensitive and
    lowercase `"high"` is the DECOMMISSIONED legacy API's spelling.

    NO `fcm_options.analytics_label`: it is optional and ships analytics metadata
    to Google, which `WakePayload`'s refusal covers verbatim.

    NO `collapse_key`, AND THE ABSENCE IS THE FIX (Tesla, cage-match PR#192, the
    round after design 17). A collapse key moves a message into FCM's COLLAPSIBLE
    class, which has two limits a call wake cannot live inside:
      * FCM keeps at most FOUR collapse keys per device; past that it keeps four
        "with no guarantees about which". Keyed per call, an offline handset
        receiving five calls inside the end's 300s life can lose a call's END,
        and the payload carries no send time for the receiver to judge an orphan
        invite by. A channel key was worse: B's invite overwrote A's end.
      * Collapsible messages are throttled per device (a burst, then a slow
        refill), and the delay runs against the TTL.
    What the key bought was "an offline call collapses to its end". The invite's
    30s TTL already buys that: an invite that cannot be delivered within the ring
    ceiling expires, and the end (300s) still arrives. Non-collapsible messages are
    each stored (up to FCM's per-device offline limit), so no call can displace
    another call's stop. Not part of the app tab's contract (claude-tasks#4421 is
    `data` + priority + TTL), so nothing on the receiver changes.

    Android's replace-the-visible-one field, `android.notification.tag`, exists only
    on a `notification` message, which a call wake must never be. Under data-only,
    lock-screen de-duplication is app-side work (it keys on `m`).
    """
    # A KeyError for an unmapped kind is caught by `push_service`'s per-device
    # boundary and logged — loud, never a guessed TTL. The test sweep below makes
    # it unreachable for every member that exists.
    android: dict = {"priority": "HIGH", "ttl": f"{_TTL_SECONDS[payload.kind]}s"}
    return {
        "message": {
            # The target is a `oneof`: exactly one of token/topic/condition.
            # Legacy `to` / `registration_ids` do not exist in v1.
            "token": device_token,
            # `k` on EVERY wake, both values explicit — see the contract above.
            # `m` on EVERY wake: v1 never wakes (app design 22 §v2.0).
            "data": {"c": payload.channel_id, "k": payload.kind.value,
                     "m": payload.call_id},
            "android": android,
        }
    }


def _fcm_error_code(body: dict) -> str:
    """The actionable error code, read BY TYPE out of `error.details[]`.

    NOT `error.status` — that is the generic `google.rpc.Code` name, which cannot
    distinguish a dead token (`NOT_FOUND` + `UNREGISTERED`) from a wrong project
    id (`NOT_FOUND` and nothing else). NOT `details[0]` either: the array can
    carry a `google.rpc.RetryInfo` alongside the `FcmError`, and position is not a
    contract.

    Returns "" when there is no FcmError detail at all, which is itself the
    load-bearing signal — see `_verdict`.
    """
    if not isinstance(body, dict):
        return ""
    # `error` CHECKED BY TYPE, like `details` below (Tesla, cage-match PR#192 r2):
    # an RFC 6749-shaped `{"error": "invalid_grant"}`, or a front end that never
    # reached FCM, put a STRING here, and `.get` on it raised AttributeError
    # straight out of `send`'s never-raise contract.
    error = body.get("error")
    if not isinstance(error, dict):
        return ""
    details = error.get("details")
    if not isinstance(details, list):
        return ""
    for detail in details:
        if not isinstance(detail, dict):
            continue
        if detail.get("@type") == (
                "type.googleapis.com/google.firebase.fcm.v1.FcmError"):
            code = detail.get("errorCode")
            return code if isinstance(code, str) else ""
    return ""


def _verdict(status: int, error_code: str) -> Verdict:
    """Map an FCM response to the one decision the caller must make.

    THE REAPING RULE, AND WHY IT IS NARROWER THAN IT LOOKS — the same doctrine as
    `apns._verdict`, keyed on a different piece of evidence.

    THE PERMANENTLY-DEAD SET IS EXACTLY `{UNREGISTERED}`, read from the FcmError
    detail. Everything else keeps the row:

      * `INVALID_ARGUMENT` (400) is overwhelmingly OUR bug — a bad ttl string, a
        non-string data value, a lowercase "high", an unknown field — and
        therefore fires IDENTICALLY FOR EVERY DEVICE ON THE ISLAND. It can also
        mean "this token string is unparseable", and the response gives us no way
        to tell those apart. The two failure directions are wildly asymmetric: one
        costs a wasted HTTP request per send, the other destroys state recoverable
        only by every user reopening the app.
      * `SENDER_ID_MISMATCH` (403) is a LIVE token minted against a DIFFERENT
        Firebase project — an island CONFIG fact, the structural twin of
        BadDeviceToken-from-the-wrong-environment, and a second independent reason
        never to reap on a generic 4xx.
      * `401 UNAUTHENTICATED` / a bare `403 PERMISSION_DENIED` are our OAuth token
        or our service account's role.
      * A `404` WITH NO `UNREGISTERED` DETAIL is a WRONG `PROJECT_ID` IN THE URL,
        and it fires for EVERY device on the island. A reaper keyed on HTTP status
        alone would delete the entire device table on the first ring. This single
        case is why the key is the detail and not the status.

    Failing closed here means NOT deleting.
    """
    if status == 200:
        return Verdict.DELIVERED
    if error_code == "UNREGISTERED":
        return Verdict.DEAD_TOKEN
    if status == 429 or status >= 500:
        return Verdict.TRANSIENT
    return Verdict.REJECTED


def _reap_order_for(verdict: Verdict) -> ReapOrder | None:
    """Whether Google's answer PERMITS deleting the row, and under what condition.

    THE ORDER CARRIES NO DATE, AND THE WEAKNESS IS STATED RATHER THAN HIDDEN. An
    FCM `UNREGISTERED` response contains no timestamp, so there is no analogue of
    Apple's resume-if-re-registered evidence. What carries the reversibility is
    the reaper's compare-and-delete on `(id, token, updated_at-as-observed-at-send-
    time)`, which is itself a compare-and-swap: `register_device` sets
    `updated_at` explicitly on every reassign, so a device that re-registers
    between our send and our reap fails the equality and survives.

    THE ONE WINDOW A DATE ARM WOULD ADDITIONALLY CLOSE is a row re-registered
    BEFORE our send but AFTER Google recorded the token dead — which for FCM
    requires the SAME token string to be re-issued after invalidation. UNVERIFIED
    KNOWLEDGE CLAIM, marked as such because this is the only irreversible
    operation in the push path: a refresh is believed to mint a NEW string. Cost
    if wrong: that device loses its row and re-registers on next app open —
    recoverable, no data loss, strictly smaller than the APNs case the date arm
    was written for. Falsifier, and it is already instrumented: a rise in
    `reason=row_changed_since_send` on the FCM path is what a wrong assumption
    here would look like. Reopen this with that data, not with argument.
    """
    return ReapOrder(None) if verdict is Verdict.DEAD_TOKEN else None


async def send(device_token: str, payload: WakePayload) -> SendResult:
    """Push one wake to one Android device. Returns a [SendResult]; never raises
    for a protocol-level refusal, a transport error, or an auth failure.

    NO `token_kind` AND NO ENVIRONMENT PARAMETER, and both absences are results.
    FCM has ONE registry — there is no VoIP-equivalent token, and ring-ness is a
    property of the MESSAGE (data-only + HIGH priority) rather than of the token —
    and one endpoint per project, with no sandbox/production split. A parameter
    the transport cannot honour is worse than its absence: it would read as a
    routing decision that nothing downstream makes.

    Raises [FcmNotConfigured] only if called on an island with no credential,
    which is a caller bug: `push_service` gates on `is_configured()` first.
    """
    if not is_configured():
        raise FcmNotConfigured("FCM credentials are not set on this island")

    token = await _access_token()
    if token is None:
        # Already logged with its cause, once, by `_apply`. TRANSIENT rather than
        # REJECTED: the DEVICE is not implicated by our auth.
        return SendResult(Verdict.TRANSIENT)

    url = f"{_FCM_HOST}/v1/projects/{_project_id()}/messages:send"
    sent_at = time.monotonic()
    try:
        response = await _client().post(
            url, json=build_message(device_token, payload),
            headers={"authorization": f"Bearer {token}"})
    except httpx.HTTPError as ex:
        # The device is not implicated by OUR network failing.
        log.warning("fcm send failed transport=%s", type(ex).__name__)
        _apply(Blinked())
        return SendResult(Verdict.TRANSIENT)

    try:
        body: object = response.json()
    except ValueError:
        body = None
    # TWO READINGS OF ONE RESPONSE, kept apart on purpose: the EVENT is what it
    # means for the island's auth (one writer, `_apply`); the VERDICT is what it
    # means for this one device's row (`_verdict`, which owns the reaping rule).
    # A 401 compare-and-clears the bearer it was sent with (Tesla, PR#192 r3);
    # a bare PERMISSION_DENIED closes the island's sending until its window lapses.
    event = classify_send(response.status_code, body, token, sent_at)
    _apply(event)
    if response.status_code == 200:
        # 200 means ACCEPTED, not delivered — the same posture as an APNs 200.
        return SendResult(Verdict.DELIVERED)
    error_code = _fcm_error_code(body) if isinstance(body, dict) else ""
    # AN AUTH-WIDE ANSWER IS NOT A VERDICT ON THE DEVICE (Carnot + Tesla,
    # cage-match PR#192). A 401 refuses OUR bearer and a bare PERMISSION_DENIED
    # refuses OUR account; `_verdict` reading the raw status called both REJECTED,
    # so a revocation logged as a fleet of bad tokens. The device is not
    # implicated, which is what TRANSIENT means here (and what a `Silent` phase
    # already returns). Neither verdict reaps.
    if isinstance(event, (Denied, Refused)):
        verdict = Verdict.TRANSIENT
    else:
        verdict = _verdict(response.status_code, error_code)
    # NEVER the device token: it rides in the request body, so nothing else in
    # this path can leak it and this line must not be the exception.
    log.log(logging.ERROR if verdict is Verdict.REJECTED else logging.WARNING,
            "fcm refused status=%s error_code=%s verdict=%s",
            response.status_code, error_code or "-", verdict.value)
    return SendResult(verdict, _reap_order_for(verdict))
