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

import json
import logging
import time

import httpx
import jwt

from ..config import settings
from .push_result import ReapOrder, SendResult, Verdict, WakePayload

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
# How long a HARD auth rejection suppresses further token minting. Without it one
# bad credential is one POST to Google's rate-limited token endpoint PER DEVICE
# PER RING, plus an ERROR line each — a misconfiguration that generates its own
# rate-limit incident.
_OAUTH_FAILURE_BACKOFF_SECONDS = 60
# How long FCM may keep trying to deliver, as a protobuf Duration STRING.
#
# THE TRAP THIS CONSTANT EXISTS TO NAME: `apns-expiration` is an ABSOLUTE unix
# timestamp; `android.ttl` is a RELATIVE duration from receipt at FCM. Reusing the
# computed APNs value would emit "1789000000s" — a perfectly legal ~56,000-year
# duration, clamped to FCM's four-week ceiling — and the result is a wake that can
# ring a handset weeks after the call ended, with no error anywhere.
#
# 60 AND NOT THE VoIP LEASE'S 30, deliberately. An Android data push gives the app
# CODE EXECUTION BEFORE anything rings, so a late delivery can be declined
# on-device; a VoIP push cannot be, which is the whole reason the iOS lease is the
# ring ceiling. The two numbers answer different questions and are not a drift.
_TTL_SECONDS = 60
_TIMEOUT_SECONDS = 10.0

# The parsed credential, the OAuth access token as (token, expiry_monotonic), and
# the negative cache's expiry.
_cached_credential: dict | None = None
_cached_access_token: tuple[str, float] | None = None
_oauth_backoff_until: float | None = None
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
    """Drop the cached credential, access token and auth backoff. Tests mutate
    settings between cases, and a token minted for the previous credential would
    outlive them.

    Deliberately does NOT reset the pooled client, for the reason written out at
    `apns.reset_for_tests`: nulling the reference abandons a live connection
    without closing it (this function is sync and cannot await `aclose`). Nothing
    in the client depends on the credential anyway — the host is in the URL and
    the auth is in a per-send header.
    """
    global _cached_credential, _cached_access_token, _oauth_backoff_until
    _cached_credential = None
    _cached_access_token = None
    _oauth_backoff_until = None


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


async def _access_token() -> str | None:
    """The cached OAuth access token, or None if we could not get one.

    RETURNS None RATHER THAN RAISING — the first of this module's three
    divergences from `apns.py`. APNs auth is LOCAL signing that can only fail on a
    broken key the boot guard already caught; this is a NETWORK EXCHANGE that
    fails whenever Google is briefly unreachable. If that escaped as an exception,
    `push_service`'s per-device `except Exception` would become the normal path
    and a Google blip would read as "wake failed for one device" forever.

    NO LOCK AROUND THE MINT. A concurrent double-mint costs one wasted exchange,
    and the reason APNs' cache is load-bearing — Apple's 20-minute minimum mint
    interval — has no counterpart here.
    """
    global _cached_access_token, _oauth_backoff_until
    now = time.monotonic()
    if _cached_access_token is not None and now < _cached_access_token[1]:
        return _cached_access_token[0]
    if _oauth_backoff_until is not None and now < _oauth_backoff_until:
        # A credential already known bad. Refusing here is what stops one
        # misconfiguration becoming one token-endpoint POST per device per ring.
        return None

    # THE SIGNING IS INSIDE THE GUARD, not above it. This function's docstring
    # promises it "returns None rather than raising", and until this change that
    # promise covered only the network POST — `_credential()` parsing and
    # `_sign_assertion()` sat outside every handler, so a credential defect became
    # a traceback per device per ring, forever, with the negative cache unreachable
    # because it lives on the return path. A stated contract that the code does not
    # keep is worse than no contract: `push_service` is written against this one.
    try:
        credential = _credential()
        assertion = _sign_assertion(credential)
    except Exception as ex:
        # Negative-cached, UNLIKE a transport failure: a credential that cannot be
        # parsed or signed with will not fix itself, and retrying it once per device
        # per ring is the shape this backoff exists to stop.
        _oauth_backoff_until = now + _OAUTH_FAILURE_BACKOFF_SECONDS
        log.error(
            "fcm credential is unusable (%s) — every Android ring will be dropped "
            "until FCM_SERVICE_ACCOUNT_JSON is fixed. The boot ladder accepts a "
            "blob this signing step cannot use, so a green /health does not mean "
            "Android can be reached.", type(ex).__name__)
        return None

    try:
        response = await _client().post(
            credential.get("token_uri") or _TOKEN_URI_FALLBACK,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                  "assertion": assertion},
        )
    except httpx.HTTPError as ex:
        # NOT negative-cached: a transport failure says nothing about the
        # credential, and suppressing the next attempt for a minute would turn a
        # one-second blip into a minute of silence.
        log.warning("fcm oauth failed transport=%s", type(ex).__name__)
        return None

    if response.status_code != 200:
        _oauth_backoff_until = now + _OAUTH_FAILURE_BACKOFF_SECONDS
        # ERROR because this is an island CONFIGURATION fault that silently costs
        # every Android ring: the service account may lack the role, or the Cloud
        # Messaging API may not be enabled on the project. Never log the response
        # body — a token endpoint's error can echo request material.
        log.error("fcm oauth refused status=%s — Android push is OFF for the next "
                  "%ds; check the service account's role and that the Cloud "
                  "Messaging API is enabled",
                  response.status_code, _OAUTH_FAILURE_BACKOFF_SECONDS)
        return None

    try:
        body = response.json()
        token = body["access_token"]
        expires_in = int(body.get("expires_in", _ASSERTION_TTL_SECONDS))
    except (ValueError, KeyError, TypeError):
        _oauth_backoff_until = now + _OAUTH_FAILURE_BACKOFF_SECONDS
        log.error("fcm oauth returned an unreadable token response")
        return None

    # Never cache a token for longer than it lives; a skew larger than the
    # lifetime would otherwise produce a cache entry already in the past.
    lifetime = max(expires_in - _ACCESS_TOKEN_SKEW_SECONDS, 0)
    _cached_access_token = (token, now + lifetime)
    return token


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


