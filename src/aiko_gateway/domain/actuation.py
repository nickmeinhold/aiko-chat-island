"""Signed physical-actuation envelopes — the robot loop's trust boundary (task #8).

A remote robot performs a PHYSICAL action (an arm waves) only when it receives an
envelope that VERIFIES: an Ed25519 signature, by an ALLOWLISTED commander key, over
canonical domain-separated bytes, that is FRESH and NOT a replay. The transport (a
LiveKit data channel) only relays bytes — a malicious room participant can inject
anything — so the signature, not room membership, is what authorizes the servo.

This module is the CANONICAL crypto both sides use:
  * the commander (location A) builds ``actuation_signing_bytes`` and signs them;
  * the bridge (at the robot) calls ``verify_actuation`` before it actuates.

It is **actuator-agnostic**: it signs ``{robot_id, command, seq, signed_at_ms}`` and
neither knows nor cares whether ``command="wave"`` drives a dog, an arm joint, or a
servo — so a change of robot hardware never touches this boundary.

Distinct from ``signing.py`` (message/reaction carriage), which validates envelope
SHAPE but NEVER checks a signature — the gateway carries those, it doesn't verify
them. Here verification is the whole point and the signature IS load-bearing, so this
does real Ed25519 verification (net-new) and fails CLOSED on anything unproven.

Two defenses, both required, because each covers a gap the other can't:
  * **Ed25519 sig + commander allowlist** — authenticity + authorization. Only the
    one shipped commander key can command the robot; "anyone in the room" cannot.
  * **Durable monotonic ``seq`` high-water** (``SeqHighWater``) — replay + staleness in
    ONE clock-independent mechanism: an envelope with ``seq <= high_water`` is dropped,
    so a captured packet can't be re-fired and a redelivered backlog can't actuate
    late. Survives a bridge restart (persisted to disk).
  * **Freshness deadline** (``max_age_ms``, default a TIGHT 2s) — a SEPARATE timeliness
    gate, only trustworthy under asserted NTP discipline; it is defense-in-depth on top
    of the seq guard, never the primary replay defense (cross-host clocks disagree).
"""
from __future__ import annotations

import base64
import json
import os
import struct
import tempfile
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .signing import (
    MAX_PUBKEY_STR,
    MAX_SIGNED_AT_MS,
    SIG_RAW_LEN,
    b64url_raw,
    decode_multikey,
)

# DISTINCT domain tag so an actuation signature can never be lifted from — or
# re-presented as — a message or reaction signature (cross-event replay). Same
# length-prefixed construction as signing.reaction_signing_bytes; only the tag and
# the trailing fields differ.
ACTUATE_DOMAIN_TAG = "aikochat:actuate:v1:EdDSA"
ALG = "EdDSA"                       # the ONLY accepted alg — allowlist, never trust the envelope's claim
SUPPORTED_V = 1

_MAX_ROBOT_ID = 128                 # a robot identity string cap (untrusted wire input)
_MAX_COMMAND = 64                   # a command token cap; the bridge additionally maps it through a CLOSED table
_MAX_SIG_STR = 128
# The bridge re-checks the deadline at DEQUEUE (immediately before actuation). Tight by
# default — a servo command that aged in the mailbox is stale. Explicit, NEVER inherit a
# minutes-long default (a 5-min window on a physical device is the crucible's rejected footgun).
DEFAULT_MAX_AGE_MS = 2000

# Exactly these keys, no more (frozen v1 shape; a change is a v2, never a silent add).
_REQUIRED_KEYS = frozenset(
    {"v", "alg", "robot_id", "command", "seq", "signed_at_ms", "sender_pubkey", "sig"}
)


class ActuationError(ValueError):
    """A malformed, unauthorized, stale, or unverifiable actuation envelope. The
    bridge MUST NOT actuate — fail closed, drop the packet, log visibly."""


