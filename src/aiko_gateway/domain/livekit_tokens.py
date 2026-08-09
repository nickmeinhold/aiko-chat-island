"""LiveKit join-token minting — the island as the AUTHORIZER of room access.

A LiveKit access token is a capability: an HS256 JWT, signed with the SFU's API
secret, whose ``video`` grant says WHICH room the bearer may enter and WHAT powers
they hold (publish / subscribe / publish-data). LiveKit validates ``iss`` == the
API key and the signature, then honors the grant verbatim — so whoever mints the
token decides the access. That authorizer is this island.

Two invariants bound the token to the caller's real access AT MINT TIME. (Scope
honestly, cage-match #122 Tesla+Wu: this is mint-time authorization, NOT continuous.
A LiveKit join token is checked only at CONNECT and the media session outlives it, so
a ban/kick/leave AFTER connect does not revoke an already-joined session — that needs
the LiveKit room API (disconnect-on-ban) and is increment 2. So: the token can never
out-scope the caller's access *at the moment it is minted*; session-lifetime
revocation is a separate, not-yet-built gate.)

  * **Server-derived identity (I5).** The participant ``identity`` (the token
    ``sub``) is ALWAYS the authenticated user's id, passed in by the route from
    ``CurrentUser`` — never a client body field. A client cannot join as someone
    else, exactly like ``messages.sender_user_id`` / ``devices``.
  * **Room == an aiko channel, gated by the ACL.** The route resolves the room via
    ``acl.readable_channel`` BEFORE calling in here, so a token is never issued for
    a room the caller could not otherwise enter. Minting does NOT re-check the ACL
    (single responsibility) — it TRUSTS that the route gated it, the same division
    the message/reaction write paths use.

This is the single door: the route and any future in-process caller mint through
``mint_room_token`` so the grant policy lives in exactly one place — and because the
DEFAULTS are least-privilege (subscribe-only), a caller widens the grant only by an
explicit, visible kwarg.

Named residuals (cage-match #122, honest scope, NOT closed here):
  * **Shared-key = one compromise domain (Tesla).** The imagineering SFU is shared
    across islands on ONE HS256 API secret. ``gateway_id`` namespacing prevents
    *accidental* room/identity collision, but it is NOT a cryptographic tenant
    boundary: whoever holds one island's secret can forge tokens for any island's
    rooms on that SFU. Per-island LiveKit API keys would make it a real boundary;
    until then, the isolation is operator discipline + shared-secret topology.
  * **``name`` is a mutable, non-unique label (Wu).** ``sub`` is the ULID identity
    (sound), but ``name`` = ``display_name``, which is not rate-limited — so any UI
    that renders ``name`` inherits the same impersonation surface as the chat line.
    A video room is a higher-trust context; treat rendered ``name`` accordingly.
"""
from __future__ import annotations

import datetime as dt

import jwt

from ..config import settings

# LiveKit REQUIRES HS256 (the token is validated with the shared API secret). This
# is a hard constant, not ``settings.jwt_algorithm``: the island's *own* auth alg
# and LiveKit's are independent contracts, and pinning it here means an env change
# to the island's JWT alg can never silently change how a LiveKit token is signed.
_LIVEKIT_ALG = "HS256"

# A small backdating of `nbf` so a fresh token isn't rejected by an SFU whose clock
# trails the island's by a second or two (cage-match #122 Tesla). Bounded and tiny —
# it does not meaningfully widen the token's validity window.
_NBF_LEEWAY_SECONDS = 10


class LiveKitNotConfigured(RuntimeError):
    """This island has no LiveKit API key/secret — the video capability is not
    enabled on this deployment. The route maps this to 503 (capability disabled),
    never a 500: an unconfigured optional feature is an expected state, not a bug."""


def is_configured() -> bool:
    """True iff both the LiveKit API key and secret are set. Both are required to
    mint a token LiveKit will accept."""
    return bool(settings.livekit_api_key and settings.livekit_api_secret)


# The only track sources a publish grant permits: camera + mic. LiveKit's
# ``canPublishSources`` SUPERSEDES ``canPublish`` when set, so restricting it to A/V
# denies screen-share and other sources for the social skeleton (cage-match #122
# Carnot). Widening (e.g. screen-share) is a deliberate future kwarg, not a default.
_AV_PUBLISH_SOURCES = ["camera", "microphone"]


def mint_room_token(
    *,
    identity: str,
    display_name: str,
    room: str,
    can_publish: bool = False,
    can_subscribe: bool = True,
    can_publish_data: bool = False,
) -> str:
    """Mint a LiveKit join token for participant ``identity`` scoped to ``room``.

    ``iss`` is the API key, ``sub`` the participant identity, ``video`` the grant.
    ``nbf``/``exp`` bound the JOIN window (short — the media session outlives the
    token; it is only checked at connect). Raises ``LiveKitNotConfigured`` if the
    island has no credentials, so the capability is disabled cleanly rather than
    signing with an empty secret.

    LEAST-PRIVILEGE DEFAULTS (cage-match #122 rd2, Tesla+Wu): a bare three-kwarg mint
    is SUBSCRIBE-ONLY — never publish, never the data side-channel — so a future
    in-process caller must EXPLICITLY opt up. When ``can_publish`` is set, the grant
    restricts track sources to camera+mic (no screen-share). The grant is never an
    admin grant: no ``roomCreate``/``roomAdmin``/``roomList``.

    Fails closed on an empty/whitespace ``identity`` or ``room`` (Tesla #6): the door
    never signs a live capability for a blank participant or an unscoped room, even if
    a future caller forgets to validate — the route already passes DB-backed ids.
    """
    if not is_configured():
        raise LiveKitNotConfigured("LiveKit API key/secret not set on this island")
    if not identity or not identity.strip():
        raise ValueError("mint_room_token: identity must be non-empty")
    if not room or not room.strip():
        raise ValueError("mint_room_token: room must be non-empty")

    now = dt.datetime.now(dt.timezone.utc)
    grant = {
        "room": room,
        "roomJoin": True,
        "canPublish": can_publish,
        "canSubscribe": can_subscribe,
        "canPublishData": can_publish_data,
    }
    # Restrict publishable sources to A/V only when publishing is allowed (supersedes
    # canPublish for source selection). Omitted when not publishing — canPublish=False
    # already denies all sources, and an empty list would be ambiguous.
    if can_publish:
        grant["canPublishSources"] = _AV_PUBLISH_SOURCES
    payload = {
        "iss": settings.livekit_api_key,
        "sub": identity,
        "name": display_name,
        "nbf": int(now.timestamp()) - _NBF_LEEWAY_SECONDS,
        "exp": int((now + dt.timedelta(seconds=settings.livekit_token_ttl_seconds)).timestamp()),
        "video": grant,  # LiveKit VideoGrant — one room, participant powers, never admin
    }
    return jwt.encode(payload, settings.livekit_api_secret, algorithm=_LIVEKIT_ALG)
