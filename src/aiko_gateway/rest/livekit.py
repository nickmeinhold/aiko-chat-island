"""Video/audio (LiveKit) endpoints — the island as room-token authorizer.

``POST /v1/channels/{channel_id}/video-token`` mints a LiveKit JOIN token for the
authenticated caller, scoped to the channel-as-room, IFF the caller may read that
channel. The room is the resolved ``channel.id``, so a LiveKit room is namespaced to
an aiko channel and the membership gate is meaningful: a private DM's video room is
joinable only by its members.

Trust-boundary properties, from the codebase's established patterns:

  * **ACL gate before mint (existence-hiding).** ``acl.readable_channel`` collapses
    "no such channel" and "private channel you're not a member of" into the SAME
    ``None`` → identical 404, so probing ids leaks nothing. The token is never issued
    for a room the caller can't enter.
  * **READ ≠ PUBLISH (cage-match #122).** Read access must not mint a broadcast
    capability. Publish (camera/mic) is gated on ``acl.is_posting_member`` — an
    EXPLICIT posting membership on both public and private channels — so a read-only
    member (``can_post=False``) OR a public-channel non-member gets a SUBSCRIBE-ONLY
    token, never ``canPublish``. Live broadcast is a higher-trust medium than a text
    line, so it is deliberately stricter than ``can_post`` (which is post-open for
    public channels). The data channel is off by default (undesigned side-channel).
  * **Server-derived identity.** The LiveKit participant identity is ``user.id`` from
    ``CurrentUser`` — never a request field (I5).

If the island has no LiveKit credentials, the capability is disabled → 503, not 500.

Rate-limited (cage-match #122): minting a join+publish bearer is a capability
surface, so it goes through the same per-IP bucket as the other auth ceremonies.
The token response is marked ``Cache-Control: no-store`` — it carries a bearer
credential that must not be cached by any intermediary (Wu).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel

from ..config import settings
from ..domain import acl, livekit_tokens, memberships_service, moderation_service
from ..domain.rate_limit import rate_limit
from .deps import CurrentUser, DbSession

log = logging.getLogger("aiko_gateway.video")

router = APIRouter(prefix="/v1", tags=["video"])


class VideoTokenResponse(BaseModel):
    """The minted join token + where to use it. Typed so the wire contract with the
    app tab (#2726) is explicit and can't silently drift (cage-match #122)."""
    token: str
    url: str
    room: str
    can_publish: bool  # echoes the grant the caller actually received (read-only → False)


@router.post(
    "/channels/{channel_id}/video-token",
    response_model=VideoTokenResponse,
    dependencies=[rate_limit("video_token")],
)
async def create_video_token(
    channel_id: str, user: CurrentUser, session: DbSession, response: Response
) -> VideoTokenResponse:
    # Capability gate FIRST (cage-match #122 rd5 Wu F4): is_configured() is a pure
    # settings read, and EVERY island returns 503 until the creds deploy — so short-
    # circuit before the ACL queries rather than doing two DB lookups for a guaranteed
    # 503. Existence-hiding is preserved: the 503 is channel-independent (it never
    # depends on whether the channel exists or the caller is a member), so it leaks
    # nothing the 404 path protects.
    if not livekit_tokens.is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "video is not enabled on this island"
        )

    # Membership/existence gate — a non-member of a private channel (or a missing
    # channel) is the same existence-hiding 404 as everywhere else.
    channel = await acl.readable_channel(session, user.id, channel_id)
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")

    # BLOCK LAYER (cage-match #122 rd6 Wu #1): readability is NECESSARY, not
    # SUFFICIENT — the block primitive must traverse the video path exactly as it
    # traverses message delivery (main.py fanout excludes blocked pairs). For a 2-party
    # DM, a block in EITHER direction denies the join outright — same existence-hiding
    # 404 as a non-member — so a blocked user can't watch the blocker's live camera.
    # is_blocked_between is the single door the reaction/mention surfaces also pass.
    # MULTI-PARTY residual (named, #2731): a pairwise block in a group room can't be
    # enforced at a room-level token (all-or-nothing subscription) — it needs selective
    # per-track subscription, the analog of per-recipient fanout exclusion (increment 2).
    if channel.kind == "dm":
        members = await memberships_service.list_members(
            session, channel_id=channel.id, actor_id=user.id)
        for m, _u in members:
            if m.user_id != user.id and await moderation_service.is_blocked_between(
                    session, user.id, m.user_id):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")

    # READ ≠ PUBLISH: publish (live camera/mic) requires an EXPLICIT posting
    # membership (acl.is_posting_member) on BOTH public and private channels — a
    # read-only member OR a public-channel non-member gets subscribe-only. Broadcast
    # is a higher-trust medium than a text line, so it is NOT open to every reader of
    # a post-open public channel (cage-match #122 Wu/Tesla blast-radius). Derive the
    # room from the RESOLVED row (channel.id), never the raw path param.
    can_publish = await acl.is_posting_member(session, user.id, channel)
    # Pass the BARE ids — the door (mint_room_token) namespaces room AND identity by
    # gateway_id on the shared SFU, so isolation is enforced in one place, not per
    # caller (cage-match #122 rd4 Wu #1). The response echoes the SAME namespaced room
    # via room_for_channel (one source of truth, no route/door drift).
    try:
        token = livekit_tokens.mint_room_token(
            identity=user.id,
            display_name=user.display_name,
            room=channel.id,
            can_publish=can_publish,
            can_subscribe=True,
            can_publish_data=False,  # undesigned side-channel — off until moderation semantics exist
        )
    except livekit_tokens.LiveKitNotConfigured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "video is not enabled on this island"
        )

    # Audit the capability mint (cage-match #122 rd6 Wu #4): a trust-boundary that
    # issues bearer grants must be answerable to "who held a publish grant for room X
    # at time T?". No token/secret in the log — just who, where, and the grant power.
    log.info("video-token mint user=%s channel=%s can_publish=%s",
             user.id, channel.id, can_publish)

    # A bearer credential — never cache it in a proxy/browser store.
    response.headers["Cache-Control"] = "no-store"
    return VideoTokenResponse(
        token=token, url=settings.livekit_url,
        room=livekit_tokens.room_for_channel(channel.id), can_publish=can_publish,
    )
