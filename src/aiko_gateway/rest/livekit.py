"""Video/audio (LiveKit) endpoints — the island as room-token authorizer.

``POST /v1/channels/{channel_id}/video-token`` mints a LiveKit JOIN token for the
authenticated caller, scoped to the channel-as-room. Increment 1 is **DM-ONLY**.

``GET /v1/channels/{channel_id}/call`` answers whether a call is happening in that
channel RIGHT NOW (#3159) — the present-tense half a signed invitation cannot supply.
It runs the SAME gate sequence, in the same order, for a reason worth stating once:
two endpoints about one object whose gates diverge turn the weaker into an oracle for
what the stronger hides. Change one, change both.

Trust-boundary properties, from the codebase's established patterns:

  * **ACL gate before mint (existence-hiding).** ``acl.readable_channel`` collapses
    "no such channel" and "private channel you're not a member of" into the SAME
    ``None`` → identical 404, so probing ids leaks nothing.
  * **DM-only (cage-match #122 rd7).** Only ``kind='dm'`` channels get video: a
    group/public room can't enforce pairwise BLOCKS at a room-level token (unbounded
    participants, all-or-nothing subscription), which would widen block semantics on a
    safety boundary. A readable non-DM channel is a clean 403; group video waits on
    selective subscription (#2731).
  * **BLOCK layer.** In a DM, a block in either direction denies the join (404) via
    ``moderation_service.is_blocked_between`` — the block primitive traverses the video
    path like it traverses message fanout.
  * **READ ≠ PUBLISH.** Publish (camera/mic) is gated on ``acl.is_posting_member`` (an
    explicit posting membership), so a read-only DM member is subscribe-only; the data
    channel is off by default (undesigned side-channel).
  * **Server-derived identity.** The LiveKit participant identity is ``user.id`` from
    ``CurrentUser`` — never a request field (I5).

If the island has no LiveKit credentials, the capability is disabled → 503, not 500.

Rate-limited (cage-match #122): minting a join+publish bearer is a capability
surface, so it goes through the same per-IP bucket as the other auth ceremonies.
The token response is marked ``Cache-Control: no-store`` — it carries a bearer
credential that must not be cached by any intermediary (Wu).
"""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select

from ..config import settings
from ..domain import acl, livekit_rooms, livekit_tokens, moderation_service
from ..domain.models import Membership
from ..domain.rate_limit import rate_limit
from .deps import CurrentUser, DbSession

log = logging.getLogger("aiko_gateway.video")

router = APIRouter(prefix="/v1", tags=["video"])


class CallOccupancyResponse(BaseModel):
    """Present-tense truth about a channel's call (#3159).

    COUNT ONLY, deliberately: the ring needs "is this call still happening", not "who is
    in it". Returning identities would make this a presence probe for any channel member
    — a strictly larger disclosure than the ring requires, on an endpoint the app polls
    once a second. The app tab specified it this way and the island agrees.
    """
    live: bool
    participants: int
    since: str | None = None  # ISO-8601 UTC; null when the room is empty


class VideoTokenResponse(BaseModel):
    """The minted join token + where to use it. Typed so the wire contract with the
    app tab (#2726) is explicit and can't silently drift (cage-match #122)."""
    token: str
    url: str
    room: str
    can_publish: bool  # echoes the grant the caller actually received (read-only → False)


