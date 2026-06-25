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
from .domain import channels_service, echo, messages_service
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

    # -- channel topology reconcile (#1281 incr 2) --------------------------- #
    # The aiko ChatServer's `channel_list` EC share is the canonical source of
    # channel existence. These callbacks fire on the AIKO thread; each hops to
    # the asyncio loop (same bridge as on_bus_message) and reconciles into the
    # local Channel rows. Replaces the old independent `_seed_channels`.

    def on_channel_add(self, aiko_channel: str) -> None:
        """AIKO thread -> asyncio loop -> idempotent channel upsert."""
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._reconcile_channel_add(aiko_channel))
        )

    async def _reconcile_channel_add(self, aiko_channel: str) -> None:
        try:
            async with SessionLocal() as session:
                await channels_service.upsert_channel(session, aiko_channel)
            log.info("channel reconcile: + %s", aiko_channel)
        except Exception:
            log.exception("channel add reconcile failed: %s", aiko_channel)

    def on_channel_remove(self, aiko_channel: str) -> None:
        """AIKO thread -> asyncio loop -> IRREVERSIBLE hard-delete (Decision B:
        only ever called on a live-producer EC remove, never on disconnect)."""
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._reconcile_channel_remove(aiko_channel))
        )

    async def _reconcile_channel_remove(self, aiko_channel: str) -> None:
        try:
            async with SessionLocal() as session:
                deleted = await channels_service.hard_delete_channel(session, aiko_channel)
            if deleted:
                log.warning(
                    "channel reconcile: HARD-DELETED %s (+ its messages + memberships)",
                    aiko_channel,
                )
        except Exception:
            log.exception("channel remove reconcile failed: %s", aiko_channel)


state = GatewayState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.loop = asyncio.get_running_loop()
    await init_models()
    # No independent seeding: channels are reconciled from the ChatServer
    # `channel_list` EC share once the bus client discovers it. An inbound
    # message for a not-yet-reconciled channel is upserted by persist_inbound
    # (closes the startup window). See #1281 incr 2.
    # Lazy import: pulling aiko_services happens only at startup, never at
    # module import time (see the import-block note above).
    from .aiko.client import AikoBusClient
    state.bus = AikoBusClient(
        settings.aiko_channels, state.on_bus_message,
        on_channel_add=state.on_channel_add,
        on_channel_remove=state.on_channel_remove,
    )
    state.bus.start()
    log.info("Gateway started; subscribed channels=%s (topology via channel_list share)",
             settings.aiko_channels)
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
