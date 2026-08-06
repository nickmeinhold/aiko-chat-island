"""WSS wire envelopes — the stable client-facing frame contract (plan §A1).

Phase 1 implements the text-message subset: client sends `subscribe` + `send`;
server emits `ack`, `message`, `error`. Reactions/typing/presence/edits/deletes
extend this in later phases. Keeping all wire DTOs here means an aiko protocol
change touches `aiko/payload.py`, never this file — the /v1 contract is frozen.
"""
from __future__ import annotations

import enum
from typing import Any


class ReactionAction(enum.StrEnum):
    """Closed set of reaction-frame actions (#2634). StrEnum (3.12) so the member's
    value IS the wire string — the same closed-set-as-StrEnum idiom the persistence
    layer uses for Role/Platform/etc. Typing the ``reaction_frame`` action against
    this stops a stray ``"added"`` from fanning out with no type rail (cage-match
    Tesla). Not persisted (reactions are STATE rows, the action is a live delta), so
    it lives here at the wire layer, not in models.py with the DB-CHECK enums."""

    ADD = "add"
    REMOVE = "remove"


# -- server -> client builders ----------------------------------------------
def ack(client_msg_id: str, msg_id: str, created_at: str) -> dict:
    return {"type": "ack", "client_msg_id": client_msg_id,
            "msg_id": msg_id, "created_at": created_at}


def message_frame(msg_view: dict) -> dict:
    return {"type": "message", "msg": msg_view}


def retraction_frame(channel_id: str, retraction_id: str, target_msg_id: str) -> dict:
    """Server->client takedown retraction (#7): tells a live subscriber to suppress +
    remove ``target_msg_id``. ``id`` advances the client's forward watermark exactly
    like a message id, so a client that later reconnects and catches up via
    ``get_history`` won't re-request work already applied. Best-effort over the wire —
    a missed/unsubscribed socket self-heals on the next forward catch-up, because the
    Retraction row is the durable system of record this frame merely mirrors. Same
    shape as the history `retraction` item (messages_service.retraction_view)."""
    return {"type": "retraction", "channel_id": channel_id,
            "id": retraction_id, "target_msg_id": target_msg_id}


def reaction_frame(
    channel_id: str, msg_id: str, emoji: str, action: ReactionAction,
    user_id: str, count: int,
) -> dict:
    """Server->client discrete reaction event (#2634): tells a live subscriber that
    ``user_id`` added/removed ``emoji`` on ``msg_id``, with the resulting server-truth
    ``count`` for that emoji. ``action`` is a ``ReactionAction`` (its value — ``"add"``
    | ``"remove"`` — rides the wire).

    Discrete on PURPOSE — NOT a re-serialised message. A reaction is STATE the
    history read recomputes (state-not-event, see the MessageReaction model), so a
    live client applies this delta and an offline one self-heals on the next re-page;
    there is no forward-ULID advance and no `retraction`-style catch-up row.

    `reacted_by_me` is deliberately ABSENT — it is viewer-dependent and this one frame
    fans out to every subscriber, so each client derives it locally: the reaction is
    mine iff ``user_id`` is my own id. Carrying a per-viewer flag on a broadcast frame
    would be wrong for every recipient but the actor."""
    return {"type": "reaction", "channel_id": channel_id, "msg_id": msg_id,
            "emoji": emoji, "action": action, "user_id": user_id, "count": count}


def suback(channel_fences: dict[str, str]) -> dict:
    """Subscription-ack: confirms a `subscribe` and carries each channel's live/
    history *fence* — the newest persisted id at subscribe time (``""`` if the
    channel is empty). The client partitions on it: ``id <= fence`` is fetched
    from history (REST), ``id > fence`` arrives live (WS). See design 04 §Gap 2."""
    return {"type": "suback", "channel_fences": channel_fences}


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
        # `origin` (sovereign-signing envelope) is passed through raw; its deep
        # trust-boundary validation lives in domain/signing.validate_origin, called
        # in the send handler where the authenticated identity is in scope. Absent
        # is legal (unsigned message).
        return {"type": "send", "client_msg_id": cmid, "channel_id": cid,
                "body": body, "reply_to": raw.get("reply_to"),
                "origin": raw.get("origin")}
    raise FrameError(f"unknown frame type: {ftype!r}")
