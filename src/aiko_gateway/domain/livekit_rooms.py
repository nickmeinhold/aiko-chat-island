"""LiveKit RoomService reads — the island asking the SFU a present-tense question.

Until this module, the island was a pure AUTHORIZER: it signed capabilities
(``livekit_tokens``) and never spoke to the SFU. ``GET /v1/channels/{id}/call``
(#3159) is the first time the island becomes a CLIENT of the SFU, and that is a
boundary worth naming rather than sliding across:

  * **A network call now sits in a request path.** The SFU being slow makes a gateway
    request slow, so every call here is bounded by ``_TIMEOUT_SECONDS`` — short,
    because the app polls this roughly once a second while a phone is ringing.
  * **Unknown is not the same as empty.** If the SFU cannot be reached, this module
    raises rather than reporting an empty room. The endpoint exists so a ring can STOP;
    a fabricated "nobody is here" cancels a real call, which is the failure the feature
    was built to prevent. Weak-signal reads fail OPEN (say "I don't know"), the inverse
    of the fail-CLOSED rule for irreversible mutations.
  * **No new dependency.** The LiveKit server SDK is not needed: the RoomService is a
    Twirp endpoint taking a JSON body and a bearer token, and this island already
    hand-rolls the HS256 token. ``httpx`` is already a main dependency.

Measured against the live imagineering SFU, 2026-09-07:
  * ``POST /twirp/livekit.RoomService/ListParticipants`` is reachable over 443,
    ~175ms round trip from off-box.
  * The grant MUST be ``{"roomAdmin": true, "room": "<the room>"}``. A broad
    ``roomList``-only token is rejected 401 "permissions denied". The per-room scope is
    therefore enforced by the SFU, not merely asserted by us.
  * A room that does not exist returns HTTP 200 ``{"participants": []}`` — identical to
    an existing-but-empty room. Good: the route's own ACL has already decided what the
    caller may know, so the SFU cannot leak existence back to us for us to relay.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import httpx

from ..config import settings
from . import livekit_tokens

log = logging.getLogger("aiko_gateway.video")

# Short by design: the app polls this ~1/s for the duration of a ring, so a slow SFU
# must degrade to "I don't know" quickly rather than queueing requests behind it.
#
# STATED PER-PHASE BECAUSE A SCALAR IS NOT A DEADLINE (cage-match #167, Tesla; part A
# item 6). ``httpx.Timeout(2.0)`` sets connect/read/write/pool to 2.0s EACH, so the
# worst-case wall clock is their SUM, not 2.0s. The comment this replaces claimed
# "~11x headroom" on a 0.175s round trip; against a 1/s poll the real ceiling was 8s,
# during which the next seven polls had already been issued. The phases are therefore
# named individually and the bound is arithmetic a reader can check:
#
#     connect 1.0 + read 1.5 + write 0.5 + pool 0.5  =  3.5s worst case
#
# ``read`` is the only phase a slow SFU can actually stretch, and 1.5s is ~8.5x the
# measured 0.175s round trip. ``pool`` is short on purpose: if the pool is saturated
# because earlier polls are still in flight, waiting is exactly the wrong move — the
# honest answer is "I cannot tell you right now", which the route already renders as
# 503 and which the ring already has a code path for.
_TIMEOUT = httpx.Timeout(connect=1.0, read=1.5, write=0.5, pool=0.5)
_TIMEOUT_WORST_CASE_SECONDS = 3.5  # the sum above, asserted by a test so it cannot drift

_LIST_PARTICIPANTS_PATH = "/twirp/livekit.RoomService/ListParticipants"


class LiveKitUnreachable(RuntimeError):
    """The SFU could not be asked — network failure, timeout, or a non-200 answer.

    Deliberately NOT collapsed into "the room is empty". The caller maps this to 503
    ("cannot determine"), never to ``live: false``.
    """


@dataclass(frozen=True)
class Occupancy:
    """Present-tense truth about one room. ``since`` is None when nobody is in it.

    ``since`` is the FINISHED wire string, not a number the caller must still convert.
    That is the fix for a CLASS, not a preference (cage-match #167 r3, Carnot): the
    route used to receive epoch milliseconds and call ``datetime.fromtimestamp`` itself,
    OUTSIDE the try that maps SFU failure to 503 — so a payload like
    ``{"joinedAt": "999999999999999999999"}`` raised OverflowError at the route and
    escaped as a 500, violating the same "malformed SFU means cannot-determine, never a
    gateway error" invariant that round 1's shape bug violated. Two instances, one
    class: SFU-derived values were being computed after the boundary that is supposed to
    contain them. Rendering here means there is no SFU-derived arithmetic left outside
    it to get wrong.
    """
    live: bool
    participants: int
    since: str | None


def _api_base() -> str:
    """Derive the RoomService HTTP origin from the configured ``wss://`` client URL.

    The SFU serves its client websocket and its Twirp API on the SAME origin, so the
    one configured value covers both and there is no second setting to drift. Scheme is
    mapped wss->https / ws->http; anything else is a config error the boot validator
    already refuses (``livekit_url`` must be wss:// for a remote SFU).

    THE PATH IS CARRIED, NOT DISCARDED (cage-match #167, Tesla; part A item 3). The
    first version built the origin from scheme+netloc alone, so the day an island is
    configured ``wss://host/livekit`` — a perfectly ordinary reverse-proxy layout — the
    Twirp call would go to ``https://host/twirp/...`` instead of
    ``https://host/livekit/twirp/...``. That failure is nastier than it sounds: every
    poll would 503 "correctly" (the module's honest cannot-tell), while ``video-token``
    kept working, because tokens are minted locally and never touch this base. A whole
    feature silently dark behind a truthful error message, with the one endpoint an
    operator would test to check LiveKit health still green.

    The trailing slash is stripped so ``wss://host/livekit/`` and ``wss://host/livekit``
    produce the same base — the path constants below all start with ``/``.
    """
    parts = urlparse(settings.livekit_url)
    scheme = {"wss": "https", "ws": "http"}.get(parts.scheme)
    if scheme is None or not parts.hostname:
        raise LiveKitUnreachable(
            f"livekit_url is not a usable websocket URL: {settings.livekit_url!r}")
    return urlunparse((scheme, parts.netloc, parts.path.rstrip("/"), "", "", ""))


# One pooled client, not one per request. MEASURED against the live imagineering
# SFU, 2026-09-07, 16 samples per arm interleaved to control for network drift:
#
#     fresh AsyncClient per request : median 198.1 ms
#     shared AsyncClient            : median  57.9 ms
#
# 140ms per poll, a 3.4x speedup, almost all of it TCP+TLS handshake. That is not a
# micro-optimisation here: this endpoint is polled about once a second by a RINGING
# phone, and the whole feature exists to stop a ring PROMPTLY. Re-handshaking on
# every poll spends the latency budget of the thing being built. Same singleton +
# aclose() shape as ``domain/apns.py`` — the app lifespan closes it on shutdown, so
# this follows the repo's existing convention rather than adding a second one.
_client_singleton: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    """The pooled client, created on first use."""
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = httpx.AsyncClient(timeout=_TIMEOUT)
    return _client_singleton


async def aclose() -> None:
    """Close the pooled client. Called from the app lifespan on shutdown."""
    global _client_singleton
    if _client_singleton is not None:
        await _client_singleton.aclose()
        _client_singleton = None


def _iso_or_unreachable(epoch_seconds: int) -> str:
    """Render a Unix timestamp as ISO-8601 UTC, or declare the payload unreadable.

    NO MAGIC RANGE CONSTANT. The question is not "is this number smaller than some
    bound I picked" — it is "can this actually be rendered as a time?", and the honest
    way to answer that is to render it and see. A bound chosen by hand would be a
    constant whose meaning drifts with the platform it was chosen on; ``fromtimestamp``
    already knows its own limits (it raises OverflowError past platform ``time_t``).

    A stamp we cannot render is treated exactly like a wrong-typed field: the SFU said
    something we do not understand, so the answer is "cannot determine" (503), never a
    gateway 500 and never a fabricated time.

    The ``Z`` suffix is the app tab's specified contract for this field
    (claude-tasks#3159 shows ``"since": "2026-08-15T13:22:04.113Z"``). Python has no
    stdlib call that emits it — ``isoformat(z=True)`` is a TypeError and ``%Z`` yields
    the zone NAME ("UTC") — so the replace is the idiom, not a workaround.
    """
    try:
        moment = dt.datetime.fromtimestamp(epoch_seconds, dt.timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise LiveKitUnreachable(
            f"SFU reported an unrenderable joinedAt ({epoch_seconds})") from exc
    return moment.isoformat().replace("+00:00", "Z")


def _participants_from(body: object) -> list[dict]:
    """Validate the SFU's payload SHAPE, and refuse to guess when it is not what we expect.

    THIS FUNCTION EXISTS BECAUSE THE MODULE'S OWN PRINCIPLE WAS ONLY PROSE. The
    docstring above promises that an SFU we cannot understand yields "I don't know",
    never "nobody is here" — but the first version of this parser read
    ``resp.json().get("participants") or []``, which quietly turned a MISSING or
    ``null`` field into an empty room and therefore into ``live: false`` on a ringing
    handset. That is the exact inversion this endpoint exists to prevent, arriving
    through protocol drift instead of through a network failure. (Found by Carnot in
    the cage-match; confirmed executably before fixing.)

    The same read also assumed ``participants`` was a list of dicts, so a body like
    ``{"participants": ["alice"]}`` raised ``AttributeError`` and escaped as a 500
    rather than the promised 503. Both are one defect: the vendor payload was being
    read as an internal typed object instead of as untrusted protocol data. So the
    shape is checked ONCE, here, and every failure to match is the same
    ``LiveKitUnreachable`` the network path raises — "cannot determine" has a single
    meaning regardless of which way the answer failed to arrive.

    REQUIRING the key is safe, and that is a measurement rather than an assumption:
    protobuf-JSON commonly omits empty repeated fields, which would make this 503 on
    every empty room and break the feature outright. Checked at the byte level against
    BOTH live islands, 2026-09-07 — an empty room returns literally
    ``{"participants":[]}`` on each. If a future LiveKit changes its marshaller to omit
    the field, this fails LOUDLY (503, "cannot tell") instead of silently cancelling
    live calls, which is the correct direction to break in.
    """
    if not isinstance(body, dict):
        raise LiveKitUnreachable(f"SFU returned a {type(body).__name__}, not an object")
    if "participants" not in body:
        # NOT an empty room. A response missing the field is one we do not understand.
        raise LiveKitUnreachable("SFU response has no 'participants' field")
    participants = body["participants"]
    if not isinstance(participants, list):
        raise LiveKitUnreachable(
            f"SFU 'participants' is a {type(participants).__name__}, not a list")
    for p in participants:
        if not isinstance(p, dict):
            raise LiveKitUnreachable(
                f"SFU participant entry is a {type(p).__name__}, not an object")
        if not p:
            # An entry with NO fields at all is not a person (cage-match #167, Tesla;
            # part A item 4). Counting it inflates ``participants`` and can hold
            # ``live`` true on a payload we plainly do not understand.
            #
            # THE BREAK DIRECTION IS THE ARGUMENT, and it is the same one this module
            # already makes for a missing ``participants`` key. Refusing means 503
            # "cannot tell" — loud, and the ring still stops at its own ceiling.
            # Counting means a silent phantom occupant. Loud beats silent.
            #
            # HONESTLY UNMEASURED, and this is the right place to say so: ``live: true``
            # has only ever been produced by a mock (the PR's own stated residual), so
            # no real occupied room has been inspected at the byte level the way the
            # empty-room shape was. If LiveKit ever marshals a real participant whose
            # every field is at its protobuf default, this turns that room into a 503.
            # The two-handset arm is where that gets measured; until then the failure
            # is bounded (a degraded ring, never a cancelled one) and visible.
            raise LiveKitUnreachable("SFU participant entry has no fields")
    return participants


async def occupancy(*, room: str) -> Occupancy:
    """Ask the SFU who is in ``room`` RIGHT NOW. ``room`` is the BARE channel id.

    Namespacing is applied inside ``mint_room_admin_token`` (the token's grant names the
    namespaced room) and again here for the request body, from the same
    ``room_for_channel`` helper the mint path uses — one source of truth, so a token and
    the body it accompanies can never disagree about which room is meant.

    ``since_ms`` is the EARLIEST join time among the participants currently present, not
    the room's creation time. That is deliberate: LiveKit keeps an emptied room alive for
    ``empty_timeout`` (300s by default, and neither island overrides it), so
    ``creationTime`` can belong to a call that already ended while a new one has since
    started in the same room object. The earliest current join answers the question the
    app actually asks — "is this still the call I was rung for?" — far better than a
    creationTime that can outlive the call it belongs to.

    STATED HONESTLY, because the first version of this paragraph did not: `since` moves
    when the EARLIEST CURRENT participant leaves, which is not the same event as "the
    original participants have all gone". In a 2-party DM where the caller drops and
    rejoins, `since` advances to the callee's join time while the same call continues.
    A client comparing `since` for identity should treat a forward jump as "possibly
    still the same call" rather than proof of a new one. Making `since` immovable would
    require the island to hold per-call state, which is exactly what the design declines
    to do — so the limitation is inherent to a stateless read, not an oversight, and the
    app tab should have it in the contract rather than discover it.
    """
    token = livekit_tokens.mint_room_admin_token(room=room)
    namespaced = livekit_tokens.room_for_channel(room)
    url = _api_base() + _LIST_PARTICIPANTS_PATH
    try:
        resp = await _client().post(
            url,
            json={"room": namespaced},
            headers={"Authorization": f"Bearer {token}"},
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        # RuntimeError IS IN THIS TUPLE ON PURPOSE (cage-match #167, Tesla; part A
        # item 5). ``aclose()`` nulls the singleton at shutdown, but a request that
        # already grabbed the reference keeps a handle on the now-closed client, and
        # httpx answers that with ``RuntimeError("Cannot send a request, as the client
        # has been closed.")`` — which is NOT an ``httpx.HTTPError``. Uncaught, a poll
        # racing shutdown escaped as a 500: a gateway error blamed on the SFU's
        # behalf, on the one endpoint whose entire contract is that it either knows or
        # honestly says it does not. A client closed underneath us is precisely
        # "cannot tell", so it renders as 503 like every other way the answer failed
        # to arrive.
        #
        # NOTE FOR ANYONE WIDENING THIS TRY: ``LiveKitUnreachable`` subclasses
        # RuntimeError, so anything raising it from INSIDE this block would be caught
        # here and re-wrapped, losing its own message. Today nothing does — the shape
        # and stamp refusals all happen below — and a guard against a state that cannot
        # occur was deleted rather than kept, because its comment would have asserted a
        # mechanism that never fires. ``test_a_shape_refusal_keeps_its_own_message``
        # goes red the moment that stops being true.
        #
        # Log the class, never the token. A ringing handset polls this, so a persistent
        # SFU outage would otherwise write one line per second per ring.
        log.warning("livekit ListParticipants failed room=%s err=%s",
                    namespaced, type(exc).__name__)
        raise LiveKitUnreachable(f"SFU request failed: {type(exc).__name__}") from exc

    if resp.status_code != 200:
        log.warning("livekit ListParticipants http=%s room=%s",
                    resp.status_code, namespaced)
        raise LiveKitUnreachable(f"SFU returned HTTP {resp.status_code}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise LiveKitUnreachable("SFU returned a non-JSON body") from exc
    participants = _participants_from(body)

    # joinedAt is Unix SECONDS in LiveKit's protobuf-JSON, and absent/0 for a participant
    # the SFU has not stamped. Drop unusable stamps rather than letting a 0 masquerade as
    # 1970 — an absent stamp must not make `since_ms` older than the call.
    # TWO CATEGORIES, and the line between them is protobuf's, not ours (cage-match
    # #167 r3). ABSENT or ZERO is protobuf-JSON's way of saying "no value" — an
    # unstamped participant is ordinary and is simply skipped, so a room full of
    # unstamped peers reports live=True with since=None rather than failing. Anything
    # ELSE that is present must be readable: a negative, unparseable, or unrenderable
    # stamp is the SFU asserting a time we cannot read, which is the same "payload we
    # do not understand" as a wrong-typed field and gets the same 503.
    #
    # An earlier version dropped negative and unparseable stamps while raising on
    # out-of-range ones — three equally malformed values, treated three ways, for no
    # reason a reader could state. Consistency here is the actual fix; the OverflowError
    # Carnot found was one arbitrary branch of it.
    joined = []
    for p in participants:
        raw = p.get("joinedAt")
        if raw is None or raw == 0 or raw == "0":
            continue                      # protobuf "no value" — unstamped, not broken
        try:
            stamp = int(raw)
        except (TypeError, ValueError) as exc:
            raise LiveKitUnreachable(
                f"SFU sent an unreadable joinedAt ({raw!r})") from exc
        if stamp <= 0:
            raise LiveKitUnreachable(f"SFU sent a non-positive joinedAt ({stamp})")
        joined.append(stamp)
    return Occupancy(
        live=bool(participants),
        participants=len(participants),
        since=_iso_or_unreachable(min(joined)) if joined else None,
    )
