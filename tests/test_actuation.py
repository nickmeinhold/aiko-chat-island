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
from aiko_gateway.domain.actuation import (
    ActuationError,
    SeqHighWater,
    verify_actuation,
    verify_and_advance,
)
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
    assert hw._check_and_advance("arm-1", 1) is True
    assert hw._check_and_advance("arm-1", 2) is True
    assert hw._check_and_advance("arm-1", 2) is False   # exact replay
    assert hw._check_and_advance("arm-1", 1) is False   # older
    assert hw._check_and_advance("arm-1", 3) is True


def test_seq_high_water_survives_restart(tmp_path):
    p = str(tmp_path / "seq.json")
    hw = SeqHighWater(p)
    hw._check_and_advance("arm-1", 5)
    # a fresh instance (bridge restart) must still reject a replay of seq<=5
    hw2 = SeqHighWater(p)
    assert hw2._check_and_advance("arm-1", 5) is False
    assert hw2._check_and_advance("arm-1", 6) is True


def test_seq_per_robot_independent(tmp_path):
    hw = SeqHighWater(str(tmp_path / "seq.json"))
    assert hw._check_and_advance("arm-1", 10) is True
    assert hw._check_and_advance("arm-2", 1) is True     # different robot, own counter
    assert hw._check_and_advance("arm-1", 10) is False


def test_corrupt_store_fails_closed(tmp_path):
    p = tmp_path / "seq.json"
    p.write_text("{ this is not json")
    with pytest.raises(ActuationError, match="corrupt"):
        SeqHighWater(str(p))


# ---- store: valid-JSON-but-wrong-shape must ALSO fail closed (the untested mirror) ----
# The old guard only caught JSONDecodeError, so a parseable-but-wrong store silently reset
# the high-water to empty — reopening the exact replay window the store exists to close.

@pytest.mark.parametrize("body", ["[]", "null", "42", '"oops"', "[1, 2, 3]"])
def test_store_valid_json_non_object_fails_closed(tmp_path, body):
    p = tmp_path / "seq.json"
    p.write_text(body)
    with pytest.raises(ActuationError, match="not a JSON object"):
        SeqHighWater(str(p))


@pytest.mark.parametrize("body", [
    '{"arm-1": "5"}',      # string value — must not be silently dropped
    '{"arm-1": true}',     # JSON true must NOT decay to int 1
    '{"arm-1": 5.0}',      # float is not an int seq
    '{"arm-1": -1}',       # negative out of u64 range
    '{"arm-1": 10, "arm-2": "bad"}',  # partial drift: one good, one bad → reject WHOLE file
])
def test_store_wrong_typed_value_fails_closed(tmp_path, body):
    p = tmp_path / "seq.json"
    p.write_text(body)
    with pytest.raises(ActuationError, match="non-u64-int mark"):
        SeqHighWater(str(p))


def test_store_empty_object_is_valid(tmp_path):
    # An empty dict is a legitimate fresh store — not corruption.
    p = tmp_path / "seq.json"
    p.write_text("{}")
    hw = SeqHighWater(str(p))
    assert hw._check_and_advance("arm-1", 1) is True


# ---- exception contract: every wire-parse failure surfaces as ActuationError ----

def test_malformed_pubkey_raises_actuation_error():
    env = _envelope()
    env["sender_pubkey"] = "not-a-multikey"  # decode_multikey raises OriginError internally
    with pytest.raises(ActuationError, match="sender_pubkey malformed"):
        verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW)


def test_malformed_sig_raises_actuation_error():
    env = _envelope()
    env["sig"] = "!!! not base64url !!!"  # b64url_raw raises OriginError internally
    with pytest.raises(ActuationError, match="sig malformed"):
        verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW)


# ---- freshness edges ----

def test_freshness_exactly_at_max_age_accepted():
    env = _envelope(signed_at_ms=_NOW - actuation.DEFAULT_MAX_AGE_MS)  # age == max_age
    parsed = verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW)
    assert parsed["seq"] == 1


def test_freshness_one_ms_past_max_age_rejected():
    env = _envelope(signed_at_ms=_NOW - actuation.DEFAULT_MAX_AGE_MS - 1)
    with pytest.raises(ActuationError, match="stale"):
        verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW)


def test_freshness_at_max_forward_skew_accepted():
    env = _envelope(signed_at_ms=_NOW + actuation._MAX_FWD_SKEW_MS)  # age == -skew (boundary)
    parsed = verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW)
    assert parsed["seq"] == 1


def test_freshness_one_ms_past_forward_skew_rejected():
    env = _envelope(signed_at_ms=_NOW + actuation._MAX_FWD_SKEW_MS + 1)
    with pytest.raises(ActuationError, match="future"):
        verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW)


# ---- identity binding ----

def test_expected_robot_id_match_accepted():
    env = _envelope(robot_id="arm-1")
    parsed = verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW, expected_robot_id="arm-1")
    assert parsed["robot_id"] == "arm-1"


def test_expected_robot_id_mismatch_rejected():
    # A validly-signed command for arm-2 must be rejected by a bridge that owns arm-1.
    env = _envelope(robot_id="arm-2")
    with pytest.raises(ActuationError, match="does not match"):
        verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW, expected_robot_id="arm-1")


# ---- the sealed door: verify_and_advance ----

def test_verify_and_advance_happy_path(tmp_path):
    hw = SeqHighWater(str(tmp_path / "seq.json"))
    parsed = verify_and_advance(_envelope(seq=1), allowed_pubkeys=_ALLOW, seq_store=hw, now_ms=_NOW)
    assert parsed["command"] == "wave"


