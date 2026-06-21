"""FastAPI application — the gateway spine.

Phase 1 (persistence slice): boot the aiko bus client, persist every observed
bus message into its channel (Postgres), and serve channel-list + history over
REST. Auth, the real WSS contract, and echo-suppressed send-then-persist land
next (plan §A1-A5).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import select

from .aiko.client import AikoBusClient
from .aiko.payload import InboundMessage
from .config import settings
from .db import SessionLocal, init_models
from .domain import messages_service
from .domain.models import Channel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aiko_gateway")

settings.export_aiko_env()  # aiko_services reads AIKO_MQTT_* from os.environ


class GatewayState:
    def __init__(self) -> None:
        self.bus: AikoBusClient | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    def on_bus_message(self, msg: InboundMessage) -> None:
        """AIKO thread -> hop to the asyncio loop, then persist."""
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._persist(msg))
        )

    async def _persist(self, msg: InboundMessage) -> None:
        try:
            async with SessionLocal() as session:
                row = await messages_service.persist_inbound(session, msg)
            if row:
                log.info("persisted %s in %s: %s", row.id, msg.channel, msg.message)
        except Exception:
            log.exception("persist failed for bus message")


state = GatewayState()


async def _seed_channels() -> None:
    """Ensure each configured aiko channel has a gateway Channel row (Phase 1)."""
    async with SessionLocal() as session:
        for ch in settings.aiko_channels:
            exists = (await session.execute(
                select(Channel).where(Channel.aiko_channel == ch)
            )).scalar_one_or_none()
            if exists is None:
                session.add(Channel(name=ch, kind="standard", aiko_channel=ch, is_private=False))
        await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.loop = asyncio.get_running_loop()
    await init_models()
    await _seed_channels()
    state.bus = AikoBusClient(settings.aiko_channels, state.on_bus_message)
    state.bus.start()
    log.info("Gateway started; channels=%s", settings.aiko_channels)
    try:
        yield
    finally:
        if state.bus is not None:
            state.bus.stop()


app = FastAPI(title="Aiko Chat Gateway", version="0.0.1", lifespan=lifespan)


def _msg_view(m) -> dict:
    return {
        "msg_id": m.id, "channel_id": m.channel_id,
        "sender": {"user_id": m.sender_user_id, "kind": m.sender_kind, "label": m.sender_label},
        "body": m.body, "created_at": m.created_at.isoformat(),
        "reply_to": m.reply_to,
    }


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "aiko_connected": bool(state.bus and state.bus.connected),
        "channels": settings.aiko_channels,
    }


@app.get("/v1/channels")
async def list_channels() -> dict:
    async with SessionLocal() as session:
        rows = list((await session.execute(select(Channel))).scalars())
    return {"channels": [
        {"id": c.id, "name": c.name, "kind": c.kind, "aiko_channel": c.aiko_channel}
        for c in rows
    ]}


@app.get("/v1/channels/{channel_id}/messages")
async def history(
    channel_id: str,
    before: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
) -> dict:
    async with SessionLocal() as session:
        channel = await session.get(Channel, channel_id)
        if channel is None:
            raise HTTPException(404, "channel not found")
        rows = await messages_service.get_history(
            session, channel_id, before=before, limit=limit
        )
    return {
        "channel_id": channel_id,
        "messages": [_msg_view(m) for m in rows],
        "next_before": rows[0].id if rows else None,
    }


@app.post("/v1/_debug/send")
def debug_send(channel: str, username: str, message: str) -> dict:
    """Temporary (pre-auth): publish onto the bus. The echo is what gets persisted."""
    ok = bool(state.bus and state.bus.send(username, channel, message))
    return {"sent": ok}
