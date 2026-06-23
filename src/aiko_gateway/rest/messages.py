"""Channel-history endpoint.

I1 (read requires auth): the `CurrentUser` dependency rejects unauthenticated
callers before any history is read. I2 (membership: a user may only read
channels they belong to) is deferred to Phase 2's ACL suite (plan §A3) — until
then any authenticated user can read any channel's history. See the I2 follow-up.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..domain import messages_service
from ..domain.models import Channel
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["messages"])


def _msg_view(m) -> dict:
    return {
        "msg_id": m.id, "channel_id": m.channel_id,
        "sender": {"user_id": m.sender_user_id, "kind": m.sender_kind, "label": m.sender_label},
        "body": m.body, "created_at": m.created_at.isoformat(),
        "reply_to": m.reply_to,
    }


@router.get("/channels/{channel_id}/messages")
async def history(
    channel_id: str,
    user: CurrentUser,
    session: DbSession,
    before: str | None = Query(default=None),
    after: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
) -> dict:
    """Channel history, ascending. `before` = scroll-up (older); `after` =
    forward catch-up (B4 reconnect). Both cursors returned so either direction
    can page: `next_before` = oldest in batch, `next_after` = newest in batch."""
    channel = await session.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(404, "channel not found")
    rows = await messages_service.get_history(
        session, channel_id, before=before, after=after, limit=limit
    )
    return {
        "channel_id": channel_id,
        "messages": [_msg_view(m) for m in rows],
        "next_before": rows[0].id if rows else None,
        "next_after": rows[-1].id if rows else None,
    }
