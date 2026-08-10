"""Direct-message endpoints (#2633) — DMs as 1:1 (member-set) channels.

    POST /v1/dm    { "target_user_id": "<key>" }   → find-or-create the 1:1 channel
    GET  /v1/dm                                     → my DM channels (+ last_message)
    GET  /v1/messages/{msg_id}                      → one message by id (reply-parent)

I1 (auth): every route takes ``CurrentUser`` so an unauthenticated caller is rejected
before any row is touched. The find-or-create + list rules live in the single mutator
door ``dm_service`` (mirroring how ``memberships_service`` owns membership mutations);
this layer only translates typed outcomes into HTTP.

A DM is an ordinary private channel, so it inherits the whole enforcement stack — auth,
membership visibility, existence-hiding, block content-filter — through the SAME doors
the rest of the API uses. Design of record: ``docs/design/11-direct-messages.md``.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..domain import (
    dm_service, messages_service, moderation_service, reactions_service,
)
from ..domain.rate_limit import rate_limit
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["dm"])


class OpenDmReq(BaseModel):
    target_user_id: str


@router.post("/dm", dependencies=[rate_limit("dm")])
async def open_dm(body: OpenDmReq, user: CurrentUser, session: DbSession) -> dict:
    """Find-or-create the 1:1 DM channel for ``{me, target}`` (idempotent — the same
    pair always resolves to the same channel). 404 if ``target_user_id`` is not a real
    user. Self-DM (target == me) is ALLOWED (notes-to-self). Rate-limited (mild
    channel-creation abuse vector). Returns the channel view; ``members`` is an ARRAY."""
    try:
        channel, member_ids = await dm_service.get_or_create_dm(
            session, me=user, target_user_id=body.target_user_id)
    except dm_service.TargetNotFound:
        # Same 404 the contract promises for a bad target. (Block is NOT gated here —
        # design 11 §Decision 5: block is a content filter on the shared read/fanout
        # path, which makes a DM under a block inert without a creation gate, and a
        # creation-time refusal would leak the block direction.)
        raise HTTPException(404, "user not found")
    return dm_service.dm_channel_view(channel, member_ids)


@router.get("/dm")
async def list_dms(user: CurrentUser, session: DbSession) -> dict:
    """My DM channels for the switcher. Each carries ``last_message`` — the newest
    message VISIBLE to me (not soft-deleted, author not blocked), or ``null`` if none
    yet. NO ``unread`` (client-side per the contract — the island has no read-position
    store). ``last_message`` is the full ``MessageView`` (a superset of the contract's
    example, additive-safe) so the client renders it exactly like a history row."""
    channels = await dm_service.list_dms(session, user.id)
    items = []
    for ch in channels:
        last = await messages_service.last_visible_message(session, ch.id, user.id)
        member_ids = await dm_service.members_of(session, ch.id)
        view = dm_service.dm_channel_view(ch, member_ids)
        view["last_message"] = (
            messages_service.message_view(last) if last is not None else None)
        items.append(view)
    return {"channels": items}


@router.get("/messages/{message_id}")
async def get_message(message_id: str, user: CurrentUser, session: DbSession) -> dict:
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
