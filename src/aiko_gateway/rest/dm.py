"""Direct-message endpoints (#2633) — DMs as 1:1 (member-set) channels.

    POST /v1/dm    { "target_user_id": "<key>" }   → find-or-create the 1:1 channel
    GET  /v1/dm                                     → my DM channels (+ last_message)

I1 (auth): every route takes ``CurrentUser`` so an unauthenticated caller is rejected
before any row is touched. The find-or-create + list rules live in the single mutator
door ``dm_service`` (mirroring how ``memberships_service`` owns membership mutations);
this layer only translates typed outcomes into HTTP.

A DM is an ordinary private channel, so it inherits the whole enforcement stack — auth,
membership visibility, existence-hiding, block content-filter — through the SAME doors
the rest of the API uses. Design of record: ``docs/design/11-direct-messages.md``.

(``GET /v1/messages/{id}`` — reply-parent resolution — lives in the MESSAGES router,
not here: it is a general messages surface, not DM-specific (cage-match PR#124 Tesla).)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..domain import dm_service, messages_service
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
        # Same 404 the contract promises for a bad target. Block is NOT gated at
        # CREATION (a creation-time refusal would leak the block direction, and the
        # channel shell is harmless) — the block gate is on the SEND, in the ws path
        # (design 11 §Decision 5, Nick 2026-08-10: a DM send under a block is refused).
        raise HTTPException(404, "user not found")
    except dm_service.DmKeyCollision:
        # The deterministic dm:<lo>:<hi> key is held by a non-DM channel — fail closed
        # (never adopt/federate a foreign channel as a DM). 409: the pair cannot get a
        # DM while the key is squatted (near-impossible; the key embeds two unguessable
        # ULIDs). See dm_service.DmKeyCollision.
        raise HTTPException(409, "cannot open a direct message with this user right now")
    return dm_service.dm_channel_view(channel, member_ids)


@router.get("/dm")
async def list_dms(user: CurrentUser, session: DbSession) -> dict:
    """My DM channels for the switcher. Each carries ``last_message`` — the newest
    message VISIBLE to me (not soft-deleted, author not blocked), or ``null`` if none
    yet. NO ``unread`` (client-side per the contract — the island has no read-position
    store). ``last_message`` is a ``MessageView`` (block/soft-delete-filtered, no
    reaction aggregate — a switcher preview needs the line, not its reactions) so the
    client renders it like a history row.

    BATCHED (cage-match PR#124): members + last-message are resolved for ALL my DM
    channels in a constant number of queries (``members_of_many`` + the two-query
    ``last_visible_messages``), not per-channel — no N+1 on the switcher endpoint. Both
    batch reads are scoped to my OWN DM channels, which satisfies the ACL precondition
    the ``last_visible_messages`` content filter carries."""
    channels = await dm_service.list_dms(session, user.id)
    channel_ids = [ch.id for ch in channels]
    members_by_channel = await dm_service.members_of_many(session, channel_ids)
    last_by_channel = await messages_service.last_visible_messages(
        session, channel_ids, user.id)
    items = []
    for ch in channels:
        # members_of_many initializes an entry for every requested id, so [] is a keyed
        # lookup (a DM always has ≥1 member anyway).
        view = dm_service.dm_channel_view(ch, members_by_channel[ch.id])
        last = last_by_channel.get(ch.id)
        view["last_message"] = (
            messages_service.message_view(last) if last is not None else None)
        items.append(view)
    return {"channels": items}
