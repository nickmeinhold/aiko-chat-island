"""In-process connection registry + channel fanout.

Phase 1 keeps connections in-process (single uvicorn worker). For multi-worker
deploy, fanout publishes to a redis channel and each worker delivers to its own
local connections (plan §A1 realtime/fanout) — the Hub interface stays the same.

Membership-at-delivery (invariant I2) is enforced here: fanout only reaches a
connection whose `subscribed` set includes the channel. The ACL check that
populates `subscribed` lands with the channels/membership slice; for now a
connection subscribes to what it asks for.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket

log = logging.getLogger("aiko_gateway.hub")


class Connection:
    def __init__(self, ws: WebSocket, user_id: str):
        self.ws = ws
        self.user_id = user_id
        self.subscribed: set[str] = set()

    async def send(self, frame: dict) -> None:
        await self.ws.send_json(frame)


class Hub:
    def __init__(self) -> None:
        self._conns: set[Connection] = set()

    def register(self, conn: Connection) -> None:
        self._conns.add(conn)

    def unregister(self, conn: Connection) -> None:
        self._conns.discard(conn)

    async def fanout(self, channel_id: str, frame: dict) -> None:
        """Deliver `frame` to every connection subscribed to `channel_id`."""
        targets = [c for c in self._conns if channel_id in c.subscribed]
        if not targets:
            return
        results = await asyncio.gather(
            *(c.send(frame) for c in targets), return_exceptions=True
        )
        for conn, res in zip(targets, results):
            if isinstance(res, Exception):
                log.debug("fanout drop (dead conn?): %s", res)
                self.unregister(conn)
