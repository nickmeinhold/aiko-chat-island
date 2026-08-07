"""Emoji-reaction endpoints (#2634, v2 social layer) — SIGNED + IDENTITY-BEARING.

POST   /v1/messages/{message_id}/reactions   body {emoji, client_msg_id?, origin?}  → add (idempotent)
DELETE /v1/messages/{message_id}/reactions    ?emoji=<encoded>                       → remove (idempotent)

A reaction carries the reactor's sovereign-signing ``origin`` envelope (shape-validated
at the trust boundary by ``signing.validate_origin``, then CARRIED verbatim — the
gateway never verifies the signature). ``origin`` is optional: an unsigned reaction is
carried as "unverified", never rejected. DELETE takes the emoji as a QUERY PARAM (an
opaque token in the path is a URL-grammar hazard — ``/`` ``#`` ``?`` ``%`` truncate or
fork the request, and keycap emoji like ``#️⃣`` legitimately contain ``#``).

Both go through the ONE mutator door (``reactions_service``, which validates the emoji
itself) and, on a REAL change, fan a discrete ``reaction`` frame to the channel's live
subscribers — the same mutation-over-REST + fanout-over-WS split takedown retractions
use. Reactions are STATE, not events: the frame is a best-effort live delta over the
aggregate a history read recomputes, so a missed frame self-heals on re-fetch.

VISIBILITY GATE (add) — you may only react to a message you can SEE. ``_visible_message``
applies the SAME predicate the history read does: exists, not soft-deleted (taken-down),
channel readable (ACL), author not in a block relationship with the viewer. Any miss
collapses to the SAME 404 as a non-existent message (existence-hiding). The emoji +
origin are validated BEFORE the message is resolved, so a malformed input is always a
422 regardless of message visibility — the 422/404 split can't probe existence.

BLOCK CONSISTENCY. Identity is exposed, so the block predicate governs the WHOLE
projection. The history aggregate drops a blocked reactor from ``reactors`` AND
``count`` (viewer-dependent, block-filtered); the mutate-response ``count`` uses the
SAME filtered tally (``reactions_service.emoji_count``); the live ``reaction`` frame is
excluded from any subscriber in a block relationship with the reactor OR the message
author, and carries NO count (an identity delta the client applies as a set change). One
predicate, all paths — no count oracle.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..domain import (
    acl, messages_service, moderation_service, reactions_service, signing,
)
from ..domain.models import Message
from ..domain.rate_limit import rate_limit
from ..realtime import envelopes
from ..realtime.envelopes import ReactionAction
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["reactions"])


class ReactionBody(BaseModel):
    emoji: str
    # The reaction's OWN id (signing-bytes field #4), echoed so the gateway binds it to
    # origin.client_msg_id. Required when `origin` is present; ignored when absent.
    client_msg_id: str | None = None
    # The sovereign-signing envelope (#1816 shape). Absent = unsigned reaction (carried
    # as "unverified"). Shape-validated at the boundary, never verified.
    origin: dict | None = None


async def _resolve_visible_message(
    session, viewer_id: str, message_id: str,
) -> Message | None:
    """Return the message IFF the viewer may currently see it (exists, not
    soft-deleted, channel readable, author not blocked) — else None. The single
    visibility predicate, mirroring the history read; callers choose whether a miss
    is a 404 or a silent count/fanout suppression."""
    msg = await messages_service.get_message(session, message_id)
    if msg is None or msg.deleted_at is not None:
        return None
    if await acl.readable_channel(session, viewer_id, msg.channel_id) is None:
        return None
    if msg.sender_user_id is not None:
        blocked = await moderation_service.blocked_pair_user_ids(session, viewer_id)
        if msg.sender_user_id in blocked:
            return None
    return msg


async def _visible_message(session, viewer_id: str, message_id: str) -> Message:
    """Resolve a message the viewer may see, or raise the existence-hiding 404 (a
    missing / soft-deleted / unreadable / blocked-author message all 404 identically,
    so the boundary never confirms a message the viewer isn't allowed to see)."""
    msg = await _resolve_visible_message(session, viewer_id, message_id)
    if msg is None:
        raise HTTPException(404, "message not found")
    return msg


async def _fanout(
    request: Request, session, *, channel_id: str, msg_id: str, emoji: str,
    action: ReactionAction, actor_id: str, author_id: str | None,
    origin: dict | None,
) -> None:
    """Best-effort live ``reaction`` frame to the channel's subscribers, excluding any
    subscriber in a block relationship with the reactor (the frame names the reactor +
    carries their ``origin``) OR with the message author (they can't see the message).
    The frame carries NO count — it is an identity delta the client applies as a set
    change, so block-excluding the recipient (not filtering a count) is what keeps a
    hidden reactor hidden. No-op if the hub isn't wired (a minimal app / worker without
    realtime); the durable state is the row, the frame is the optimisation."""
    gw = getattr(request.app.state, "gw", None)
    if gw is None or getattr(gw, "hub", None) is None:
        return
    exclude = await moderation_service.blocked_pair_user_ids(session, actor_id)
    if author_id is not None and author_id != actor_id:
        exclude = exclude | await moderation_service.blocked_pair_user_ids(
            session, author_id)
    await gw.hub.fanout(
        channel_id,
        envelopes.reaction_frame(channel_id, msg_id, emoji, action, actor_id, origin),
        exclude_user_ids=exclude)


@router.post("/messages/{message_id}/reactions",
             dependencies=[rate_limit("reactions")])
async def add_reaction(
    message_id: str, body: ReactionBody, user: CurrentUser, session: DbSession,
    request: Request,
) -> dict:
    """Add the caller's ``emoji`` reaction (idempotent — re-adding the same emoji is a
    no-op returning the unchanged count and firing no frame; the first signature wins).
    422 if the emoji or ``origin`` envelope is malformed (checked BEFORE resolution, so
    it can't probe existence); 404 if the message isn't visible; 429 at the per-message
    reaction cap. ``count`` in the response is the caller's block-filtered tally, the
    same one a subsequent history read shows."""
    # Validate emoji + origin BEFORE resolving the message, so bad input is 422 whether
    # or not the message is visible — the 422/404 split can't leak message existence.
    try:
        emoji = reactions_service.validate_emoji(body.emoji)
    except reactions_service.InvalidEmoji as exc:
        raise HTTPException(422, str(exc))
    # A signed reaction MUST carry a non-empty outer client_msg_id to bind against
    # (cage-match Carnot r2): otherwise `frame_client_msg_id=""` would let an origin
    # with an embedded EMPTY client_msg_id satisfy the binding — a degenerate id must
    # never authenticate. Reject before validate_origin so the 422 is unambiguous.
    if body.origin is not None and not body.client_msg_id:
        raise HTTPException(
            422, "origin: a signed reaction requires a non-empty client_msg_id")
    # Shape-validate the sovereign-signing envelope at the trust boundary (the gateway
    # carries, does not verify). Binds origin.client_msg_id to the request's
    # client_msg_id — the reaction's own id. None origin → returns None (unsigned).
    try:
        origin = signing.validate_origin(
            body.origin, frame_client_msg_id=body.client_msg_id or "")
    except signing.OriginError as exc:
        raise HTTPException(422, f"origin: {exc}")
    msg = await _visible_message(session, user.id, message_id)
    try:
        changed = await reactions_service.add_reaction(
            session, user_id=user.id, message_id=message_id, emoji=emoji,
            origin=origin)
    except reactions_service.ReactionLimitExceeded:
        raise HTTPException(
            429, f"at most {reactions_service.MAX_REACTIONS_PER_USER_PER_MESSAGE} "
                 "reactions per message")
    blocked = await moderation_service.blocked_pair_user_ids(session, user.id)
    if changed:
        await _fanout(
            request, session, channel_id=msg.channel_id, msg_id=message_id,
            emoji=emoji, action=ReactionAction.ADD, actor_id=user.id,
            author_id=msg.sender_user_id, origin=origin)
    count = await reactions_service.emoji_count(
        session, message_id, emoji, blocked_user_ids=blocked)
    return {"msg_id": message_id, "emoji": emoji, "count": count,
            "reacted_by_me": True}


@router.delete("/messages/{message_id}/reactions",
               dependencies=[rate_limit("reactions")])
async def remove_reaction(
    message_id: str, user: CurrentUser, session: DbSession, request: Request,
    emoji: str = Query(..., description="the reaction emoji to remove"),
) -> dict:
    """Remove the caller's ``emoji`` reaction (idempotent). 422 if the emoji is
    malformed.

    THREE audiences, THREE gates:
    * MUTATION → OWNERSHIP. Un-reacting your OWN row is a strictly-reducing self-owned
      action, allowed even after you lose sight of the message (takedown / kick / a new
      block) — otherwise your reaction is an un-deletable scar.
    * FANOUT → the MESSAGE being a live target (exists + not soft-deleted), NOT the
      caller's visibility. The frame serves OTHER subscribers, who can still see the
      message; their view must move on your removal even if YOU no longer see it.
    * RESPONSE count → the CALLER's visibility. A caller who removed a row while no
      longer able to see the message gets ``count: null`` — no post-revocation count
      oracle for a channel they've lost.
    If NO row was removed, fall back to the existence-hiding 404 ``add`` uses, so a
    caller with no row can't probe a message they can't see."""
    try:
        emoji = reactions_service.validate_emoji(emoji)
    except reactions_service.InvalidEmoji as exc:
        raise HTTPException(422, str(exc))
    changed = await reactions_service.remove_reaction(
        session, user_id=user.id, message_id=message_id, emoji=emoji)
    blocked = await moderation_service.blocked_pair_user_ids(session, user.id)
    if changed:
        # FANOUT: gate on the MESSAGE being a live target (unfiltered fetch), so peers'
        # views move even when the caller has lost visibility. A remove carries no
        # origin (un-reaction is ownership-authorised, not a persisted signed event).
        live = await messages_service.get_message(session, message_id)
        if live is not None and live.deleted_at is None:
            await _fanout(
                request, session, channel_id=live.channel_id, msg_id=message_id,
                emoji=emoji, action=ReactionAction.REMOVE, actor_id=user.id,
                author_id=live.sender_user_id, origin=None)
        # RESPONSE: gate the count on the CALLER's own visibility — null if they've lost
        # it (no oracle), the block-filtered count if they can still see the message.
        if await _resolve_visible_message(session, user.id, message_id) is not None:
            count = await reactions_service.emoji_count(
                session, message_id, emoji, blocked_user_ids=blocked)
            return {"msg_id": message_id, "emoji": emoji, "count": count,
                    "reacted_by_me": False}
        return {"msg_id": message_id, "emoji": emoji, "count": None,
                "reacted_by_me": False}
    # No row removed → enforce the visibility gate so a prober can't tell "no reaction
    # here" from "message I can't see" (both 404 when not visible).
    await _visible_message(session, user.id, message_id)
    count = await reactions_service.emoji_count(
        session, message_id, emoji, blocked_user_ids=blocked)
    return {"msg_id": message_id, "emoji": emoji, "count": count,
            "reacted_by_me": False}
