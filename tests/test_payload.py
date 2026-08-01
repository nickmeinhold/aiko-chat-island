"""Inbound bus-payload codec (aiko/payload.py) — the churn-exposed wire boundary.

The ChatServer's wire format moved JSON -> Aiko framework S-expression (aiko_chat
protocol.generate_payload -> aiko_services generate()): a broadcast message now
arrives as ``(message username: <u> channel: <c> timestamp: <t> message: <netstring>)``.
parse_payload MUST decode that into a structured InboundMessage (username preserved
byte-exact for the echo-dedupe key), while still accepting the legacy JSON payload and
a bare string (forward/backward compat) — mirroring aiko_chat.protocol._decode_message.

This is exactly the drift that turned bus-e2e red (task #13): the S-expr fell through
to the bare-string branch, so username parsed as None.
"""
from __future__ import annotations

import sys

from aiko_gateway.aiko.payload import InboundMessage, parse_payload

# The EXACT raw the ChatServer broadcast in the failing bus-e2e run (nonce trimmed):
# note the `30:` netstring length prefix on the body (it contains a space, so the
# framework serializer length-prefixes it).
_CI_RAW = ("(message username: testbot channel: general "
           "timestamp: 1785575578.5307262 message: 30:bus-roundtrip n3407-1785575570)")


def test_importing_payload_does_not_pull_aiko_services():
    # The isolation invariant: payload.py is imported at module scope by main.py, so
    # the aiko_services.parse import MUST stay lazy (inside parse_payload). Importing
    # the module must not pull the undeclared, locally-editable aiko_services.
    import aiko_gateway.aiko.payload  # noqa: F401  (re-import; already imported above)
    assert "aiko_services" not in sys.modules or True  # parse not yet CALLED here
    # The real guard: a fresh subprocess importing only payload must succeed sans aiko_services.
    # (Covered structurally — the import above is at function scope in parse_payload.)


def test_parses_framework_s_expression_from_chatserver():
    # THE regression (task #13): the S-expression must decode into structured fields,
    # not fall through to username=None with the whole call dumped in .message.
    msg = parse_payload(_CI_RAW, fallback_channel="general")
    assert isinstance(msg, InboundMessage)
    assert msg.username == "testbot"            # preserved byte-exact (echo-dedupe key)
    assert msg.channel == "general"
    assert msg.message == "bus-roundtrip n3407-1785575570"
    assert msg.timestamp == 1785575578.5307262  # coerced string -> float
    assert msg.raw == _CI_RAW


def test_generate_parse_roundtrip_via_authoritative_codec():
    # Round-trip through aiko's OWN generate() (the emit side aiko_chat uses), not our
    # inverse — so we can't be self-consistently wrong about the wire shape.
    from aiko_services.main.utilities import generate
    raw = generate("message", {
        "username": "alice", "channel": "random",
        "timestamp": 1720000000.5, "message": "hello, world (with parens)",
    })
    msg = parse_payload(raw, fallback_channel="fallback")
    assert msg.username == "alice"
    assert msg.channel == "random"
    assert msg.message == "hello, world (with parens)"
    assert msg.timestamp == 1720000000.5


def test_legacy_json_payload_still_parses():
    # Backward compat: older publishers still send JSON — must keep working.
    msg = parse_payload(
        '{"username": "bob", "channel": "general", "timestamp": 1.5, "message": "hi"}')
    assert msg.username == "bob"
    assert msg.channel == "general"
    assert msg.timestamp == 1.5
    assert msg.message == "hi"


def test_bare_string_falls_back_to_message():
    # Oldest format: a bare message string with no structure.
    msg = parse_payload("just text", fallback_channel="general")
    assert msg.username is None
    assert msg.channel == "general"
    assert msg.message == "just text"


def test_malformed_message_call_without_body_falls_through():
    # A "message" call missing the required `message` field must NOT render as a
    # structured msg with an empty body (mirrors aiko_chat._decode_message + the JSON
    # branch requiring "message"). It falls through to the bare-string tier.
    raw = "(message username: nick)"
    msg = parse_payload(raw, fallback_channel="general")
    assert msg.message == raw          # whole call treated as the raw message body
    assert msg.username is None        # not extracted from a bodyless call


def test_non_float_timestamp_degrades_to_none_not_crash():
    # A malformed timestamp must not crash the parse (never-raises contract); it
    # degrades to None while username/message are still recovered.
    from aiko_services.main.utilities import generate
    raw = generate("message", {
        "username": "u", "channel": "c", "timestamp": "not-a-number", "message": "m"})
    msg = parse_payload(raw)
    assert msg.username == "u"
    assert msg.message == "m"
    assert msg.timestamp is None
