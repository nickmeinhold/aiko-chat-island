"""Emoji-reaction endpoints (#2634, v2 social layer).

POST   /v1/messages/{message_id}/reactions        body {emoji}      → add (idempotent)
DELETE /v1/messages/{message_id}/reactions        ?emoji=<encoded>  → remove (idempotent)

(DELETE takes the emoji as a QUERY PARAM, not a path segment — an opaque token in the
path is a URL-grammar hazard; see the remove handler's docstring for the full reasoning.)

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

FANOUT respects the block on the IDENTITY the frame carries, not on the count. The
``reaction`` frame carries the reactor's ``user_id``, so it is excluded for any
subscriber in a block relationship with the reactor (they can't see the reactor) OR
with the message author (they can't see the message at all). The ``count`` itself is
a global anonymous tally (no reactor list in v2), so it is NOT block-filtered — same
number on the frame, the mutate response, and the history aggregate; block-filtering
it would only leak, via a count mismatch, that a hidden user reacted (the count
oracle, cage-match round 2). See ``reactions_service`` for the full reasoning.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..domain import acl, messages_service, moderation_service, reactions_service
from ..domain.models import Message
from ..domain.rate_limit import rate_limit
from ..realtime import envelopes
from ..realtime.envelopes import ReactionAction
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["reactions"])


class ReactionBody(BaseModel):
    emoji: str


async def _resolve_visible_message(
    session, viewer_id: str, message_id: str,
) -> Message | None:
    """Return the message IFF the viewer may currently see it (exists, not
    soft-deleted, channel readable, author not blocked) — else None. The single
    visibility predicate, mirroring the history read; callers choose whether a miss
    is a 404 (``_visible_message``) or a silent count/fanout suppression (remove)."""
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
    action: ReactionAction, actor_id: str, author_id: str | None, count: int,
) -> None:
    """Best-effort live ``reaction`` frame to the channel's subscribers, excluding any
    subscriber in a block relationship with the reactor (the frame names the reactor)
    OR with the message author (they can't see the message). The ``count`` on the
    frame is the global anonymous tally, matching history — only the identity-bearing
    delivery is block-filtered. No-op if the hub isn't wired (a minimal app / worker
    without realtime); the durable state is the row, the frame is the optimisation."""
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


@router.post("/messages/{message_id}/reactions",
             dependencies=[rate_limit("reactions")])
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


@router.delete("/messages/{message_id}/reactions",
               dependencies=[rate_limit("reactions")])
async def remove_reaction(
    message_id: str, user: CurrentUser, session: DbSession, request: Request,
    emoji: str = Query(..., description="the reaction emoji to remove"),
) -> dict:
    """Remove the caller's ``emoji`` reaction from a message (idempotent). 422 if the
    emoji is malformed.

    The emoji rides a QUERY PARAM, not a path segment (cage-match round 5, Tesla): an
    opaque token in the path is a URL-grammar hazard (``/`` ``#`` ``?`` ``%`` truncate
    or fork the request, and keycap emoji like ``#️⃣`` legitimately CONTAIN ``#``), so
    a path-stored reaction could be un-removable. A query value is percent-encoded
    end-to-end and dissolves the whole class.

    THREE audiences, THREE gates (cage-match rounds 3-5, Tesla + Carnot):
    * MUTATION → OWNERSHIP. Un-reacting your OWN row is a strictly-reducing self-owned
      action, allowed even after you lose sight of the message (takedown / kick / a new
      block) — otherwise your reaction is an un-deletable scar.
    * FANOUT → the MESSAGE being a live target (exists + not soft-deleted), NOT the
      caller's visibility. The frame serves OTHER subscribers, who can still see the
      message; their anonymous count must move on your removal even if YOU no longer
      see it. (Gating fanout on the caller's visibility froze peers' counts — the r4
      regression this fixes.)
    * RESPONSE count → the CALLER's visibility. A caller who removed a row while no
      longer able to see the message gets ``count: null`` — no post-revocation count
      oracle for a channel they've lost.
    If NO row was removed, fall back to the existence-hiding 404 ``add`` uses, so a
    caller with no row can't probe a message they can't see."""
    try:
        emoji = reactions_service.validate_emoji(emoji)
    except reactions_service.InvalidEmoji as exc:
        raise HTTPException(422, str(exc))
    count, changed = await reactions_service.remove_reaction(
        session, user_id=user.id, message_id=message_id, emoji=emoji)
    if changed:
        # FANOUT: gate on the MESSAGE being a live target (unfiltered fetch), so peers'
        # counts move even when the caller has lost visibility.
        live = await messages_service.get_message(session, message_id)
        if live is not None and live.deleted_at is None:
            await _fanout(
                request, session, channel_id=live.channel_id, msg_id=message_id,
                emoji=emoji, action=ReactionAction.REMOVE, actor_id=user.id,
                author_id=live.sender_user_id, count=count)
        # RESPONSE: gate the count on the CALLER's own visibility — null if they've lost
        # it (no oracle), the real count if they can still see the message.
        if await _resolve_visible_message(session, user.id, message_id) is not None:
            return {"msg_id": message_id, "emoji": emoji, "count": count,
                    "reacted_by_me": False}
        return {"msg_id": message_id, "emoji": emoji, "count": None,
                "reacted_by_me": False}
    # No row removed → enforce the visibility gate so a prober can't tell "no reaction
    # here" from "message I can't see" (both 404 when not visible).
    await _visible_message(session, user.id, message_id)
    return {"msg_id": message_id, "emoji": emoji, "count": count,
            "reacted_by_me": False}
