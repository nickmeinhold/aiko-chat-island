"""Codec for the aiko_chat wire payload.

The inbound shape carries exactly:
    {"username": str, "channel": str, "timestamp": float, "message": str}
with `username` preserved byte-exact (so it is usable in the echo-dedupe key).

Three wire tiers are accepted, newest first (mirroring aiko_chat.protocol._decode_message):
  1. the current Aiko framework S-expression the ChatServer emits via
     ``generate("message", {...})`` —
     ``(message username: <u> channel: <c> timestamp: <t> message: <netstring>)``;
  2. the legacy JSON object from the previous wire format; and
  3. a bare message string (oldest publishers).

This is one of only two churn-exposed files (with client.py) — when aiko's wire
format moves (as it did, JSON->S-expr, function-call protocol), the change lands here
and the /v1 contract stays frozen. The framework serializer (`aiko_services…parse`) is
imported LAZILY inside `parse_payload` so importing this module (and thus
`aiko_gateway.main`) never pulls the undeclared, locally-editable `aiko_services` — the
suite's clean-import isolation invariant. `parse_payload` runs only on the bus path
(`aiko/client.py`), where `aiko_services` is present.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

_MESSAGE_COMMAND = "message"  # the Aiko function-call verb aiko_chat uses for a broadcast


@dataclass(frozen=True)
class InboundMessage:
    """A parsed channel payload. `username` is None for legacy bare strings."""
    username: str | None
    channel: str | None
    timestamp: float | None
    message: str
    raw: str


def _coerce_timestamp(value: object) -> float | None:
    """The framework serializer returns every field as a string (netstring decode), so
    a valid ``timestamp`` arrives as e.g. ``"1785575578.53"``. Coerce to float; a
    missing/garbage value degrades to None rather than crashing the never-raises parse."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_payload(payload_in: str, *, fallback_channel: str | None = None) -> InboundMessage:
    """Parse a channel payload into an InboundMessage (never raises).

    Tier 1 — the current Aiko framework S-expression (`generate("message", {...})`).
    Tier 2 — legacy JSON. Tier 3 — a bare string. Newest-first so the live wire wins,
    with the older tiers as forward/backward-compat fallbacks (mirrors
    aiko_chat.protocol._decode_message)."""
    # Tier 1: framework S-expression. Import the serializer LAZILY so importing this
    # module stays free of aiko_services (clean-import isolation invariant); on the real
    # bus path aiko_services is present. A missing dep or a decode failure just falls
    # through to the legacy tiers — the parser is best-effort, never fatal.
    try:
        from aiko_services.main.utilities import parse as _aiko_parse
    except ImportError:
        _aiko_parse = None
    if _aiko_parse is not None:
        try:
            command, fields = _aiko_parse(payload_in)
            # Require the `message` field (as the JSON branch does) so a bodyless call
            # like `(message username: nick)` falls through instead of rendering an
            # empty-bodied structured message.
            if command == _MESSAGE_COMMAND and isinstance(fields, dict) \
                    and "message" in fields:
                return InboundMessage(
                    username=fields.get("username"),
                    channel=fields.get("channel", fallback_channel),
                    timestamp=_coerce_timestamp(fields.get("timestamp")),
                    message=fields["message"],
                    raw=payload_in,
                )
        except (ValueError, IndexError, TypeError):
            pass  # not a well-formed framework call — try the legacy tiers

    # Tier 2: legacy JSON payload from the previous wire format.
    try:
        data = json.loads(payload_in)
    except (TypeError, ValueError):
        # Tier 3: neither S-expr nor JSON — a bare message string.
        return InboundMessage(None, fallback_channel, None, payload_in, payload_in)
    if isinstance(data, dict) and "message" in data:
        return InboundMessage(
            username=data.get("username"),
            channel=data.get("channel", fallback_channel),
            timestamp=_coerce_timestamp(data.get("timestamp")),
            message=data["message"],
            raw=payload_in,
        )
    # JSON, but not a chat payload shape — treat the raw text as the message.
    return InboundMessage(None, fallback_channel, None, payload_in, payload_in)
