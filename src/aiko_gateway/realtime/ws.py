"""The /v1/ws endpoint — authenticated realtime send/receive.

Handshake: ?token=<access jwt>. Invalid token -> close 1008 before any frame
(invariant I1 for the WS path). The send path is the one transaction boundary
for an outgoing message (plan): validate -> persist (server ULID, server-derived
sender) -> mark echo -> publish to bus -> ack sender -> fanout to subscribers.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from ..db import SessionLocal
from ..domain import (
    acl, auth_session, echo, mentions, messages_service,
    moderation_service, signing,
)
from . import envelopes
from .hub import Connection

log = logging.getLogger("aiko_gateway.ws")
router = APIRouter()


@router.websocket("/v1/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token", "")
    # Thin adapter over the shared session resolver (auth_session): the SAME
    # per-user gate as REST + refresh, applied at the ingress that was historically
    # the one forgotten (it decodes tokens outside get_current_user). Any neutral
    # rejection — invalid/expired/revoked token, unknown user, OR a ban — closes
    # 1008 with no body (a frame has none; the app reclassifies the drop via a REST
    # refresh, no existence leak). An already-open socket is dropped separately by
    # hub.disconnect_user at policy-change time (active-disconnect, out of scope here).
    async with SessionLocal() as session:
        try:
            user = await auth_session.resolve_session_user(
                session, token, expected_type="access")
        except (auth_session.InvalidSession, auth_session.SessionBanned):
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
        # reply_to integrity + NO-INTERACTION across a block (#7, cage-match Carnot).
        # The reply target must EXIST and live in THIS channel: a missing id would
        # FK-500 at insert (messages.reply_to is an FK), and a cross-channel id
        # would mint a reference to a message the sender may not be able to read in
        # context. Both collapse to the same "no_reply_target" (don't confirm which
        # — a bad/foreign id is indistinguishable). THEN the block gate: a reply to
        # a message authored by a user in a block relationship is refused. The
        # single door future interaction surfaces (DMs, mentions) must also pass is
        # `moderation_service.is_blocked_between`. A reply to an external-actor
        # message (sender_user_id NULL) or a soft-deleted same-channel target is
        # allowed (the replier saw it before it went).
        reply_to = frame.get("reply_to")
        if reply_to is not None:
            target = await messages_service.get_message(session, reply_to)
            if target is None or target.channel_id != channel.id:
                await conn.send(envelopes.error(
                    "no_reply_target", "reply target not found in this channel",
                    frame["client_msg_id"]))
                return
            # BLOCK gate on the reply — NON-DM channels ONLY (#2633; cage-match PR#124
            # round 7 Carnot/Tesla P0). For a DM, the block refusal is owned by the
            # mutator (create_outbound: idempotency-first, then DM peer-block →
            # BlockedDmSend → no_channel). Doing it HERE would short-circuit BEFORE that
            # idempotency check, so a reply RETRY-after-block would get no_channel instead
            # of reconciling the already-persisted row — the exact idempotency break the
            # plain-send fix removed, hiding in the reply arm. A DM has only the peer to
            # reply to, so create_outbound's peer-block gate covers the reply case
            # uniformly (new → no_channel; retry → ack). The reply-target validation above
            # (no_reply_target) still runs for DMs — only this block sub-check is deferred.
            if (channel.kind != "dm"
                    and target.sender_user_id is not None
                    and await moderation_service.is_blocked_between(
                        session, user.id, target.sender_user_id)):
                await conn.send(envelopes.error(
                    "blocked", "cannot reply to a blocked user",
                    frame["client_msg_id"]))
                return
        # Sovereign-signing carriage (#1816): shape-validate the `origin` envelope
        # at the trust boundary (never trust its claimed alg; its client_msg_id must
        # match the frame's). The gateway carries it verbatim, it does NOT verify
        # the signature. Absent origin is legal (unsigned message). A malformed
        # envelope is rejected fail-closed rather than persisted.
        try:
            origin = signing.validate_origin(
                frame.get("origin"), frame_client_msg_id=frame["client_msg_id"])
        except signing.OriginError as e:
            await conn.send(envelopes.error("bad_origin", str(e), frame["client_msg_id"]))
            return
        # Key-bound @-mention spans (#2632): SHAPE+caps validated at the trust
        # boundary, then carried VERBATIM. Fail-closed — a malformed array is
        # rejected, never persisted, exactly like a bad origin. Absent is legal.
        #
        # WHY NO BLOCK GATE HERE (deliberate layering, not an oversight): a mention
        # span is INERT carrier metadata, and the block invariant is enforced where
        # the INTERACTION happens, not at carriage. The message body carrying the
        # span is ALREADY block-excluded from every delivery path — the live fanout
        # `exclude` below and `get_history`'s block predicate both drop a blocked
        # party's view — so a stored span never reaches a blocked user through any
        # read path. The ONE surface that could reach them is a "you were @'d"
        # NOTIFICATION, which does not exist yet (#2526) and MUST block-filter at
        # THAT layer (tracked). Enforcing at carriage instead would force the gateway
        # to resolve `target_id`'s identity + recognize a reserved `target_type`
        # value — breaking the value-opaque, resolver-free carrier contract that
        # keeps the /v1 wire append-only. Block enforcement lives at the interaction
        # layer; carriage stays a pure carrier. (Blocking does not un-name a person:
        # a third party may still see "@someone" — it governs interaction, not
        # nameability.)
        try:
            spans = mentions.validate_mentions(frame.get("mentions"))
        except mentions.MentionError as e:
            await conn.send(envelopes.error("bad_mentions", str(e), frame["client_msg_id"]))
            return
        try:
            row, created = await messages_service.create_outbound(
                session, user=user, channel=channel,
                body=frame["body"], client_msg_id=frame["client_msg_id"],
                reply_to=reply_to, origin=origin, mentions=spans,
            )
        except messages_service.BlockedDmSend:
            # DM send refused under a block (design 11 §Decision 5, enforced at the mutator
            # door). Collapse into the SAME existence-hiding no_channel as a missing /
            # unreadable channel (Nick's approved shape) so the refusal doesn't add a
            # distinct "blocked" code — symmetric in both block directions.
            await conn.send(envelopes.error("no_channel", "channel not found",
                                            frame["client_msg_id"]))
            return
        view = messages_service.message_view(row)
        # Block exclusion for live fanout: everyone in a block relationship with the
        # sender must NOT receive this frame (the live twin of the history visibility
        # filter). Computed inside the session; applied in-memory by the hub so
        # fanout stays one query, not one-per-connection.
        exclude = await moderation_service.blocked_pair_user_ids(session, user.id)

    # Ack the sender (optimistic-send reconciliation: client_msg_id -> server id).
    await conn.send(envelopes.ack(frame["client_msg_id"], row.id, view["created_at"]))

    if created:
        # A DM NEVER FEDERATES ON THE BUS (#2633, design 11 §Decision 3). The federate
        # decision is the DOMAIN's (messages_service.should_federate), NOT a predicate this
        # route re-derives — so a second send path can't forget the DM gate and leak a
        # private room (cage-match PR#124 round 12 Tesla P1: egress lives behind the same
        # single domain door as the block gate). It is False for a DM (dual-gated on kind
        # AND the dm: prefix, so a lone kind retint can't re-open it).
        if messages_service.should_federate(channel):
            # Mark our publish so its bus echo is dropped by ingest (Phase 0 payoff).
            echo.mark_sent(channel.aiko_channel, user.aiko_username, frame["body"])
            gw.bus.send(user.aiko_username, channel.aiko_channel, frame["body"])
        # Fan the persisted message out to channel subscribers (local; both DM members).
        await gw.hub.fanout(channel.id, envelopes.message_frame(view),
                            exclude_user_ids=exclude)