async def _gated_dm_channel(session, user_id: str, channel_id: str):
    """THE ONE DOOR both video endpoints pass through. Returns the resolved channel.

    WHY THIS IS A FUNCTION AND NOT A CONVENTION (cage-match #167 r2, Carnot). The two
    endpoints answer questions about the same object, so a gate present on one and
    absent on the other turns the weaker into an oracle for what the stronger hides: a
    user blocked from minting a join token could still poll occupancy and learn exactly
    when the person who blocked them is on a call. The first version of this PR kept the
    two gate sequences in sync by hand and PROVED the agreement with property tests
    (``test_gates_agree_*``). That guards the window; it does not remove the coupling —
    a third endpoint, or a new safety check added to one caller, re-opens it and the
    tests only notice the cases they enumerate. This repo's own rule is to seal the
    shared door rather than each caller, so the gate lives here once and the property
    tests now verify the door is actually shared rather than that two copies still
    match.

    The order is load-bearing and unchanged from the reviewed original:

      1. **Existence-hiding.** ``acl.readable_channel`` collapses "no such channel" and
         "private channel you are not in" into the SAME ``None`` -> identical 404.
      2. **DM-only** (cage-match #122 rd7). A group/public room cannot enforce pairwise
         BLOCKS at a room-level token: participants are unbounded and LiveKit
         subscription is all-or-nothing, so a blocked user could watch the blocker's
         live camera. Fail closed to DMs until selective per-track subscription lands
         (#2731). Private is checked too as defence in depth: migration 0020's
         ``ck_channels_dm_private`` makes a public DM unrepresentable, so this branch is
         unreachable through the DB today, but a future writer path would make it
         reachable again.
      3. **2-party cardinality from GROUND TRUTH** (cage-match #122 rd9). Resolved from
         RAW ``Membership`` rows, never ``list_members`` — that is the visibility-shaped
         @-mention roster, and if it ever hides blocked or soft-deleted peers a
         list-based check would see only self and fail OPEN on the exact safety surface
         DM-only exists to protect.
      4. **Block in EITHER direction** -> existence-hiding 404, so the block primitive
         traverses the video paths exactly as it traverses message fanout.
    """
    channel = await acl.readable_channel(session, user_id, channel_id)
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")

    if channel.kind != "dm" or not channel.is_private:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "video is only available in direct messages")

    peer_ids = (await session.execute(
        select(Membership.user_id).where(
            Membership.channel_id == channel.id,
            Membership.user_id != user_id,
        )
    )).scalars().all()
    if len(peer_ids) != 1:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "video is only available in direct messages")
    if await moderation_service.is_blocked_between(session, user_id, peer_ids[0]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")

    return channel


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

    channel = await _gated_dm_channel(session, user.id, channel_id)

    # READ ≠ PUBLISH: publish (live camera/mic) requires an EXPLICIT posting
    # membership (acl.is_posting_member) on BOTH public and private channels — a
    # read-only member OR a public-channel non-member gets subscribe-only. Broadcast
    # is a higher-trust medium than a text line, so it is NOT open to every reader of
    # a post-open public channel (cage-match #122 Wu/Tesla blast-radius). Derive the
    # room from the RESOLVED row (channel.id), never the raw path param.
    can_publish = await acl.is_posting_member(session, user.id, channel)
    # Pass the BARE ids — the door (mint_room_token) namespaces room AND identity by
    # island_id on the shared SFU, so isolation is enforced in one place, not per
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


# The ring polls this for the length of a ring. The app tab's spec is ~1/s bounded by a
# 30s ring = up to 30 requests per ring per party, and caller and callee are frequently
# behind ONE NAT (same house, same office), so a shared-IP ring costs up to ~60. The auth
# default of 20/60s would 429 a legitimate call partway through ringing — which the app
# would most likely surface as the call dying for no reason. 150/60s carries two parties
# at 1/s with headroom for a retry and a second concurrent ring, while still bounding an
# abusive client to a read that costs one indexed DB lookup plus one ~175ms SFU call.
_OCCUPANCY_LIMIT_PER_WINDOW = 150


@router.get(
    "/channels/{channel_id}/call",
    response_model=CallOccupancyResponse,
    dependencies=[rate_limit("call_occupancy", limit=_OCCUPANCY_LIMIT_PER_WINDOW)],
)
async def get_call_occupancy(
    channel_id: str, user: CurrentUser, session: DbSession, response: Response
) -> CallOccupancyResponse:
    """Is a call happening in this channel RIGHT NOW? (#3159)

    A signed call invitation is a permanent claim about the PAST; call liveness is a fact
    about the PRESENT, and nothing in the message layer reconciles them. Without this, a
    caller who hangs up three seconds in leaves the callee ringing, answering, and landing
    alone in an empty room. This is the present-tense half.

    THE GATE ORDER IS video-token's, DELIBERATELY AND IDENTICALLY. Both endpoints answer
    questions about the same object, so if their gates ever diverge the weaker one becomes
    the way to learn what the stronger one hides — a caller blocked from minting a token
    could still watch a DM's occupancy and infer when two people are talking. Any change
    to one gate is a change to both.
    """
    if not livekit_tokens.is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "video is not enabled on this island"
        )

    channel = await _gated_dm_channel(session, user.id, channel_id)

    try:
        occ = await livekit_rooms.occupancy(room=channel.id)
    except (livekit_rooms.LiveKitUnreachable, livekit_tokens.LiveKitNotConfigured):
        # UNKNOWN IS NOT EMPTY. Reporting live=false here would tell a ringing handset the
        # call had ended, cancelling a call that is in fact happening — the precise failure
        # this endpoint exists to prevent, inverted. 503 says "I cannot tell", which the app
        # already has a code path for (it mirrors video-token's capability-disabled 503), and
        # a ring that cannot be told to stop still stops at its own duration ceiling.
        # no-store on the ERROR path too (cage-match #167 r2, Carnot). The success
        # path already refuses caching; a 503 left to default behaviour could be held
        # by an intermediary and replayed to a handset that is polling once a second,
        # which turns a momentary SFU blip into a persistent "cannot tell". The whole
        # endpoint is a present-tense read: NO response from it is ever cacheable.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "call state is temporarily unavailable",
            headers={"Cache-Control": "no-store"})

    # A ring polls this: a cached answer is a stale answer, and a stale answer is exactly
    # the bug. Never let an intermediary hold it.
    response.headers["Cache-Control"] = "no-store"
    since = (
        dt.datetime.fromtimestamp(occ.since_ms / 1000, dt.timezone.utc)
          .isoformat().replace("+00:00", "Z")
        if occ.since_ms is not None else None
    )
    return CallOccupancyResponse(
        live=occ.live, participants=occ.participants, since=since)
