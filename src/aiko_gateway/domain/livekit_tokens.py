"""LiveKit join-token minting — the island as the AUTHORIZER of room access.

A LiveKit access token is a capability: an HS256 JWT, signed with the SFU's API
secret, whose ``video`` grant says WHICH room the bearer may enter and WHAT powers
they hold (publish / subscribe / publish-data). LiveKit validates ``iss`` == the
API key and the signature, then honors the grant verbatim — so whoever mints the
token decides the access. That authorizer is this island.

Two invariants make the token unable to out-scope the caller's real access:

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
``mint_room_token`` so the grant policy lives in exactly one place.
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


class LiveKitNotConfigured(RuntimeError):
    """This island has no LiveKit API key/secret — the video capability is not
    enabled on this deployment. The route maps this to 503 (capability disabled),
    never a 500: an unconfigured optional feature is an expected state, not a bug."""


def is_configured() -> bool:
    """True iff both the LiveKit API key and secret are set. Both are required to
    mint a token LiveKit will accept."""
    return bool(settings.livekit_api_key and settings.livekit_api_secret)


def mint_room_token(
    *,
    identity: str,
    display_name: str,
    room: str,
    can_publish: bool = True,
    can_subscribe: bool = True,
    can_publish_data: bool = True,
) -> str:
    """Mint a LiveKit join token for participant ``identity`` scoped to ``room``.

    ``iss`` is the API key, ``sub`` the participant identity, ``video`` the grant.
    ``nbf``/``exp`` bound the JOIN window (short — the media session outlives the
    token; it is only checked at connect). Raises ``LiveKitNotConfigured`` if the
    island has no credentials, so the capability is disabled cleanly rather than
    signing with an empty secret.

    The grant is deliberately the CALLER'S powers, not an admin grant: no
    ``roomCreate``/``roomAdmin``/``roomList`` — a participant token can join and
    (by default) publish+subscribe in exactly one room, nothing else.
    """
    if not is_configured():
        raise LiveKitNotConfigured("LiveKit API key/secret not set on this island")

    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "iss": settings.livekit_api_key,
        "sub": identity,
        "name": display_name,
        "nbf": int(now.timestamp()),
        "exp": int((now + dt.timedelta(seconds=settings.livekit_token_ttl_seconds)).timestamp()),
        # LiveKit's VideoGrant claim. Scoped to ONE room; only join + the requested
        # publish/subscribe powers, never room-admin.
        "video": {
            "room": room,
            "roomJoin": True,
            "canPublish": can_publish,
            "canSubscribe": can_subscribe,
            "canPublishData": can_publish_data,
        },
    }
    return jwt.encode(payload, settings.livekit_api_secret, algorithm=_LIVEKIT_ALG)
