"""Channel-history endpoint.

I1 (read requires auth): the `CurrentUser` dependency rejects unauthenticated
callers before any history is read. I2 (membership, #36): a user may only read
channels they may see — public channels, or private channels they belong to. A
private channel the user is not a member of is collapsed into the SAME 404 as a
non-existent channel (`acl.can_read`), so the boundary never confirms it exists.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..domain import acl, messages_service, reactions_service
from ..domain.models import Message
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["messages"])


@router.get("/channels/{channel_id}/messages")
async def history(
    channel_id: str,
    user: CurrentUser,
    session: DbSession,
    before: str | None = Query(default=None),
    after: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Channel history, ascending. `before` = scroll-up (older); `after` =
    forward catch-up (B4 reconnect). Both cursors returned so either direction
    can page: `next_before` = oldest in batch, `next_after` = newest in batch."""
    # One query resolves existence AND access: a missing channel and a private
    # channel the user is not in both return None with identical DB work, so the
    # 404 leaks neither existence nor timing (existence-hiding, #36).
    channel = await acl.readable_channel(session, user.id, channel_id)
    if channel is None:
        raise HTTPException(404, "channel not found")
    rows = await messages_service.get_history(
        session, channel_id, user.id, before=before, after=after, limit=limit
    )
    # Reactions (#2634): ONE viewer-dependent aggregate for every message row in the
    # page (never N+1), injected into message_view. Only Message rows carry
    # reactions — retraction items are content-less events. Empty for a page with no
    # reacted messages, so message_view falls back to `[]`.
    reactions = await reactions_service.aggregate_for_messages(
        session, [r.id for r in rows if isinstance(r, Message)], user.id)
    # Heterogeneous stream (#7): message items AND takedown `retraction` items,
    # interleaved in ULID order. Both advance next_before/next_after (each row has an
    # `id` on the shared axis), so a client paging forward from its watermark receives
    # a retraction inline and reconciles. Messages carry an explicit "type":"message"
    # (wire contract option A) so the app disambiguates by a single `type` field;
    # message_view stays untyped, keeping the WS/bus fanout shape unchanged.
    items = [
        {"type": "message",
         **messages_service.message_view(r, reactions=reactions.get(r.id))}
        if isinstance(r, Message)
        else messages_service.retraction_view(r)
        for r in rows
    ]
    return {
        "channel_id": channel_id,
        "messages": items,
        "next_before": rows[0].id if rows else None,
        "next_after": rows[-1].id if rows else None,
    }
