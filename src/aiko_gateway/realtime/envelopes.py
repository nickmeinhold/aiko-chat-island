"""WSS wire envelopes — the stable client-facing frame contract (plan §A1).

Phase 1 implements the text-message subset: client sends `subscribe` + `send`;
server emits `ack`, `message`, `error`. Reactions/typing/presence/edits/deletes
extend this in later phases. Keeping all wire DTOs here means an aiko protocol
change touches `aiko/payload.py`, never this file — the /v1 contract is frozen.
"""
from __future__ import annotations

from typing import Any


# -- server -> client builders ----------------------------------------------
def ack(client_msg_id: str, msg_id: str, created_at: str) -> dict:
    return {"type": "ack", "client_msg_id": client_msg_id,
            "msg_id": msg_id, "created_at": created_at}


def message_frame(msg_view: dict) -> dict:
    return {"type": "message", "msg": msg_view}


def error(code: str, detail: str, ref_client_msg_id: str | None = None) -> dict:
    return {"type": "error", "code": code, "detail": detail,
            "ref_client_msg_id": ref_client_msg_id}


# -- client -> server parsing -------------------------------------------------
class FrameError(ValueError):
    """Malformed inbound frame; caller replies with an `error` envelope."""


def parse_inbound(raw: Any) -> dict:
    """Validate an inbound frame to a normalised dict, or raise FrameError."""
    if not isinstance(raw, dict):
        raise FrameError("frame must be a JSON object")
    ftype = raw.get("type")
    if ftype == "subscribe":
        ids = raw.get("channel_ids")
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            raise FrameError("subscribe.channel_ids must be a list[str]")
        return {"type": "subscribe", "channel_ids": ids}
    if ftype == "send":
        cmid, cid, body = raw.get("client_msg_id"), raw.get("channel_id"), raw.get("body")
        if not (isinstance(cmid, str) and isinstance(cid, str) and isinstance(body, str)):
            raise FrameError("send requires client_msg_id, channel_id, body (str)")
        if not body.strip():
            raise FrameError("send.body must be non-empty")
        return {"type": "send", "client_msg_id": cmid, "channel_id": cid,
                "body": body, "reply_to": raw.get("reply_to")}
    raise FrameError(f"unknown frame type: {ftype!r}")
