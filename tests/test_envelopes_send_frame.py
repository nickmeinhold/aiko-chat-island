"""Inbound `send` frame parsing (#2632 mentions pass-through).

`parse_inbound` is intentionally SHALLOW on `mentions` (and `origin`): it passes
the raw value through so the deep, fail-closed validation runs at the trust
boundary in the send handler where the authenticated identity is in scope
(domain/mentions.validate_mentions). These pin that pass-through contract — the
value reaches the handler unchanged, and its ABSENCE is legal (None), never a
parse error.
"""
from __future__ import annotations

import pytest

from aiko_gateway.realtime import envelopes


def _base(**extra) -> dict:
    return {"type": "send", "client_msg_id": "m1", "channel_id": "c1",
            "body": "hi @bob", **extra}


def test_send_frame_passes_mentions_through_verbatim():
    spans = [{"target_type": "user", "target_id": "bob-key", "offset": 3, "length": 4}]
    out = envelopes.parse_inbound(_base(mentions=spans))
    assert out["mentions"] == spans  # untouched; validated later at the trust boundary


def test_send_frame_mentions_absent_is_none_not_error():
    out = envelopes.parse_inbound(_base())
    assert out["mentions"] is None


def test_send_frame_does_not_validate_mentions_shape():
    """Garbage `mentions` still PARSES (it's rejected later, fail-closed, by the
    handler) — parse_inbound must not couple to the mention schema."""
    out = envelopes.parse_inbound(_base(mentions="not-a-list"))
    assert out["mentions"] == "not-a-list"


def test_send_frame_still_requires_core_fields():
    with pytest.raises(envelopes.FrameError):
        envelopes.parse_inbound({"type": "send", "client_msg_id": "m1",
                                 "mentions": []})  # missing channel_id/body
