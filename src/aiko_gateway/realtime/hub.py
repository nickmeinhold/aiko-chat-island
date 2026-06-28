"""In-process connection registry + channel fanout.

Phase 1 keeps connections in-process (single uvicorn worker). For multi-worker
deploy, fanout publishes to a redis channel and each worker delivers to its own
local connections (plan §A1 realtime/fanout) — the Hub interface stays the same.

Membership-at-delivery (invariant I2) is enforced here: fanout only reaches a
connection whose `subscribed` set includes the channel. As of #36 that set is
ACL-gated at subscribe time (`ws._handle_subscribe` -> `acl.filter_readable_ids`
drops channels the user may not read), so a non-member can never appear in
`subscribed` and therefore can never be reached by fanout — this delivery gate
and the subscribe gate enforce the same boundary from two sides.
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

    async def fanout(
        self, channel_id: str, frame: dict, *, exclude_user_ids: set[str] | None = None
    ) -> None:
        """Deliver `frame` to every connection subscribed to `channel_id`, except
        connections owned by a user in `exclude_user_ids`.

        The exclusion set carries the moderation block boundary into live
        delivery (#7): the caller passes every user in a block relationship with
        the message's sender AS OF SEND TIME, so a blocked party's open connection
        is skipped for this frame — the live twin of the history/fence visibility
        filter. It is point-in-time, not transactional: a block committed in the
        window between the caller computing the set and this fanout is not reflected
        for the in-flight frame (cage-match Carnot LOW); the next send recomputes,
        and the history path hides it on reload. Empty / None means "no exclusions"
        (the common case)."""
        excluded = exclude_user_ids or frozenset()
        targets = [
            c for c in self._conns
            if channel_id in c.subscribed and c.user_id not in excluded
        ]
        if not targets:
            return
        results = await asyncio.gather(
            *(c.send(frame) for c in targets), return_exceptions=True
        )
        for conn, res in zip(targets, results):
            if isinstance(res, Exception):
                log.debug("fanout drop (dead conn?): %s", res)
                self.unregister(conn)
