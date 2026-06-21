"""Echo suppression — the Phase 0 spike payoff.

The aiko server republishes every message onto the channel topic, so a message
the gateway publishes comes straight back to its own subscription (Phase 0
verified: echo present, `username` byte-exact). Without dedupe, a gateway-
originated message would be persisted once at send-time AND again on the echo.

We record a short-TTL key `(aiko_channel, aiko_username, body)` at send-time;
ingest drops any inbound matching a live key. Dev uses this in-memory set
(single worker); deploy swaps the same interface for a redis set shared across
workers (plan §A5) — that's the only change for horizontal scale.
"""
from __future__ import annotations

import time

_TTL_SECONDS = 30.0
_seen: dict[tuple[str, str, str], float] = {}


def _key(aiko_channel: str, aiko_username: str, body: str) -> tuple[str, str, str]:
    return (aiko_channel, aiko_username, body)


def _evict(now: float) -> None:
    expired = [k for k, exp in _seen.items() if exp <= now]
    for k in expired:
        _seen.pop(k, None)


def mark_sent(aiko_channel: str, aiko_username: str, body: str) -> None:
    """Record that the gateway just published this; its echo should be dropped."""
    now = time.time()
    _evict(now)
    _seen[_key(aiko_channel, aiko_username, body)] = now + _TTL_SECONDS


def is_own_echo(aiko_channel: str, aiko_username: str | None, body: str) -> bool:
    """True if this inbound matches a recent gateway-originated send (consume it)."""
    if aiko_username is None:
        return False
    now = time.time()
    _evict(now)
    return _seen.pop(_key(aiko_channel, aiko_username, body), None) is not None
