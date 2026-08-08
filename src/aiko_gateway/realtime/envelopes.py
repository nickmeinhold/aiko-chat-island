"""WSS wire envelopes — the stable client-facing frame contract (plan §A1).

The text-message subset (client sends `subscribe` + `send`; server emits `ack`,
`message`, `error`, `suback`), takedown `retraction` frames (#7), and emoji
`reaction` frames (#2634) all ship here; typing/presence/edits extend it later.
Keeping all wire DTOs here means an aiko protocol change touches `aiko/payload.py`,
never this file — the /v1 contract is frozen.
"""
from __future__ import annotations

import enum
from typing import Any


class ReactionAction(enum.StrEnum):
    """Closed set of reaction-frame actions (#2634). StrEnum (3.12) so the member's
    value IS the wire string — the same closed-set-as-StrEnum idiom the persistence
    layer uses for Role/Platform/etc. Typing the ``reaction_frame`` action against
    this stops a stray ``"added"`` from fanning out with no type rail. Not persisted
    (reactions are STATE rows; the action is a live delta + a signed field inside the
    reaction's own ``origin``), so it lives here at the wire layer, not in models.py."""

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
    user_id: str, origin: dict | None,
) -> dict:
    """Server->client discrete reaction event (#2634): tells a live subscriber that
    ``user_id`` added/removed ``emoji`` on ``msg_id``, carrying the reactor's signed
    ``origin`` envelope on an ``add`` (present iff the reaction was signed; omitted on
    ``remove`` and for an unsigned add). ``action`` is a ``ReactionAction`` (its value
    — ``"add"`` | ``"remove"`` — rides the wire).

    IDENTITY-DELTA, NO COUNT (the honest fix for the anonymous model's count-oracle).
    The frame names the REACTOR and the ACTION; it carries NO server-computed count.
    A client applies it as a SET membership change on its own filtered aggregate —
    ``add`` inserts ``user_id`` into that emoji's reactor set, ``remove`` drops it —
    and derives the count as the set size. Set semantics make a REPEATED same-action
    frame idempotent (a duplicated ``add`` is a no-op). It does NOT make the live path
    fully convergent, and this frame does not claim to be — it is a BEST-EFFORT live
    hint whose authoritative reconciliation is the history re-page (state-not-event):
      * ``add`` and ``remove`` are NOT commutative, so a REORDERED add/remove for the
        same (user, emoji) can transiently leave the live set disagreeing with the DB;
      * a signed UPGRADE of an unsigned row (``add_reaction`` NULL→signed) does not
        re-strike a frame, so a peer that saw the unsigned ``add`` keeps an origin-less
        delta until it re-pages.
    Both self-heal on the next history read of that message row — the durable aggregate
    is truth, this frame only optimises latency over it.

    A broadcast frame needs no absolute count: an anonymous global count on a fan-out
    frame would disagree with each recipient's block-filtered history count and leak
    that a hidden user reacted (the count oracle). Here every path — history aggregate,
    this frame's delivery — is filtered by the SAME block predicate: a subscriber in a
    block relationship with the reactor (or the message author) never receives this
    frame (route-level fanout exclusion), so their count never moves for someone they
    can't see. One predicate, all paths; no oracle by construction.

    Discrete on PURPOSE — NOT a re-serialised message. A reaction is STATE the history
    read recomputes (state-not-event, see the MessageReaction model), so a live client
    applies this delta and an offline one self-heals on the next re-page; there is no
    forward-ULID advance and no ``retraction``-style catch-up row.

    ``reacted_by_me`` is deliberately ABSENT — it is viewer-dependent and this one
    frame fans out to every eligible subscriber, so each client derives it locally: the
    reaction is mine iff ``user_id`` is my own id."""
    frame = {"type": "reaction", "channel_id": channel_id, "msg_id": msg_id,
             "emoji": emoji, "action": action, "user_id": user_id}
    if origin is not None:
        frame["origin"] = origin
    return frame


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
