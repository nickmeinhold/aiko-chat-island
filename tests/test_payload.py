"""Inbound bus-payload codec (aiko/payload.py) — the churn-exposed wire boundary.

The ChatServer's wire format moved JSON -> Aiko framework S-expression (aiko_chat
protocol.generate_payload -> aiko_services generate()): a broadcast message now
arrives as ``(message username: <u> channel: <c> timestamp: <t> message: <netstring>)``.
parse_payload MUST decode that into a structured InboundMessage (username preserved
byte-exact for the echo-dedupe key), while still accepting the legacy JSON payload and
a bare string (forward/backward compat) — mirroring aiko_chat.protocol._decode_message.

This is exactly the drift that turned bus-e2e red (task #13): the S-expr fell through
to the bare-string branch, so username parsed as None.

ISOLATION INVARIANT: the fast `test` CI job runs `pytest tests/` WITHOUT the
locally-editable, undeclared `aiko_services` installed. Tests that need the framework
serializer are guarded with ``pytest.importorskip`` so they SKIP there (and run in dev
+ the bus-e2e job, where aiko_services is present) — a test must never smuggle the
banned dependency into the clean-import suite.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from aiko_gateway.aiko.payload import InboundMessage, parse_payload

# The EXACT raw the ChatServer broadcast in the failing bus-e2e run (nonce trimmed):
# note the `30:` netstring length prefix on the body (it contains a space, so the
# framework serializer length-prefixes it).
_CI_RAW = ("(message username: testbot channel: general "
           "timestamp: 1785575578.5307262 message: 30:bus-roundtrip n3407-1785575570)")


def test_importing_payload_does_not_pull_aiko_services():
    # A REAL guard (not the old tautological `assert ... or True`): a FRESH interpreter
    # importing only the payload module must NOT end up with aiko_services in
    # sys.modules — the lazy import inside parse_payload keeps module import clean. A
    # subprocess so it's a genuine cold import, immune to this suite having imported
    # aiko_services elsewhere. Fails loudly if someone hoists `from aiko_services...`
    # to module scope (the regression that would silently re-break the clean-import job).
    code = (
        "import sys, aiko_gateway.aiko.payload; "
        "assert 'aiko_services' not in sys.modules, "
        "'importing payload pulled aiko_services — clean-import isolation invariant broken'"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_parses_framework_s_expression_from_chatserver():
    # THE regression (task #13): the S-expression must decode into structured fields,
    # not fall through to username=None with the whole call dumped in .message.
    pytest.importorskip("aiko_services.main.utilities")  # tier-1 needs the serializer
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
    generate = pytest.importorskip("aiko_services.main.utilities").generate
    raw = generate("message", {
        "username": "alice", "channel": "random",
        "timestamp": 1720000000.5, "message": "hello, world (with parens)",
    })
    msg = parse_payload(raw, fallback_channel="fallback")
    assert msg.username == "alice"
    assert msg.channel == "random"
    assert msg.message == "hello, world (with parens)"
    assert msg.timestamp == 1720000000.5


def test_non_float_timestamp_degrades_to_none_not_crash():
    # A malformed timestamp must not crash the parse (never-raises contract); it
    # degrades to None while username/message are still recovered.
    generate = pytest.importorskip("aiko_services.main.utilities").generate
    raw = generate("message", {
        "username": "u", "channel": "c", "timestamp": "not-a-number", "message": "m"})
    msg = parse_payload(raw)
    assert msg.username == "u"
    assert msg.message == "m"
    assert msg.timestamp is None


# --- tiers that need NO aiko_services (run in the clean-import job too) -------- #

def test_legacy_json_payload_still_parses():
    # Backward compat: older publishers still send JSON — must keep working.
    msg = parse_payload(
        '{"username": "bob", "channel": "general", "timestamp": 1.5, "message": "hi"}')
    assert msg.username == "bob"
    assert msg.channel == "general"
    assert msg.timestamp == 1.5
    assert msg.message == "hi"


def test_json_non_string_message_falls_through():
    # A JSON object whose `message` is not a string is not a usable chat payload — it
    # falls through to the bare-string tier rather than violating InboundMessage's
    # str `message` contract (Carnot/Tesla: pin the type at the shared door).
    raw = '{"username": "bob", "message": 123}'
    msg = parse_payload(raw, fallback_channel="general")
    assert msg.message == raw
    assert msg.username is None


def test_json_non_string_username_pinned_to_none():
    # username is the echo-dedupe key — a non-string username must not become the key;
    # it pins to None (the body still parses).
    raw = '{"username": 42, "message": "hi"}'
    msg = parse_payload(raw)
    assert msg.username is None
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
    # branch requiring "message"). Falls through to the bare-string tier — the same
    # outcome whether or not aiko_services is present (tier-1 rejects it or is skipped).
    raw = "(message username: nick)"
    msg = parse_payload(raw, fallback_channel="general")
    assert msg.message == raw          # whole call treated as the raw message body
    assert msg.username is None        # not extracted from a bodyless call


def test_wrong_command_sexpr_not_misrouted():
    # A framework call whose verb is NOT "message" must not cross-talk as a chat
    # payload — it falls through to JSON/bare (Tesla: the dual-format ambiguity fixture).
    # Same outcome with or without aiko_services (tier-1 rejects the command or is skipped).
    raw = "(notmessage username: x message: y)"
    msg = parse_payload(raw, fallback_channel="general")
    assert msg.message == raw
    assert msg.username is None
