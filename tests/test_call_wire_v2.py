"""Call wire v2 — the call id in the signed body, carried into the wake as `m`.

Design 12 Decision 1 (client-minted ULID, the island owns no call object); bytes
agreed with the app tab 2026-10-06 on claude-tasks#4421.

THE GOLDEN VECTORS BELOW ARE SHARED WITH `aiko_chat_app`, BYTE FOR BYTE. The two
repos cannot import from each other, so the only thing holding the island's gate
and the app's `isCallInviteBody` / `admitRing` / `admitCallEnd` to the same
language is that both test suites pin the SAME strings. Edit one side and not the
other and the halves disagree about what a call is — silently, because an
unrecognised body is an ordinary message on both. Change them only together.
"""
from __future__ import annotations

import pytest

from aiko_gateway.domain import apns, fcm, push_service
from aiko_gateway.domain.push_result import WakeKind, WakePayload

# ---------------------------------------------------------- golden vectors (shared)

VALID_INVITE = "aiko:call/2 01JABCDEFGHJKMNPQRSTVWXYZ0 · 📞 started a call"
VALID_END = "aiko:call/2 01JABCDEFGHJKMNPQRSTVWXYZ0 · 📞 ended the call"
CALL_ID = "01JABCDEFGHJKMNPQRSTVWXYZ0"

# Each is an ORDINARY MESSAGE, never a call.
REJECT = {
    "lowercase ulid": "aiko:call/2 01jabcdefghjkmnpqrstvwxyz0 · 📞 started a call",
    "overflow lead 8": "aiko:call/2 81JABCDEFGHJKMNPQRSTVWXYZ0 · 📞 started a call",
    "25 chars": "aiko:call/2 01JABCDEFGHJKMNPQRSTVWXYZ · 📞 started a call",
    "excluded letter I": "aiko:call/2 01JABCDEFGHIKMNPQRSTVWXYZ0 · 📞 started a call",
    "trailing content": "aiko:call/2 01JABCDEFGHJKMNPQRSTVWXYZ0 · 📞 started a call!",
    "v1 tail, no space": "aiko:call/201JABCDEFGHJKMNPQRSTVWXYZ0 · 📞 started a call",
}


def test_the_golden_tail_is_v1s_tail_byte_for_byte():
    """The v2 frame reuses v1's human-readable tail so a pre-v2 build renders a
    readable message. Asserted by codepoint, because U+00B7 vs U+2022 or a
    different phone emoji is invisible in review and fatal on the wire."""
    for v2, v1 in ((VALID_INVITE, push_service.CALL_INVITE_BODY),
                   (VALID_END, push_service.CALL_END_BODY)):
        assert v2.split(CALL_ID, 1)[1] == v1.split("aiko:call/1", 1)[1]


def test_valid_vectors_parse_to_their_kind_and_id():
    assert push_service.parse_call_body(VALID_INVITE) == (WakeKind.CALL_INVITE, CALL_ID)
    assert push_service.parse_call_body(VALID_END) == (WakeKind.CALL_END, CALL_ID)


@pytest.mark.parametrize("name", list(REJECT))
def test_reject_vectors_are_ordinary_messages(name):
    """Fullmatch, never search: every one of these would wake under a looser
    test, and each names a distinct way to get it wrong."""
    body = REJECT[name]
    assert push_service.parse_call_body(body) is None, name
    assert push_service.should_wake("dm", body) is None, name


def test_v1_is_history_not_a_call():
    """Calling is v2-only (app design 22 §v2.0; Nick, 2026-10-06). The v1 bodies
    are signed history — stored and served forever — but parse as ordinary
    messages and never wake."""
    assert push_service.parse_call_body(push_service.CALL_INVITE_BODY) is None
    assert push_service.parse_call_body(push_service.CALL_END_BODY) is None


@pytest.mark.parametrize("kind", ["standard", "public", "private", "llm", "robot"])
@pytest.mark.parametrize("body", [VALID_INVITE, VALID_END])
def test_v2_does_not_wake_outside_a_dm(kind, body):
    """The DM check is hoisted above the parser, so v2 inherits it — asserted
    rather than assumed, because a third sentinel family is exactly when a
    per-arm copy of the gate would have forgotten it."""
    assert push_service.should_wake(kind, body) is None


# -------------------------------------------------------------- the wire, both sides

def _payload(kind: WakeKind) -> WakePayload:
    return WakePayload(channel_id="01CHAN", kind=kind, call_id=CALL_ID)


@pytest.mark.parametrize("kind", list(WakeKind))
def test_fcm_carries_m_on_every_wake(kind):
    msg = fcm.build_message("t", _payload(kind))["message"]
    assert msg["data"] == {"c": "01CHAN", "k": kind.value, "m": CALL_ID}
    # No collapse key: the collapsible class caps a device at four keys and
    # throttles, which can lose a call's end (cage-match PR#192). Unused by the
    # app's receiver, which keys on `m` (checked on main and android-ring).
    assert "collapse_key" not in msg["android"]


@pytest.mark.parametrize("kind", list(WakeKind))
def test_apns_carries_m_on_every_wake(kind):
    body = apns._render(_payload(kind))
    assert body["m"] == CALL_ID and body["c"] == "01CHAN" and body["k"] == kind.value


def test_call_id_has_no_default():
    """Same safety argument as `kind`: a forgotten argument must be a TypeError,
    not every v2 call silently shipped v1-shaped."""
    with pytest.raises(TypeError):
        WakePayload(channel_id="01CHAN", kind=WakeKind.CALL_INVITE)  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", ["alice", "01jabcdefghjkmnpqrstvwxyz0",
                                 "81JABCDEFGHJKMNPQRSTVWXYZ0", "", None])
def test_a_payload_refuses_a_malformed_call_id(bad):
    """The shape is enforced at CONSTRUCTION (Carnot, PR#192 r1), so no future
    caller can hand Apple/Google an arbitrary `m` by building the payload
    directly instead of going through `parse_call_body`."""
    with pytest.raises(ValueError):
        WakePayload(channel_id="01CHAN", kind=WakeKind.CALL_INVITE, call_id=bad)
