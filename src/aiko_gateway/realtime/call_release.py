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
it again. A duplicate is harmless: the client dedups on the server id.
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


async def release_once(hub: Hub, session, *, after_ms: int, upto_ms: int) -> int:
    """Send every invite whose hold ended in (after_ms, upto_ms] to the users it was
    held from. Returns how many invites went out."""
    due = await call_hold.due_releases(session, after_ms=after_ms, upto_ms=upto_ms)
    for msg, recipients in due:
        await hub.fanout(msg.channel_id,
                         envelopes.message_frame(messages_service.message_view(msg)),
                         only_user_ids=recipients)
        log.info("call invite released msg=%s channel=%s recipients=%d",
                 msg.id, msg.channel_id, len(recipients))
    return len(due)


async def release_loop(hub: Hub, interval: float = INTERVAL_SECONDS) -> None:
    last = call_hold.now_ms()
    while True:
        await asyncio.sleep(interval)
        upto = call_hold.now_ms()
        try:
            async with SessionLocal() as session:
                await release_once(hub, session, after_ms=last, upto_ms=upto)
            last = upto
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("call release tick failed; the window is retried next tick")
