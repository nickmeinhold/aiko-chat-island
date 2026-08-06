"""Emoji-reaction endpoints (#2634, v2 social layer).

POST   /v1/messages/{message_id}/reactions        body {emoji}   → add (idempotent)
DELETE /v1/messages/{message_id}/reactions/{emoji}                → remove (idempotent)

Both go through the ONE mutator door (``reactions_service``) and, on success, fan a
discrete ``reaction`` frame to the channel's live subscribers — the same
mutation-over-REST + fanout-over-WS split takedown retractions use (rest/moderation).
Reactions are STATE, not events (see the MessageReaction model): the frame is a
best-effort live delta over the aggregate a history read recomputes, so a missed
frame self-heals on re-page.

VISIBILITY GATE — you may only react to a message you can SEE. ``_visible_message``
applies the SAME predicate the history read does: the message must exist, not be
soft-deleted (taken-down), live in a channel the viewer may read (ACL), and not be
authored by someone in a block relationship with the viewer. Any miss collapses to
the SAME 404 as a non-existent message, so the boundary never confirms a message the
viewer isn't allowed to see (existence-hiding, mirrors rest/messages + rest/moderation).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..domain import acl, messages_service, moderation_service, reactions_service
from ..domain.models import Message
from ..realtime import envelopes
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["reactions"])


class ReactionBody(BaseModel):
    emoji: str


async def _visible_message(session, viewer_id: str, message_id: str) -> Message:
    """Resolve a message the viewer is allowed to react to, or raise 404. Same
    visibility predicate as the history read (soft-delete + channel ACL + block),
    collapsed into one existence-hiding 404."""
    msg = await messages_service.get_message(session, message_id)
    if msg is None or msg.deleted_at is not None:
        raise HTTPException(404, "message not found")
    if await acl.readable_channel(session, viewer_id, msg.channel_id) is None:
        raise HTTPException(404, "message not found")
    # A blocked author's message is hidden from the viewer in the block relationship
    # (same content filter as get_history) — so it is un-reactable, same 404.
    if msg.sender_user_id is not None:
        blocked = await moderation_service.blocked_pair_user_ids(session, viewer_id)
        if msg.sender_user_id in blocked:
            raise HTTPException(404, "message not found")
    return msg


async def _fanout(
    request: Request, session, *, channel_id: str, msg_id: str, emoji: str,
    action: str, actor_id: str, count: int,
) -> None:
    """Best-effort live ``reaction`` frame to the channel's subscribers. Excludes the
    actor's block pairs — the live twin of the visibility filter (mirrors the send
    path, ws._handle_send). No-op if the hub isn't wired (a minimal app / worker
    without realtime); the durable state is the row, the frame is the optimisation."""
    gw = getattr(request.app.state, "gw", None)
    if gw is None or getattr(gw, "hub", None) is None:
        return
    exclude = await moderation_service.blocked_pair_user_ids(session, actor_id)
    await gw.hub.fanout(
        channel_id,
        envelopes.reaction_frame(channel_id, msg_id, emoji, action, actor_id, count),
        exclude_user_ids=exclude)


@router.post("/messages/{message_id}/reactions")
async def add_reaction(
    message_id: str, body: ReactionBody, user: CurrentUser, session: DbSession,
    request: Request,
) -> dict:
    """Add the caller's ``emoji`` reaction to a message (idempotent — re-adding the
    same emoji is a no-op returning the unchanged count). 404 if the message isn't
    visible; 422 if the emoji is blank/over-long."""
    try:
        emoji = reactions_service.validate_emoji(body.emoji)
    except reactions_service.InvalidEmoji as exc:
        raise HTTPException(422, str(exc))
    msg = await _visible_message(session, user.id, message_id)
    count = await reactions_service.add_reaction(
        session, user_id=user.id, message_id=message_id, emoji=emoji)
    await _fanout(
        request, session, channel_id=msg.channel_id, msg_id=message_id,
        emoji=emoji, action="add", actor_id=user.id, count=count)
    return {"msg_id": message_id, "emoji": emoji, "count": count,
            "reacted_by_me": True}


@router.delete("/messages/{message_id}/reactions/{emoji}")
async def remove_reaction(
    message_id: str, emoji: str, user: CurrentUser, session: DbSession,
    request: Request,
) -> dict:
    """Remove the caller's ``emoji`` reaction from a message (idempotent — removing an
    absent reaction returns the current count, not an error). ``emoji`` is the
    percent-decoded path segment. 404 if the message isn't visible; 422 if the emoji
    is malformed."""
    try:
        emoji = reactions_service.validate_emoji(emoji)
    except reactions_service.InvalidEmoji as exc:
        raise HTTPException(422, str(exc))
    msg = await _visible_message(session, user.id, message_id)
    count = await reactions_service.remove_reaction(
        session, user_id=user.id, message_id=message_id, emoji=emoji)
    await _fanout(
        request, session, channel_id=msg.channel_id, msg_id=message_id,
        emoji=emoji, action="remove", actor_id=user.id, count=count)
    return {"msg_id": message_id, "emoji": emoji, "count": count,
            "reacted_by_me": False}
