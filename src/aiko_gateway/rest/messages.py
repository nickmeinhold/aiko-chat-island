"""Channel-history endpoint.

I1 (read requires auth): the `CurrentUser` dependency rejects unauthenticated
callers before any history is read. I2 (membership, #36): a user may only read
channels they may see — public channels, or private channels they belong to. A
private channel the user is not a member of is collapsed into the SAME 404 as a
non-existent channel (`acl.can_read`), so the boundary never confirms it exists.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..domain import acl, messages_service, moderation_service, reactions_service
from ..domain.models import Message
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["messages"])


@router.get("/messages/{message_id}")
async def get_message(
    message_id: str, user: CurrentUser, session: DbSession
) -> dict:
    """One message by id — reply-parent resolution, deep-links, jump-to-message
    (#2633). Applies the SAME visibility predicate as history
    (``messages_service.visible_message``): a missing / soft-deleted / unreadable /
    blocked-author message all 404 IDENTICALLY (existence-hiding), and a taken-down
    parent is a 404 (its body is never resurrected — no retraction leak). Returns the
    SAME enriched ``MessageView`` as the history read (the reaction aggregate injected
    with the SAME block-filter), so there is no new wire shape."""
    msg = await messages_service.visible_message(session, user.id, message_id)
    if msg is None:
        raise HTTPException(404, "message not found")
    blocked = await moderation_service.blocked_pair_user_ids(session, user.id)
    aggregate = await reactions_service.aggregate_for_messages(
        session, [msg.id], user.id, blocked_user_ids=blocked)
    return messages_service.message_view(msg, reactions=aggregate.get(msg.id))


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
    # Reaction aggregate (#2634), block-filtered for THIS viewer. One block-pair probe
    # + one grouped reactions query for the whole page (never N+1), injected into each
    # message_view. The SAME block predicate that gates the live `reaction` frame's
    # fanout, so history and live agree (no count oracle). Retraction items are
    # content-less events with no reactions.
    msg_ids = [r.id for r in rows if isinstance(r, Message)]
    blocked = await moderation_service.blocked_pair_user_ids(session, user.id)
    reactions = await reactions_service.aggregate_for_messages(
        session, msg_ids, user.id, blocked_user_ids=blocked)
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
