"""The /v1/ws endpoint — authenticated realtime send/receive.

Handshake: ?token=<access jwt>. Invalid token -> close 1008 before any frame
(invariant I1 for the WS path). The send path is the one transaction boundary
for an outgoing message (plan): validate -> persist (server ULID, server-derived
sender) -> mark echo -> publish to bus -> ack sender -> fanout to subscribers.
"""
from __future__ import annotations

import logging

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from ..db import SessionLocal
from ..domain import (
    acl, echo, messages_service, moderation_service, security, users_service,
)
from . import envelopes
from .hub import Connection

log = logging.getLogger("aiko_gateway.ws")
router = APIRouter()


@router.websocket("/v1/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token", "")
    try:
        user_id = security.decode_token(token, expected_type="access")
    except jwt.InvalidTokenError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    async with SessionLocal() as session:
        user = await users_service.get_by_id(session, user_id)
    if user is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    gw = websocket.app.state.gw
    conn = Connection(websocket, user.id)
    gw.hub.register(conn)
    log.info("ws connected user=%s", user.username)
    try:
        while True:
            raw = await websocket.receive_json()
            try:
                frame = envelopes.parse_inbound(raw)
            except envelopes.FrameError as e:
                await conn.send(envelopes.error("bad_frame", str(e),
                                                raw.get("client_msg_id") if isinstance(raw, dict) else None))
                continue
            if frame["type"] == "subscribe":
                async with SessionLocal() as session:
                    await _handle_subscribe(conn, frame, session)
            elif frame["type"] == "send":
                await _handle_send(gw, conn, user, frame)
    except WebSocketDisconnect:
        pass
    finally:
        gw.hub.unregister(conn)
        log.info("ws disconnected user=%s", user.username)


async def _handle_subscribe(conn: Connection, frame: dict, session) -> None:
    """Subscribe to channels and reply with the live/history fence per channel.

    ORDERING INVARIANT (design 04 §Gap 2 — the whole point of Change B):
    add the channels to ``conn.subscribed`` BEFORE reading the fence. A message
    that persists + fanouts in the window between these two steps is then either
    delivered live (the connection is already subscribed) or already counted in
    the fence — never both missed. Reverse the order and that window leaks: the
    message is ``> fence`` (client won't fetch it from history) AND not delivered
    live (not yet subscribed) -> lost forever on a then-quiet channel. The cost
    of subscribing first is at worst a duplicate (``<= fence`` AND live), which
    the client's drift cache dedups on the UNIQUE serverUlid.

    ACL (I2, #36): filter the requested ids to the channels the user may read
    FIRST — public channels, or private channels they belong to. Inaccessible /
    unknown ids are silently dropped from both ``conn.subscribed`` and the suback
    (existence-hiding). This runs BEFORE the subscribe-then-fence steps so it
    never disturbs that ordering invariant; ``hub.fanout`` only reaches the
    ``subscribed`` set, so gating it here also gates live delivery (defence in
    depth — a non-member can be neither backfilled nor pushed to).
    """
    accessible = await acl.filter_readable_ids(session, conn.user_id, frame["channel_ids"])
    conn.subscribed |= set(accessible)  # (1) subscribed FIRST (accessible only)
    fences = {  # (2) fence AFTER — every gap message is now live or <= fence
        # Per-viewer fence: latest_ulid applies the same block-visibility filter
        # get_history does, so the fence never points at a message blocked from
        # this viewer (which history would never return → reconnect-loop hang).
        cid: await messages_service.latest_ulid(session, cid, conn.user_id)
        for cid in accessible
    }
    await conn.send(envelopes.suback(fences))


async def _handle_send(gw, conn: Connection, user, frame: dict) -> None:
    async with SessionLocal() as session:
        # Existence-hiding (#36): one query resolves existence AND access, so a
        # non-member of a private channel gets the same "no_channel" as a missing
        # one — identical DB work, no timing leak.
        channel = await acl.readable_channel(session, user.id, frame["channel_id"])
        if channel is None:
            await conn.send(envelopes.error("no_channel", "channel not found",
                                            frame["client_msg_id"]))
            return
        # A member who exists but lacks can_post already knows the channel is real,
        # so this is an honest 'forbidden' (no existence leak) rather than collapse.
        if not await acl.can_post(session, user.id, channel):
            await conn.send(envelopes.error("forbidden", "cannot post to this channel",
                                            frame["client_msg_id"]))
            return
        # NO-INTERACTION across a block (#7): a reply to a message authored by a
        # user in a block relationship with the sender is refused. The single door
        # any future interaction surface (DMs, mentions) must also pass through is
        # `moderation_service.is_blocked_between`. A reply to an external-actor
        # message (sender_user_id NULL) or a now-deleted target is unaffected.
        reply_to = frame.get("reply_to")
        if reply_to is not None:
            target = await messages_service.get_message(session, reply_to)
            if (target is not None and target.sender_user_id is not None
                    and await moderation_service.is_blocked_between(
                        session, user.id, target.sender_user_id)):
                await conn.send(envelopes.error(
                    "blocked", "cannot reply to a blocked user",
                    frame["client_msg_id"]))
                return
        row, created = await messages_service.create_outbound(
            session, user=user, channel=channel,
            body=frame["body"], client_msg_id=frame["client_msg_id"],
            reply_to=reply_to,
        )
        view = messages_service.message_view(row)
        # Block exclusion for live fanout: everyone in a block relationship with
        # the sender must NOT receive this frame (the live twin of the history
        # visibility filter). Computed inside the session; applied in-memory by
        # the hub so fanout stays one query, not one-per-connection.
        exclude = await moderation_service.blocked_pair_user_ids(session, user.id)

    # Ack the sender (optimistic-send reconciliation: client_msg_id -> server id).
    await conn.send(envelopes.ack(frame["client_msg_id"], row.id, view["created_at"]))

    if created:
        # Mark our publish so its bus echo is dropped by ingest (Phase 0 payoff).
        echo.mark_sent(channel.aiko_channel, user.aiko_username, frame["body"])
        gw.bus.send(user.aiko_username, channel.aiko_channel, frame["body"])
        # Fan the persisted message out to channel subscribers.
        await gw.hub.fanout(channel.id, envelopes.message_frame(view),
                            exclude_user_ids=exclude)
