"""Deliver a held call invite once it can no longer ring (design 21).

History alone cannot carry the missed call. A connected client takes everything
above its subscribe fence from the live socket and never pages it, so a released
invite would sit unseen until the next reconnect. This sends it live, by id order,
to the people it was held from. The app inserts it in place and refuses to ring it
as `stale` (checked against the app's code by the app tab, 2026-10-29).

A SWEEPER, NOT A TIMER PER INVITE. Its only inputs are the database and the clock
(`call_hold.due_releases`), so a hold that spans a restart is found again and
nothing has to be re-armed. On boot it starts from now: nobody is connected yet to
receive what fell due while the island was down, and those clients read it from
history when they reconnect, where it is now visible.

AT-LEAST-ONCE. A failed tick does not advance the window, so the next tick sends
it again. A duplicate is harmless: the client dedups on the server id. The window a
retry covers is clamped to MAX_CATCHUP_MS, so a tick that keeps failing cannot grow
its scan without bound (Noether, #445 r1); an invite released further back than that
reaches its recipient through history on their next walk.
"""
from __future__ import annotations

import asyncio
import logging

from ..db import SessionLocal
from ..domain import call_hold, messages_service
from . import envelopes
from .hub import Hub

log = logging.getLogger("aiko_gateway.call_release")

INTERVAL_SECONDS = 2.0
MAX_CATCHUP_MS = 5 * 60_000


async def release_once(hub: Hub, session, *, after_ms: int, upto_ms: int) -> int:
    """Send every invite whose hold ended in (after_ms, upto_ms] to the users it was
    held from, each with their own view (reactions are per viewer). Returns how many
    CONNECTIONS received one, which is zero when nobody it was held from is listening:
    that is not a failure (history carries it), but it must not read as a delivery."""
    delivered = 0
    due = await call_hold.due_releases(session, after_ms=after_ms, upto_ms=upto_ms)
    for msg, recipients in due:
        reached = 0
        for user_id in recipients:
            (view,) = await messages_service.message_views(session, [msg], user_id)
            reached += await hub.fanout(msg.channel_id, envelopes.message_frame(view),
                                        only_user_ids={user_id})
        log.info("call invite released msg=%s channel=%s recipients=%d connections=%d",
                 msg.id, msg.channel_id, len(recipients), reached)
        delivered += reached
    return delivered


async def release_loop(hub: Hub, interval: float = INTERVAL_SECONDS) -> None:
    last = call_hold.now_ms()
    while True:
        await asyncio.sleep(interval)
        upto = call_hold.now_ms()
        last = max(last, upto - MAX_CATCHUP_MS)
        try:
            async with SessionLocal() as session:
                await release_once(hub, session, after_ms=last, upto_ms=upto)
            last = upto
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("call release tick failed; the window is retried next tick")
