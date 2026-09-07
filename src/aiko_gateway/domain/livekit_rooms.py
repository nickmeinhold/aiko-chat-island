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

import logging
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import httpx

from ..config import settings
from . import livekit_tokens

log = logging.getLogger("aiko_gateway.video")

# Short by design: the app polls this ~1/s for the duration of a ring, so a slow SFU
# must degrade to "I don't know" quickly rather than queueing requests behind it. The
# measured round trip is ~0.175s, so this is ~11x headroom, not a tight race.
_TIMEOUT_SECONDS = 2.0

_LIST_PARTICIPANTS_PATH = "/twirp/livekit.RoomService/ListParticipants"


class LiveKitUnreachable(RuntimeError):
    """The SFU could not be asked — network failure, timeout, or a non-200 answer.

    Deliberately NOT collapsed into "the room is empty". The caller maps this to 503
    ("cannot determine"), never to ``live: false``.
    """


@dataclass(frozen=True)
class Occupancy:
    """Present-tense truth about one room. ``since_ms`` is None when nobody is in it."""
    live: bool
    participants: int
    since_ms: int | None


def _api_base() -> str:
    """Derive the RoomService HTTP origin from the configured ``wss://`` client URL.

    The SFU serves its client websocket and its Twirp API on the SAME origin, so the
    one configured value covers both and there is no second setting to drift. Scheme is
    mapped wss->https / ws->http; anything else is a config error the boot validator
    already refuses (``livekit_url`` must be wss:// for a remote SFU).
    """
    parts = urlparse(settings.livekit_url)
    scheme = {"wss": "https", "ws": "http"}.get(parts.scheme)
    if scheme is None or not parts.hostname:
        raise LiveKitUnreachable(
            f"livekit_url is not a usable websocket URL: {settings.livekit_url!r}")
    return urlunparse((scheme, parts.netloc, "", "", "", ""))


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
    app actually asks — "is this still the call I was rung for?" — and moves, correctly,
    when the original participants have all gone and a different call has begun.
    """
    token = livekit_tokens.mint_room_admin_token(room=room)
    namespaced = livekit_tokens.room_for_channel(room)
    url = _api_base() + _LIST_PARTICIPANTS_PATH
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                url,
                json={"room": namespaced},
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
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
        participants = resp.json().get("participants") or []
    except ValueError as exc:
        raise LiveKitUnreachable("SFU returned a non-JSON body") from exc

    # joinedAt is Unix SECONDS in LiveKit's protobuf-JSON, and absent/0 for a participant
    # the SFU has not stamped. Drop unusable stamps rather than letting a 0 masquerade as
    # 1970 — an absent stamp must not make `since_ms` older than the call.
    joined = []
    for p in participants:
        raw = p.get("joinedAt")
        # Bind the value ONCE. An earlier version guarded with .get() and then indexed
        # with [], so a participant carrying no stamp at all raised KeyError instead of
        # being skipped — the guard and the value were reading different expressions.
        if raw is None:
            continue
        try:
            stamp = int(raw)
        except (TypeError, ValueError):
            continue
        if stamp > 0:
            joined.append(stamp)
    return Occupancy(
        live=bool(participants),
        participants=len(participants),
        since_ms=min(joined) * 1000 if joined else None,
    )
