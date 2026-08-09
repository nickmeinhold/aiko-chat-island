"""Video/audio (LiveKit) endpoints — the island as room-token authorizer.

``POST /v1/channels/{channel_id}/video-token`` mints a LiveKit JOIN token for the
authenticated caller, scoped to the channel-as-room, IFF the caller may read that
channel. The room is the ``channel_id`` itself, so a LiveKit room is namespaced to
an aiko channel and the membership gate is meaningful: a private DM's video room is
joinable only by its members.

Two trust-boundary properties, both inherited from the codebase's established
patterns:

  * **ACL gate before mint (existence-hiding).** ``acl.readable_channel`` collapses
    "no such channel" and "private channel you're not a member of" into the SAME
    ``None`` → identical 404, so probing ids leaks nothing (the messages-read
    contract). The token is thus never issued for a room the caller can't enter.
  * **Server-derived identity.** The LiveKit participant identity is
    ``user.id`` from ``CurrentUser`` — never a request field (I5).

If the island has no LiveKit credentials, the capability is disabled → 503, not a
500 (``livekit_tokens.LiveKitNotConfigured``).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..config import settings
from ..domain import acl, livekit_tokens
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["video"])


@router.post("/channels/{channel_id}/video-token")
async def create_video_token(channel_id: str, user: CurrentUser, session: DbSession):
    # Membership/existence gate FIRST — a non-member of a private channel (or a
    # missing channel) is the same existence-hiding 404 as everywhere else. Only a
    # caller who may read the channel gets a token scoped to its room.
    channel = await acl.readable_channel(session, user.id, channel_id)
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")

    try:
        token = livekit_tokens.mint_room_token(
            identity=user.id,
            display_name=user.display_name,
            room=channel_id,
        )
    except livekit_tokens.LiveKitNotConfigured:
        # Optional capability not enabled on this deployment — expected state, not a bug.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "video is not enabled on this island"
        )

    return {"token": token, "url": settings.livekit_url, "room": channel_id}