def test_verify_and_advance_replay_raises(tmp_path):
    hw = SeqHighWater(str(tmp_path / "seq.json"))
    verify_and_advance(_envelope(seq=5), allowed_pubkeys=_ALLOW, seq_store=hw, now_ms=_NOW)
    # a fresh, validly-signed envelope reusing seq=5 is a replay → ActuationError, not False
    with pytest.raises(ActuationError, match="replay or stale"):
        verify_and_advance(_envelope(seq=5), allowed_pubkeys=_ALLOW, seq_store=hw, now_ms=_NOW)


def test_verify_and_advance_does_not_advance_on_bad_signature(tmp_path):
    # The catastrophic order made impossible: an UNVERIFIED envelope must NOT touch the
    # high-water, so a later legitimate low seq still works (the DoS wedge cannot happen).
    hw = SeqHighWater(str(tmp_path / "seq.json"))
    forged = _envelope(seq=2**64 - 1)
    forged["command"] = "terminate"  # breaks the signature
    with pytest.raises(ActuationError, match="does not verify"):
        verify_and_advance(forged, allowed_pubkeys=_ALLOW, seq_store=hw, now_ms=_NOW)
    # high-water untouched → a normal seq=1 still actuates
    assert verify_and_advance(_envelope(seq=1), allowed_pubkeys=_ALLOW, seq_store=hw, now_ms=_NOW)["seq"] == 1


def test_verify_and_advance_unallowlisted_key_does_not_advance(tmp_path):
    hw = SeqHighWater(str(tmp_path / "seq.json"))
    other = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    other_pub_raw = other.public_key().public_bytes_raw()
    other_pub = encode_multikey(other_pub_raw)

    def lp(b):
        return struct.pack(">I", len(b)) + b
    msg = b"".join((lp(b"aikochat:actuate:v1:EdDSA"), lp(other_pub_raw), lp(b"arm-1"),
                    lp(b"wave"), struct.pack(">Q", 2**64 - 1), struct.pack(">Q", _NOW)))
    env = {"v": 1, "alg": "EdDSA", "robot_id": "arm-1", "command": "wave", "seq": 2**64 - 1,
           "signed_at_ms": _NOW, "sender_pubkey": other_pub,
           "sig": base64.urlsafe_b64encode(other.sign(msg)).rstrip(b"=").decode()}
    with pytest.raises(ActuationError, match="allowlisted"):
        verify_and_advance(env, allowed_pubkeys=_ALLOW, seq_store=hw, now_ms=_NOW)
    # store never advanced by an unauthorized max-seq envelope
    assert verify_and_advance(_envelope(seq=1), allowed_pubkeys=_ALLOW, seq_store=hw, now_ms=_NOW)["seq"] == 1


def test_concurrent_check_and_advance_admits_one(tmp_path):
    # Under concurrent callers with the SAME next seq, exactly one may win — the lock
    # serializes the read-modify-write so two handlers can't both actuate.
    import threading
    hw = SeqHighWater(str(tmp_path / "seq.json"))
    hw._check_and_advance("arm-1", 0)  # high-water = 0
    results = []
    barrier = threading.Barrier(8)

    def race():
        barrier.wait()
        results.append(hw._check_and_advance("arm-1", 1))

    threads = [threading.Thread(target=race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1  # exactly one winner
    assert results.count(False) == 7


# ---- max_age_ms is a bounded floor, not a caller-optional footgun ----

def test_max_age_ms_above_ceiling_rejected():
    # A bridge cannot widen the freshness window into the crucible-rejected minutes range.
    env = _envelope()
    with pytest.raises(ActuationError, match="freshness floor is not caller-optional"):
        verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW, max_age_ms=300_000)


def test_max_age_ms_negative_rejected():
    env = _envelope()
    with pytest.raises(ActuationError, match="freshness floor"):
        verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW, max_age_ms=-1)


def test_max_age_ms_at_ceiling_accepted():
    # Exactly at the ceiling is allowed; an envelope within that window verifies.
    env = _envelope(signed_at_ms=_NOW - 10_000)
    parsed = verify_actuation(env, allowed_pubkeys=_ALLOW, now_ms=_NOW,
                              max_age_ms=actuation._MAX_ALLOWED_AGE_MS)
    assert parsed["seq"] == 1


# ---- the private advance primitive still validates (store-poisoning defense) ----

def test_private_advance_rejects_bool_seq(tmp_path):
    # bool is an int subclass — a persisted JSON `true` would wedge the next restart.
    hw = SeqHighWater(str(tmp_path / "seq.json"))
    with pytest.raises(ActuationError, match="u64-range"):
        hw._check_and_advance("arm-1", True)


def test_private_advance_rejects_oversized_seq(tmp_path):
    hw = SeqHighWater(str(tmp_path / "seq.json"))
    with pytest.raises(ActuationError, match="u64-range"):
        hw._check_and_advance("arm-1", 1 << 64)


def test_private_advance_rejects_empty_robot_id(tmp_path):
    hw = SeqHighWater(str(tmp_path / "seq.json"))
    with pytest.raises(ActuationError, match="robot_id"):
        hw._check_and_advance("", 1)


def test_persist_failure_surfaces_as_actuation_error(tmp_path):
    # A store in a read-only directory can't persist — the failure must surface as
    # ActuationError (not a raw OSError) so the caller's fail-closed handler catches it.
    import os
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("running as root — directory mode does not block writes")
    d = tmp_path / "ro"
    d.mkdir()
    hw = SeqHighWater(str(d / "seq.json"))
    os.chmod(d, 0o500)  # read+execute, no write
    try:
        with pytest.raises(ActuationError, match="persist failed"):
            hw._check_and_advance("arm-1", 1)
    finally:
        os.chmod(d, 0o700)  # restore so tmp cleanup can remove it
