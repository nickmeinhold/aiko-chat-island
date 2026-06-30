"""FastAPI application — the gateway spine.

Phase 1 (persistence slice): boot the aiko bus client, persist every observed
bus message into its channel (Postgres), and serve channel-list + history over
REST. Auth, the real WSS contract, and echo-suppressed send-then-persist land
next (plan §A1-A5).
"""
from __future__ import annotations

import asyncio
import contextlib
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
from .db import SessionLocal, verify_schema
from .domain import channels_service, echo, messages_service, moderation_service
from .realtime.hub import Hub

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aiko_gateway")

settings.export_aiko_env()  # aiko_services reads AIKO_MQTT_* from os.environ


class GatewayState:
    def __init__(self) -> None:
        self.bus: "AikoBusClient | None" = None
        self.hub: Hub = Hub()
        self.loop: asyncio.AbstractEventLoop | None = None
        # Single ordered lane for channel topology events (set up in lifespan
        # once the loop exists). One worker drains it FIFO so an add/remove pair
        # for the same channel can never interleave (cage-match PR#12, P1a).
        self._channel_events: "asyncio.Queue[tuple[str, str]] | None" = None
        self._channel_worker: "asyncio.Task | None" = None

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
                # Block exclusion (#7): a bus message can map to a real gateway
                # user (sender_user_id set) — that human's blocks apply to live
                # delivery just as on the send path. An external actor (NULL
                # sender) has no block relationships, so the set is empty.
                exclude = (
                    await moderation_service.blocked_pair_user_ids(session, row.sender_user_id)
                    if row and row.sender_user_id else set()
                )
            if row:
                log.info("ingest %s in %s: %s", row.id, msg.channel, msg.message)
                # External message (LLM/robot/REPL/other) -> fan out live.
                await self.hub.fanout(
                    row.channel_id,
                    {"type": "message", "msg": messages_service.message_view(row)},
                    exclude_user_ids=exclude,
                )
        except Exception:
            log.exception("ingest failed for bus message")

    # -- channel topology reconcile (#1281 incr 2) --------------------------- #
    # The aiko ChatServer's `channel_list` EC share is the canonical source of
    # channel existence. Callbacks fire on the AIKO thread and enqueue onto a
    # SINGLE ordered asyncio queue; one worker drains it FIFO and reconciles
    # into the local Channel rows. The queue is what makes ordering safe — a bare
    # `create_task` per event let an add/remove pair for the same channel
    # interleave at the DB await and finish out of order, which for an
    # irreversible hard-delete is unacceptable (cage-match PR#12, Carnot P1a).
    # Replaces the old independent `_seed_channels`.

    def on_channel_add(self, aiko_channel: str) -> None:
        self._enqueue_channel_event("add", aiko_channel)

    def on_channel_remove(self, aiko_channel: str) -> None:
        self._enqueue_channel_event("remove", aiko_channel)

    def _enqueue_channel_event(self, action: str, aiko_channel: str) -> None:
        """AIKO thread -> the one ordered topology queue (thread-safe handoff)."""
        if self.loop is None or self._channel_events is None:
            return
        self.loop.call_soon_threadsafe(
            self._channel_events.put_nowait, (action, aiko_channel)
        )

    async def _run_channel_worker(self) -> None:
        """Single consumer of the topology queue — serializes every add/remove so
        they apply in arrival order. Owns the transaction boundary (the services
        only flush)."""
        assert self._channel_events is not None
        while True:
            action, aiko_channel = await self._channel_events.get()
            try:
                async with SessionLocal() as session:
                    if action == "add":
                        await channels_service.upsert_channel(session, aiko_channel)
                        await session.commit()
                        log.info("channel reconcile: + %s", aiko_channel)
                    elif action == "remove":
                        deleted = await channels_service.hard_delete_channel(
                            session, aiko_channel)
                        await session.commit()
                        if deleted:
                            log.warning(
                                "channel reconcile: HARD-DELETED %s "
                                "(+ its messages + memberships)", aiko_channel)
            except Exception:
                log.exception("channel reconcile failed: %s %s", action, aiko_channel)
            finally:
                self._channel_events.task_done()


state = GatewayState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.loop = asyncio.get_running_loop()
    # Alembic (run by the container entrypoint before uvicorn) owns schema
    # creation/evolution; here we only VERIFY the live schema is migrated +
    # current, failing closed if not (#14).
    await verify_schema()
    # No independent seeding: channels are reconciled from the ChatServer
    # `channel_list` EC share once the bus client discovers it. An inbound
    # message for a not-yet-reconciled channel is upserted by persist_inbound
    # (closes the startup window). See #1281 incr 2.
    state._channel_events = asyncio.Queue()
    state._channel_worker = asyncio.create_task(state._run_channel_worker())
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
        if state._channel_worker is not None:
            state._channel_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state._channel_worker  # clean task ownership (Carnot r2)


app = FastAPI(title="Aiko Chat Gateway", version="0.0.1", lifespan=lifespan)
app.state.gw = state  # the WS endpoint reaches bus + hub via websocket.app.state.gw

# Cap request body size app-wide (#28) — rejects oversized bodies with 413 before
# they reach a route. Generous cap (no upload endpoint); see middleware + config.
from .middleware import ContentSizeLimitMiddleware  # noqa: E402
app.add_middleware(ContentSizeLimitMiddleware, max_bytes=settings.max_request_bytes)

from .rest import auth as auth_routes  # noqa: E402
from .rest import channels as channel_routes  # noqa: E402
from .rest import communities as community_routes  # noqa: E402
from .rest import devices as device_routes  # noqa: E402
from .rest import legal as legal_routes  # noqa: E402
from .rest import members as member_routes  # noqa: E402
from .rest import messages as message_routes  # noqa: E402
from .rest import moderation as moderation_routes  # noqa: E402
from .rest import well_known as well_known_routes  # noqa: E402
from .realtime import ws as ws_routes  # noqa: E402
app.include_router(auth_routes.router)
app.include_router(auth_routes.me_router)
app.include_router(channel_routes.router)
app.include_router(community_routes.router)
app.include_router(device_routes.router)
app.include_router(legal_routes.router)
app.include_router(member_routes.router)
app.include_router(message_routes.router)
app.include_router(moderation_routes.router)
app.include_router(ws_routes.router)
app.include_router(well_known_routes.router)


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
