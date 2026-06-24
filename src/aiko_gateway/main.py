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
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, status
from sqlalchemy import select

# NOTE: `AikoBusClient` is imported lazily inside `lifespan` (not at module
# scope) so that `import aiko_gateway.main` does NOT transitively pull in
# `aiko_services` (an undeclared dependency absent on clean CI). This keeps the
# production app importable under the test suite's "never import aiko_services"
# isolation invariant, which lets a test introspect the real route table +
# auth dependency tree without the bus. See tests/test_main_routes.py.
from .aiko.payload import InboundMessage

if TYPE_CHECKING:
    from .aiko.client import AikoBusClient
from .config import settings
from .db import SessionLocal, init_models
from .domain import echo, messages_service
from .domain.models import Channel
from .realtime.hub import Hub

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aiko_gateway")

settings.export_aiko_env()  # aiko_services reads AIKO_MQTT_* from os.environ


class GatewayState:
    def __init__(self) -> None:
        self.bus: "AikoBusClient | None" = None
        self.hub: Hub = Hub()
        self.loop: asyncio.AbstractEventLoop | None = None

    def on_bus_message(self, msg: InboundMessage) -> None:
        """AIKO thread -> hop to the asyncio loop, then ingest."""
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._ingest(msg))
        )

    async def _ingest(self, msg: InboundMessage) -> None:
        # Drop our own echo — it was already persisted + fanned out at send-time.
        if msg.channel and echo.is_own_echo(msg.channel, msg.username, msg.message):
            return
        try:
            async with SessionLocal() as session:
                row = await messages_service.persist_inbound(session, msg)
            if row:
                log.info("ingest %s in %s: %s", row.id, msg.channel, msg.message)
                # External message (LLM/robot/REPL/other) -> fan out live.
                await self.hub.fanout(
                    row.channel_id, {"type": "message", "msg": messages_service.message_view(row)}
                )
        except Exception:
            log.exception("ingest failed for bus message")


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
    # Lazy import: pulling aiko_services happens only at startup, never at
    # module import time (see the import-block note above).
    from .aiko.client import AikoBusClient
    state.bus = AikoBusClient(settings.aiko_channels, state.on_bus_message)
    state.bus.start()
    log.info("Gateway started; channels=%s", settings.aiko_channels)
    try:
        yield
    finally:
        if state.bus is not None:
            state.bus.stop()


app = FastAPI(title="Aiko Chat Gateway", version="0.0.1", lifespan=lifespan)
app.state.gw = state  # the WS endpoint reaches bus + hub via websocket.app.state.gw

from .rest import auth as auth_routes  # noqa: E402
from .rest import channels as channel_routes  # noqa: E402
from .rest import members as member_routes  # noqa: E402
from .rest import messages as message_routes  # noqa: E402
from .realtime import ws as ws_routes  # noqa: E402
app.include_router(auth_routes.router)
app.include_router(auth_routes.me_router)
app.include_router(channel_routes.router)
app.include_router(member_routes.router)
app.include_router(message_routes.router)
app.include_router(ws_routes.router)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "aiko_connected": bool(state.bus and state.bus.connected),
        "channels": settings.aiko_channels,
    }


@app.post("/v1/_debug/send")
def debug_send(channel: str, username: str, message: str) -> dict:
    """Dev-only (pre-auth): publish onto the bus. The echo is what gets persisted.

    Gated to non-production: this endpoint has NO auth and would let anyone
    inject bus messages, so it 404s in production (fail-closed alongside the
    jwt_secret + registration guards)."""
    if settings.is_production:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    ok = bool(state.bus and state.bus.send(username, channel, message))
    return {"sent": ok}
