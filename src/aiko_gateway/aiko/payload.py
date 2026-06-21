"""Codec for the aiko_chat wire payload.

Phase 0 verified the inbound shape is exactly:
    {"username": str, "channel": str, "timestamp": float, "message": str}
with `username` preserved byte-exact (so it is usable in the echo-dedupe key).
Legacy/older builds may publish a bare message string; we handle both.

This is one of only two churn-exposed files (with client.py) — when aiko's wire
format moves (JSON->S-expr, function-call protocol), the change lands here and
the /v1 contract stays frozen.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class InboundMessage:
    """A parsed channel payload. `username` is None for legacy bare strings."""
    username: str | None
    channel: str | None
    timestamp: float | None
    message: str
    raw: str


def parse_payload(payload_in: str, *, fallback_channel: str | None = None) -> InboundMessage:
    """Parse a channel payload into an InboundMessage (never raises)."""
    try:
        data = json.loads(payload_in)
    except (TypeError, ValueError):
        return InboundMessage(None, fallback_channel, None, payload_in, payload_in)
    if isinstance(data, dict) and "message" in data:
        return InboundMessage(
            username=data.get("username"),
            channel=data.get("channel", fallback_channel),
            timestamp=data.get("timestamp"),
            message=data["message"],
            raw=payload_in,
        )
    # JSON, but not a chat payload shape — treat the raw text as the message.
    return InboundMessage(None, fallback_channel, None, payload_in, payload_in)
