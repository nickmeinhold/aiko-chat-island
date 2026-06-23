"""Channel-history endpoint.

I1 (read requires auth): the `CurrentUser` dependency rejects unauthenticated
callers before any history is read. I2 (membership, #36): a user may only read
channels they may see — public channels, or private channels they belong to. A
private channel the user is not a member of is collapsed into the SAME 404 as a
non-existent channel (`acl.can_read`), so the boundary never confirms it exists.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..domain import acl, messages_service
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
        session, channel_id, before=before, after=after, limit=limit
    )
    return {
        "channel_id": channel_id,
        # Single source for the MessageView wire shape — shared with the WS
        # fanout path so REST and live frames can never drift (plan §A1).
        "messages": [messages_service.message_view(m) for m in rows],
        "next_before": rows[0].id if rows else None,
        "next_after": rows[-1].id if rows else None,
    }