def build_message(device_token: str, payload: WakePayload, *,
                  collapse_key: str | None = None) -> dict:
    """The FCM v1 envelope for a wake. Module-level and pure, so the invariants
    below are testable with no network.

    THIS SHAPE IS CORRECT FOR THE RING, AND TODAY'S CLIENT CONSUMES NEITHER HALF
    OF IT. Read this before provisioning a credential.

    Measured 2026-09-10 against `../aiko_chat_app`: the app's ONLY Android push
    consumer is `FcmNotificationTapSource`
    (`lib/features/notifications/data/notification_tap_source.dart:68-96`), whose
    whole contract is `getInitialMessage()` + `onMessageOpenedApp` — a USER TAP on
    a system-tray entry. Both streams fire only for a message that PRODUCED a tray
    entry, which a data-only message never does. `grep -rn onBackgroundMessage lib`
    returns nothing, so no Dart runs either, and the manifest carries no
    `POST_NOTIFICATIONS`, `FOREGROUND_SERVICE`, `USE_FULL_SCREEN_INTENT` or custom
    `FirebaseMessagingService`. The app tab says so itself: design 16
    §"Android is unscoped here".

    So with a credential provisioned, FCM answers 200, `push_service` logs
    `verdict=delivered`, the `delivered_to=0` alarm stays quiet — and the handset
    does nothing. The island's own alarms are STRUCTURALLY BLIND to it, which makes
    this worse than today's loud `transport_not_built` skip.

    HARD GATE, therefore: **do not provision `FCM_SERVICE_ACCOUNT_JSON` on any box
    until the app's Android receive half exists.** No island carries one today and
    that is the safe state, not an oversight.

    WHICH SHAPE SHIPS IS A JOINT DECISION, NOT TIE-BROKEN HERE. (a) data-only, as
    built — ring-capable, and the only shape that can drive a full-screen intent,
    which is what Nick's 2026-09-09 "ring like a telephone" ruling requires; it
    needs a background handler plus foreground-service/full-screen-intent plumbing
    that does not exist. (b) a `notification` block — exactly what
    `FcmNotificationTapSource` was built to consume, an interim tap-to-join path,
    and structurally incapable of ringing. The ruling points at (a); the working
    client is (b); the gap between them is app work, not island work.

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

    `collapse_key` IS NOT `apns-collapse-id`, AND THE GAP IS REAL. Apple's header
    coalesces DISPLAYED notifications; this coalesces UNDELIVERED messages while
    the device is offline, and is effectively inert for a high-priority message
    delivered immediately. Android's replace-the-visible-one field is
    `android.notification.tag`, which only exists on a `notification` message —
    i.e. exactly the thing a call wake must not be. Under data-only, lock-screen
    de-duplication is app-side work.
    """
    android: dict = {"priority": "HIGH", "ttl": f"{_TTL_SECONDS}s"}
    if collapse_key is not None:
        android["collapse_key"] = collapse_key
    return {
        "message": {
            # The target is a `oneof`: exactly one of token/topic/condition.
            # Legacy `to` / `registration_ids` do not exist in v1.
            "token": device_token,
            "data": {"c": payload.channel_id},
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
    details = body.get("error", {}).get("details")
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


async def send(device_token: str, payload: WakePayload, *,
               collapse_key: str | None = None) -> SendResult:
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
        # Already logged with its cause by `_access_token`. TRANSIENT rather than
        # REJECTED: the DEVICE is not implicated by our auth failing.
        return SendResult(Verdict.TRANSIENT)

    url = f"{_FCM_HOST}/v1/projects/{_project_id()}/messages:send"
    try:
        response = await _client().post(
            url, json=build_message(device_token, payload,
                                    collapse_key=collapse_key),
            headers={"authorization": f"Bearer {token}"})
    except httpx.HTTPError as ex:
        # The device is not implicated by OUR network failing.
        log.warning("fcm send failed transport=%s", type(ex).__name__)
        return SendResult(Verdict.TRANSIENT)

    if response.status_code == 200:
        # 200 means ACCEPTED, not delivered — the same posture as an APNs 200.
        # There is no delivery receipt in the response; delivery data exists only
        # in the BigQuery export.
        return SendResult(Verdict.DELIVERED)

    try:
        error_code = _fcm_error_code(response.json())
    except ValueError:
        error_code = ""
    verdict = _verdict(response.status_code, error_code)

    # A REFUSED BEARER IS NOT A REUSABLE ONE (Tesla r2, Carnot r4 — two families
    # independently, which is what makes it not optional).
    #
    # `_access_token` negative-caches failures of the OAuth EXCHANGE, but nothing
    # invalidated a token that the exchange handed us happily and the MESSAGE
    # endpoint then refused. A revoked key, a disabled service account or a
    # permission change produces 401/403 on every send while the cached bearer sits
    # valid-by-the-clock for up to its full lifetime — so every Android send in the
    # fanout, and every ring after it, fails identically until the cache ages out,
    # with no retry that could ever succeed. `delivered_to=0` screams the whole
    # time and names nothing an operator can act on.
    #
    # Dropping the cache entry is the smallest correct move: the NEXT send re-mints,
    # which either succeeds (the refusal was transient or the key was rotated back)
    # or fails at the exchange, where the existing 60s backoff takes over and does
    # the rate-limiting properly. We deliberately do NOT add a second backoff here
    # — one door for that decision, and it already exists one layer down.
    #
    # 403 is included because Google returns it for credential-shaped refusals
    # (SERVICE_DISABLED, PERMISSION_DENIED), not only for per-message policy. The
    # cost of being wrong is one extra token mint; the cost of the other direction
    # is an island deaf to Android for the better part of an hour.
    if response.status_code in (401, 403):
        global _cached_access_token
        if _cached_access_token is not None:
            log.warning(
                "fcm dropped the cached access token after status=%s — it was "
                "accepted by the OAuth exchange and refused by the message "
                "endpoint, so it cannot be reused", response.status_code)
            _cached_access_token = None

    # NEVER the device token: it rides in the request body, so nothing else in
    # this path can leak it and this line must not be the exception. ERROR for
    # REJECTED because that is the quietest failure mode here and the one where a
    # wrong project id or a malformed message hides.
    log.log(logging.ERROR if verdict is Verdict.REJECTED else logging.WARNING,
            "fcm refused status=%s error_code=%s verdict=%s",
            response.status_code, error_code or "-", verdict.value)
    return SendResult(verdict, _reap_order_for(verdict))
