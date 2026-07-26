"""Echo suppression — the Phase 0 spike payoff.

The aiko server republishes every message onto the channel topic, so a message
the gateway publishes comes straight back to its own subscription (Phase 0
verified: echo present, `username` byte-exact). Without dedupe, a gateway-
originated message would be persisted once at send-time AND again on the echo.

We record a short-TTL key `(aiko_channel, aiko_username, body)` at send-time;
ingest drops any inbound matching a live key. Dev uses this in-memory set
(single worker); deploy swaps the same interface for a redis set shared across
workers (plan §A5) — that's the only change for horizontal scale.

Because the key is content, two IDENTICAL messages from the same user in the
same channel collide on one key. Each outstanding publish must therefore be able
to suppress exactly ONE echo: the value is a LIST of per-publish expiries (a
multiset by count), so N identical sends record N pending echoes and consume N
on the way back. A single-slot value would drop only the first echo and
re-persist every subsequent duplicate (the message appearing twice). The real
fix is to carry a message id across the bus and dedupe on identity, not content
(task #42, aiko_chat discussion #9); this keeps the content scheme honest until
then.
"""
from __future__ import annotations

import time

_TTL_SECONDS = 30.0
# key -> pending echo expiries, one entry per outstanding gateway publish.
_seen: dict[tuple[str, str, str], list[float]] = {}


def _key(aiko_channel: str, aiko_username: str, body: str) -> tuple[str, str, str]:
    return (aiko_channel, aiko_username, body)


def _evict(now: float) -> None:
    for k in list(_seen):
        live = [exp for exp in _seen[k] if exp > now]
        if live:
            _seen[k] = live
        else:
            del _seen[k]


def mark_sent(aiko_channel: str, aiko_username: str, body: str) -> None:
    """Record that the gateway just published this; its echo should be dropped.

    Appends one pending expiry so repeated identical sends each suppress their
    own echo (not just the first)."""
    now = time.time()
    _evict(now)
    _seen.setdefault(_key(aiko_channel, aiko_username, body), []).append(
        now + _TTL_SECONDS)


def is_own_echo(aiko_channel: str, aiko_username: str | None, body: str) -> bool:
    """True if this inbound matches a recent gateway-originated send (consume one).

    Consumes a single pending echo per call (FIFO), so the Nth identical inbound
    beyond the number we published is treated as a genuine new message."""
    if aiko_username is None:
        return False
    now = time.time()
    _evict(now)
    key = _key(aiko_channel, aiko_username, body)
    pending = _seen.get(key)
    if not pending:
        return False
    pending.pop(0)  # consume one outstanding echo
    if not pending:
        del _seen[key]
    return True
