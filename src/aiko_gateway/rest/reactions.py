"""Emoji-reaction endpoints (#2634, v2 social layer).

POST   /v1/messages/{message_id}/reactions        body {emoji}   → add (idempotent)
DELETE /v1/messages/{message_id}/reactions/{emoji}                → remove (idempotent)

Both go through the ONE mutator door (``reactions_service``, which validates the
emoji itself) and, on a REAL change, fan a discrete ``reaction`` frame to the
channel's live subscribers — the same mutation-over-REST + fanout-over-WS split
takedown retractions use (rest/moderation). Reactions are STATE, not events (see the
MessageReaction model): the frame is a best-effort live delta over the aggregate a
history read recomputes, so a missed frame self-heals when the message row is
re-fetched.

VISIBILITY GATE — you may only react to a message you can SEE. ``_visible_message``
applies the SAME predicate the history read does: the message must exist, not be
soft-deleted (taken-down), live in a channel the viewer may read (ACL), and not be
authored by someone in a block relationship with the viewer. Any miss collapses to
the SAME 404 as a non-existent message (existence-hiding). The emoji is validated
BEFORE the message is resolved, so a malformed emoji is always a 422 regardless of
whether the message is visible — the 422/404 split can't be used to probe existence.

FANOUT VISIBILITY = the read predicate's live twin. The frame is excluded for any
subscriber who could not see the target message: the reactor's block pairs (they
can't see the reactor) UNION the message author's block pairs (they can't see the
message). This matches what ``reactions_service.aggregate_for_messages`` would show
that subscriber on a subsequent history read — one predicate, both paths.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..domain import acl, messages_service, moderation_service, reactions_service
from ..domain.models import Message
from ..realtime import envelopes
from ..realtime.envelopes import ReactionAction
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
    action: ReactionAction, actor_id: str, author_id: str | None, count: int,
) -> None:
    """Best-effort live ``reaction`` frame to the channel's subscribers, excluding any
    subscriber who could not see the target message: the reactor's block pairs UNION
    the message author's block pairs. This is the live twin of the read predicate
    (``aggregate_for_messages`` hides blocked reactors; history hides a blocked
    author's message entirely), so the two paths never disagree. No-op if the hub
    isn't wired (a minimal app / worker without realtime); the durable state is the
    row, the frame is the optimisation."""
    gw = getattr(request.app.state, "gw", None)
    if gw is None or getattr(gw, "hub", None) is None:
        return
    exclude = await moderation_service.blocked_pair_user_ids(session, actor_id)
    if author_id is not None and author_id != actor_id:
        exclude = exclude | await moderation_service.blocked_pair_user_ids(
            session, author_id)
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
    same emoji is a no-op returning the unchanged count and firing no frame). 422 if
    the emoji is malformed (checked BEFORE resolution, so it can't probe existence);
    404 if the message isn't visible; 429 if the caller is at the per-message reaction
    cap."""
    # Validate the emoji BEFORE resolving the message so a bad emoji is 422 whether or
    # not the message is visible — the 422/404 split can't leak message existence.
    try:
        emoji = reactions_service.validate_emoji(body.emoji)
    except reactions_service.InvalidEmoji as exc:
        raise HTTPException(422, str(exc))
    msg = await _visible_message(session, user.id, message_id)
    try:
        count, changed = await reactions_service.add_reaction(
            session, user_id=user.id, message_id=message_id, emoji=emoji)
    except reactions_service.ReactionLimitExceeded:
        raise HTTPException(
            429, f"at most {reactions_service.MAX_REACTIONS_PER_USER_PER_MESSAGE} "
                 "reactions per message")
    if changed:
        await _fanout(
            request, session, channel_id=msg.channel_id, msg_id=message_id,
            emoji=emoji, action=ReactionAction.ADD, actor_id=user.id,
            author_id=msg.sender_user_id, count=count)
    return {"msg_id": message_id, "emoji": emoji, "count": count,
            "reacted_by_me": True}


@router.delete("/messages/{message_id}/reactions/{emoji}")
async def remove_reaction(
    message_id: str, emoji: str, user: CurrentUser, session: DbSession,
    request: Request,
) -> dict:
    """Remove the caller's ``emoji`` reaction from a message (idempotent — removing an
    absent reaction returns the current count, fires no frame, and is not an error).
    ``emoji`` is the percent-decoded path segment. 422 if the emoji is malformed
    (checked before resolution); 404 if the message isn't visible."""
    try:
        emoji = reactions_service.validate_emoji(emoji)
    except reactions_service.InvalidEmoji as exc:
        raise HTTPException(422, str(exc))
    msg = await _visible_message(session, user.id, message_id)
    count, changed = await reactions_service.remove_reaction(
        session, user_id=user.id, message_id=message_id, emoji=emoji)
    if changed:
        await _fanout(
            request, session, channel_id=msg.channel_id, msg_id=message_id,
            emoji=emoji, action=ReactionAction.REMOVE, actor_id=user.id,
            author_id=msg.sender_user_id, count=count)
    return {"msg_id": message_id, "emoji": emoji, "count": count,
            "reacted_by_me": False}