def actuation_signing_bytes(
    *, raw_pubkey: bytes, robot_id: str, command: str, seq: int, signed_at_ms: int
) -> bytes:
    """The canonical, length-prefixed, domain-separated bytes an actuation signature
    is computed over — a faithful mirror of ``signing.reaction_signing_bytes`` with the
    actuation content fields. Every variable-length field is preceded by a big-endian
    u32 length; ``seq`` and ``signed_at_ms`` are fixed-width big-endian u64 (no length
    prefix). Both signer (commander) and verifier (bridge) MUST produce identical bytes;
    the external known-answer vector test pins this so the two can never silently drift.
    """
    def lp(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b

    return b"".join((
        lp(ACTUATE_DOMAIN_TAG.encode()),
        lp(raw_pubkey),
        lp(robot_id.encode()),
        lp(command.encode()),
        struct.pack(">Q", seq),
        struct.pack(">Q", signed_at_ms),
    ))


def verify_actuation(
    raw: Any,
    *,
    allowed_pubkeys: set[str],
    now_ms: int,
    max_age_ms: int = DEFAULT_MAX_AGE_MS,
) -> dict:
    """Verify an inbound actuation envelope at the robot's trust boundary and return
    the validated dict, or raise ``ActuationError``. Fail-closed at every step, in this
    order (cheapest / most-decisive first):

      1. exactly the frozen v1 key set (no unknown/missing keys);
      2. ``alg`` allowlisted to EdDSA (never trust the envelope's claimed alg);
      3. ``sender_pubkey`` a well-formed ed25519 Multikey **and IN ``allowed_pubkeys``**
         (authorization — an unknown key is rejected before any crypto work);
      4. ``sig`` unpadded base64url decoding to exactly 64 bytes;
      5. the **Ed25519 signature verifies** over ``actuation_signing_bytes`` (authenticity —
         the load-bearing check this whole feature exists for);
      6. **freshness**: ``signed_at_ms`` within ``[now_ms - max_age_ms, now_ms + skew]``.

    Does NOT check the ``seq`` high-water — that is stateful and belongs to
    ``SeqHighWater.check_and_advance`` at the call site, so verification stays pure and
    the durable replay guard is a single explicit gate. The caller MUST run BOTH.
    """
    if not isinstance(raw, dict):
        raise ActuationError("actuation envelope must be a JSON object")
    keys = set(raw.keys())
    if keys != _REQUIRED_KEYS:
        missing = _REQUIRED_KEYS - keys
        extra = keys - _REQUIRED_KEYS
        raise ActuationError(f"key set invalid (missing={sorted(missing)}, unexpected={sorted(extra)})")

    # bool is an int subclass — exclude it wherever an int is expected so JSON true/false
    # can't satisfy `== 1`. `v` is the frozen discriminator, so it gets the guard too.
    if isinstance(raw["v"], bool) or not isinstance(raw["v"], int) or raw["v"] != SUPPORTED_V:
        raise ActuationError(f"v {raw['v']!r} unsupported (expected int {SUPPORTED_V})")
    if raw["alg"] != ALG:
        raise ActuationError(f"alg {raw['alg']!r} not allowed (only {ALG!r})")

    robot_id = raw["robot_id"]
    if not isinstance(robot_id, str) or not robot_id or len(robot_id) > _MAX_ROBOT_ID:
        raise ActuationError("robot_id must be a non-empty string within the size cap")

    command = raw["command"]
    if not isinstance(command, str) or not command or len(command) > _MAX_COMMAND:
        raise ActuationError("command must be a non-empty string within the size cap")

    seq = raw["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0 or seq > (1 << 64) - 1:
        raise ActuationError("seq must be a u64-range non-negative integer")

    ts = raw["signed_at_ms"]
    if isinstance(ts, bool) or not isinstance(ts, int) or ts < 0 or ts > MAX_SIGNED_AT_MS:
        raise ActuationError("signed_at_ms must be a sane non-negative integer")

    pubkey_str = raw["sender_pubkey"]
    if not isinstance(pubkey_str, str) or len(pubkey_str) > MAX_PUBKEY_STR:
        raise ActuationError("sender_pubkey must be a string within the size cap")
    # Authorization BEFORE crypto: an un-allowlisted key never reaches signature verify.
    # Compare the canonical string form; decode_multikey also guarantees it is well-formed.
    raw_pubkey = decode_multikey(pubkey_str)  # raises OriginError (ValueError) if malformed
    if pubkey_str not in allowed_pubkeys:
        raise ActuationError("sender_pubkey is not an allowlisted commander key")

    sig_str = raw["sig"]
    if not isinstance(sig_str, str) or len(sig_str) > _MAX_SIG_STR:
        raise ActuationError("sig must be a string within the size cap")
    sig = b64url_raw(sig_str, expect_len=SIG_RAW_LEN, field="sig")

    # The load-bearing check: Ed25519 over the canonical bytes. Fail closed on ANY
    # verification error (bad sig, tampered field → reconstructed bytes differ → invalid).
    message = actuation_signing_bytes(
        raw_pubkey=raw_pubkey, robot_id=robot_id, command=command,
        seq=seq, signed_at_ms=ts)
    try:
        Ed25519PublicKey.from_public_bytes(raw_pubkey).verify(sig, message)
    except InvalidSignature as e:
        raise ActuationError("signature does not verify") from e

    # Freshness (defense in depth on top of the seq guard). A small forward skew is
    # tolerated (clocks lead); a packet older than max_age_ms is stale → dropped.
    _MAX_FWD_SKEW_MS = 1000
    age = now_ms - ts
    if age > max_age_ms:
        raise ActuationError(f"stale: signed {age}ms ago > max_age {max_age_ms}ms — check clocks")
    if age < -_MAX_FWD_SKEW_MS:
        raise ActuationError(f"from the future by {-age}ms (> {_MAX_FWD_SKEW_MS}ms skew) — check clocks")

    return {k: raw[k] for k in _REQUIRED_KEYS}


class SeqHighWater:
    """Durable per-robot monotonic sequence high-water mark — the replay + staleness
    guard, clock-independent and restart-surviving.

    ``check_and_advance(robot_id, seq)`` returns True and persists the new high-water iff
    ``seq`` is STRICTLY greater than the stored one; otherwise returns False (a replay or
    an out-of-order/stale packet) and actuation MUST be skipped. Persistence is atomic
    (temp file + ``os.replace``) so a crash mid-write can never corrupt the store or lose
    the mark — a corrupted store that reset to 0 would re-open the replay window.

    Single-process reference implementation (the bridge is one process). Not safe across
    processes; the bridge owns exactly one.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._marks: dict[str, int] = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._marks = {str(k): int(v) for k, v in data.items() if isinstance(v, int)}
            except (json.JSONDecodeError, ValueError, OSError):
                # A corrupt store must NOT silently reset to an empty (replay-open) state:
                # fail closed so the operator sees it, rather than accepting old seqs again.
                raise ActuationError(f"seq high-water store at {path} is corrupt — refusing to start")

    def check_and_advance(self, robot_id: str, seq: int) -> bool:
        if seq <= self._marks.get(robot_id, -1):
            return False
        self._marks[robot_id] = seq
        self._persist()
        return True

    def _persist(self) -> None:
        d = os.path.dirname(self._path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".seq-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._marks, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)  # atomic on POSIX
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
