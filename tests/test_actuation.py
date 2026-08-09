"""Actuation-envelope trust boundary — sign/verify + durable replay guard (#8).

The physical-safety boundary for the remote-robot loop. Two properties under test:
  * ``verify_actuation`` accepts ONLY an envelope that is well-formed, signed by an
    ALLOWLISTED commander key, cryptographically valid, and fresh — and fails CLOSED on
    every deviation (tampered field, unknown key, bad sig, stale, future).
  * ``SeqHighWater`` accepts a strictly-increasing seq exactly once and rejects replays /
    stale seqs, surviving a restart (persisted) and failing closed on a corrupt store.

EXTERNAL known-answer anchor (not a self-roundtrip): the tests build the signed bytes
with their OWN inline length-prefix construction and sign with ``cryptography``'s
Ed25519 — an independent implementation. If the module's ``actuation_signing_bytes``
layout drifts, ``verify_actuation`` REJECTS the independently-signed envelope, so a
self-consistent-but-wrong codec cannot pass (feedback_self_referential_test_blindness).
"""
from __future__ import annotations

import base64
import struct

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aiko_gateway.domain import actuation
from aiko_gateway.domain.actuation import ActuationError, SeqHighWater, verify_actuation
from aiko_gateway.domain.signing import encode_multikey

# A DETERMINISTIC commander keypair from a fixed 32-byte seed — reproducible vector,
# no randomness in the test.
_SEED = bytes(range(32))
_SK = Ed25519PrivateKey.from_private_bytes(_SEED)
_RAW_PUB = _SK.public_key().public_bytes_raw()
_PUB_MULTIKEY = encode_multikey(_RAW_PUB)
_ALLOW = {_PUB_MULTIKEY}

_NOW = 1_700_000_000_000  # a fixed "now" in ms


def _independent_signing_bytes(*, robot_id: str, command: str, seq: int, signed_at_ms: int) -> bytes:
    """Rebuild the canonical bytes INLINE — independent of actuation_signing_bytes — so
    this is an external anchor on the layout, not a self-roundtrip."""
    def lp(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b
    return b"".join((
        lp(b"aikochat:actuate:v1:EdDSA"),
        lp(_RAW_PUB),
        lp(robot_id.encode()),
        lp(command.encode()),
        struct.pack(">Q", seq),
        struct.pack(">Q", signed_at_ms),
    ))


def _envelope(*, robot_id="arm-1", command="wave", seq=1, signed_at_ms=_NOW, pub=_PUB_MULTIKEY):
    msg = _independent_signing_bytes(
        robot_id=robot_id, command=command, seq=seq, signed_at_ms=signed_at_ms)
    sig = _SK.sign(msg)
    return {
        "v": 1, "alg": "EdDSA", "robot_id": robot_id, "command": command,
        "seq": seq, "signed_at_ms": signed_at_ms, "sender_pubkey": pub,
        "sig": base64.urlsafe_b64encode(sig).rstrip(b"=").decode(),
    }


# ---- happy path + external anchor -------------------------------------------------

def test_valid_allowlisted_signed_fresh_envelope_verifies():
    parsed = verify_actuation(_envelope(), allowed_pubkeys=_ALLOW, now_ms=_NOW)
    assert parsed["command"] == "wave"
    assert parsed["seq"] == 1


def test_module_bytes_match_independent_construction():
    # The external anchor made explicit: the module's layout == the inline one.
    from aiko_gateway.domain.actuation import actuation_signing_bytes
    a = actuation_signing_bytes(raw_pubkey=_RAW_PUB, robot_id="arm-1", command="wave",
                                seq=7, signed_at_ms=_NOW)
    b = _independent_signing_bytes(robot_id="arm-1", command="wave", seq=7, signed_at_ms=_NOW)
    assert a == b


# ---- fail-closed rejections -------------------------------------------------------

def test_unallowlisted_key_rejected_even_if_signature_valid():
    # A DIFFERENT valid keypair signs a perfectly good envelope — must still be rejected:
    # authorization, not just authenticity.
    other = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    other_pub_raw = other.public_key().public_bytes_raw()
    other_pub = encode_multikey(other_pub_raw)
    msg = _independent_signing_bytes(robot_id="arm-1", command="wave", seq=1, signed_at_ms=_NOW)
    # sign with OTHER key over bytes that name OTHER's pubkey (so the sig itself is valid)
    def lp(b): return struct.pack(">I", len(b)) + b
    msg2 = b"".join((lp(b"aikochat:actuate:v1:EdDSA"), lp(other_pub_raw), lp(b"arm-1"),
                     lp(b"wave"), struct.pack(">Q", 1), struct.pack(">Q", _NOW)))
    env = {"v": 1, "alg": "EdDSA", "robot_id": "arm-1", "command": "wave", "seq": 1,
           "signed_at_ms": _NOW, "sender_pubkey": other_pub,
           "sig": base64.urlsafe_b64encode(other.sign(msg2)).rstrip(b"=").decode()}
    with pytest.raises(ActuationError, match="allowlisted"):
        verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW)


def test_tampered_command_rejected():
    env = _envelope(command="wave")
    env["command"] = "terminate"  # sig was over "wave" → reconstructed bytes differ
    with pytest.raises(ActuationError, match="does not verify"):
        verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW)


def test_tampered_seq_rejected():
    env = _envelope(seq=1)
    env["seq"] = 999
    with pytest.raises(ActuationError, match="does not verify"):
        verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW)


def test_stale_envelope_rejected():
    env = _envelope(signed_at_ms=_NOW - 5000)  # 5s old, > 2s default
    with pytest.raises(ActuationError, match="stale"):
        verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW)


def test_future_envelope_rejected():
    env = _envelope(signed_at_ms=_NOW + 5000)
    with pytest.raises(ActuationError, match="future"):
        verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW)


def test_wrong_alg_rejected():
    env = _envelope()
    env["alg"] = "HS256"
    with pytest.raises(ActuationError, match="alg"):
        verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW)


def test_extra_key_rejected():
    env = _envelope()
    env["extra"] = "x"
    with pytest.raises(ActuationError, match="key set"):
        verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW)


# ---- durable seq high-water -------------------------------------------------------

def test_seq_monotonic_accept_then_replay_rejected(tmp_path):
    hw = SeqHighWater(str(tmp_path / "seq.json"))
    assert hw.check_and_advance("arm-1", 1) is True
    assert hw.check_and_advance("arm-1", 2) is True
    assert hw.check_and_advance("arm-1", 2) is False   # exact replay
    assert hw.check_and_advance("arm-1", 1) is False   # older
    assert hw.check_and_advance("arm-1", 3) is True


def test_seq_high_water_survives_restart(tmp_path):
    p = str(tmp_path / "seq.json")
    hw = SeqHighWater(p)
    hw.check_and_advance("arm-1", 5)
    # a fresh instance (bridge restart) must still reject a replay of seq<=5
    hw2 = SeqHighWater(p)
    assert hw2.check_and_advance("arm-1", 5) is False
    assert hw2.check_and_advance("arm-1", 6) is True


def test_seq_per_robot_independent(tmp_path):
    hw = SeqHighWater(str(tmp_path / "seq.json"))
    assert hw.check_and_advance("arm-1", 10) is True
    assert hw.check_and_advance("arm-2", 1) is True     # different robot, own counter
    assert hw.check_and_advance("arm-1", 10) is False


def test_corrupt_store_fails_closed(tmp_path):
    p = tmp_path / "seq.json"
    p.write_text("{ this is not json")
    with pytest.raises(ActuationError, match="corrupt"):
        SeqHighWater(str(p))
