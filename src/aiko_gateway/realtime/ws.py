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
from ..domain import echo, messages_service, security, users_service
from ..domain.models import Channel
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
                conn.subscribed |= set(frame["channel_ids"])
            elif frame["type"] == "send":
                await _handle_send(gw, conn, user, frame)
    except WebSocketDisconnect:
        pass
    finally:
        gw.hub.unregister(conn)
        log.info("ws disconnected user=%s", user.username)


async def _handle_send(gw, conn: Connection, user, frame: dict) -> None:
    async with SessionLocal() as session:
        channel = await session.get(Channel, frame["channel_id"])
        if channel is None:
            await conn.send(envelopes.error("no_channel", "channel not found",
                                            frame["client_msg_id"]))
            return
        # (ACL can_post lands with the membership slice; Phase 1 allows members.)
        row, created = await messages_service.create_outbound(
            session, user=user, channel=channel,
            body=frame["body"], client_msg_id=frame["client_msg_id"],
            reply_to=frame.get("reply_to"),
        )
        view = messages_service.message_view(row)

    # Ack the sender (optimistic-send reconciliation: client_msg_id -> server id).
    await conn.send(envelopes.ack(frame["client_msg_id"], row.id, view["created_at"]))

    if created:
        # Mark our publish so its bus echo is dropped by ingest (Phase 0 payoff).
        echo.mark_sent(channel.aiko_channel, user.aiko_username, frame["body"])
        gw.bus.send(user.aiko_username, channel.aiko_channel, frame["body"])
        # Fan the persisted message out to channel subscribers.
        await gw.hub.fanout(channel.id, envelopes.message_frame(view))
