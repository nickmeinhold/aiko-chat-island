"""FastAPI application — the gateway spine.

Phase 1 skeleton: boot the aiko bus client on its daemon thread, expose health
+ a debug view proving inbound messages flow from the bus into the asyncio app.
Auth, the real WSS contract, Postgres history, and ACLs land in subsequent
slices (see plan §A1-A4). This file's job right now is: the process starts, the
aiko thread connects, and bus messages reach the event loop.
"""
from __future__ import annotations

import asyncio
import collections
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .aiko.client import AikoBusClient
from .aiko.payload import InboundMessage
from .config import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aiko_gateway")

# Ensure aiko_services sees our MQTT config before any aiko process composes.
settings.export_aiko_env()


class GatewayState:
    """Holds the bus client + the asyncio loop, and bridges the two threads."""

    def __init__(self) -> None:
        self.bus: AikoBusClient | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        # Debug ring buffer proving inbound flow (replaced by WS fanout later).
        self.recent: collections.deque[dict] = collections.deque(maxlen=50)

    def on_bus_message(self, msg: InboundMessage) -> None:
        """Called on the AIKO thread. Hop to the asyncio loop thread-safely."""
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._ingest, msg)

    def _ingest(self, msg: InboundMessage) -> None:
        # Runs on the asyncio loop thread. (Real ingest: dedupe -> persist ->
        # fanout -> push. For now just record it.)
        self.recent.append({
            "username": msg.username,
            "channel": msg.channel,
            "timestamp": msg.timestamp,
            "message": msg.message,
        })
        log.info("ingest %s@%s: %s", msg.username, msg.channel, msg.message)


state = GatewayState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.loop = asyncio.get_running_loop()
    state.bus = AikoBusClient(settings.aiko_channels, state.on_bus_message)
    state.bus.start()
    log.info("Gateway started; aiko bus client launched (channels=%s)",
             settings.aiko_channels)
    try:
        yield
    finally:
        if state.bus is not None:
            state.bus.stop()


app = FastAPI(title="Aiko Chat Gateway", version="0.0.1", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "aiko_connected": bool(state.bus and state.bus.connected),
        "channels": settings.aiko_channels,
    }


@app.get("/v1/_debug/recent")
def recent() -> dict:
    """Temporary: prove bus->asyncio inbound flow before the WSS layer exists."""
    return {"count": len(state.recent), "messages": list(state.recent)}


@app.post("/v1/_debug/send")
def debug_send(channel: str, username: str, message: str) -> dict:
    """Temporary: send a message onto the bus (pre-auth, pre-WS)."""
    ok = bool(state.bus and state.bus.send(username, channel, message))
    return {"sent": ok}
