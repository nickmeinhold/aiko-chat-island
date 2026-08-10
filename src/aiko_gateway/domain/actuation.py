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

Three defenses, all required, because each covers a gap the others can't:
  * **Ed25519 sig + commander allowlist** — authenticity + authorization. Only the
    one shipped commander key can command the robot; "anyone in the room" cannot.
  * **Durable monotonic ``seq`` high-water** (``SeqHighWater``) — replay + staleness in
    ONE clock-independent mechanism: an envelope with ``seq <= high_water`` is dropped,
    so a captured packet can't be re-fired and a redelivered backlog can't actuate
    late. Survives a bridge restart (persisted to disk).
  * **Freshness deadline** (``max_age_ms``, default a TIGHT 2s) — a SEPARATE timeliness
    gate, only trustworthy under asserted NTP discipline; it is defense-in-depth on top
    of the seq guard, never the primary replay defense (cross-host clocks disagree).

The blessed actuation path is ``verify_and_advance`` — the SEALED door that runs verify
THEN seq-advance in the only safe order (advancing before verifying is a permanent-DoS
footgun; the sealed door makes that order unrepresentable). ``verify_actuation`` stays
public for pure, side-effect-free verification.

Caller / operational contract this boundary DEPENDS ON but cannot itself enforce (the
commander + the LiveKit transport are out of this repo — tracked as follow-ups):
  * The **commander** must mint a strictly-increasing per-robot ``seq`` from DURABLE state:
    if the commander restarts and its counter resets, envelopes are rejected until seq
    climbs back past the bridge's persisted high-water (a self-inflicted denial of
    actuation), so commander-side seq durability is a hard requirement.
  * The seq guard is **at-most-once, not exactly-once**: a reordered delivery (``seq=5``
    arriving before ``seq=4``) drops the lower seq permanently. That is the correct bias
    for a physical actuator (better a missed wave than a double/late one), but the
    commander should not rely on every envelope landing.
  * ``verify_and_advance`` BURNS the seq at verify time. If the bridge re-checks freshness
    again at dequeue (recommended) and a mailbox delay exceeds ``max_age_ms``, the envelope
    is dropped with its seq already spent — the commander must mint a NEW seq to retry, never
    reuse the burned one. Prefer checking freshness immediately before ``verify_and_advance``
    so admit and actuate are adjacent.
  * The allowlist is AUTHORIZATION, not DoS resistance: ``sender_pubkey`` is public, so any
    room participant can name the allowlisted key and make the bridge burn verify-CPU on
    garbage signatures. Rate-limiting / admission control belongs at the transport, OUTSIDE
    this pure verifier.
  * The store's authority is FILESYSTEM permissions: a missing store file is a clean empty
    map (first run), so anyone who can delete ``seq.json`` and restart the bridge reopens the
    replay window (bounded to ``max_age_ms`` by the freshness gate). If the bridge user is not
    the host root, the store's file mode + directory permissions are part of this boundary.
  * The high-water is keyed by ``robot_id`` ALONE, but the allowlist is a SET of commander
    keys — so the seq space is shared across all allowlisted commanders for a robot. Two
    commanders are therefore assumed to be one logical single-writer per robot: if commander A
    reaches ``seq=100`` and commander B (also allowlisted) then sends a valid ``seq=50``, B's
    command is dropped. For increment 1 (one commander per robot) this holds; a genuine
    multi-commander deployment must key the store by ``(robot_id, sender_pubkey)`` instead.
  * A persist failure keeps the in-memory mark advanced (fail-safe) but leaves DISK at the old
    mark. On a bridge RESTART after such a failure the store reloads the lower mark, so a
    still-fresh, previously memory-only-burned envelope can actuate ONCE (bounded by
    ``max_age_ms`` and still requiring a valid signature) — "spent" is process-lifetime, not
    crash-lifetime. Treat a persist failure as an operator-visible fault, not a soft skip.
"""
from __future__ import annotations

import json
import os
import struct
import tempfile
import threading
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .signing import (
    MAX_PUBKEY_STR,
    MAX_SIGNED_AT_MS,
    OriginError,
    SIG_RAW_LEN,
    b64url_raw,
    decode_multikey,
    encode_multikey,
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
# seq ceiling is 2**53-1 (JS Number.MAX_SAFE_INTEGER), NOT full u64: the wire is JSON and a
# JavaScript/TypeScript commander loses integer precision above 2**53-1, so an independent
# signer and this verifier could silently diverge on a larger seq. Cap it at the interop floor.
_MAX_SEQ = (1 << 53) - 1
# The bridge re-checks the deadline at DEQUEUE (immediately before actuation). Tight by
# default — a servo command that aged in the mailbox is stale. Explicit, NEVER inherit a
# minutes-long default (a 5-min window on a physical device is the crucible's rejected footgun).
DEFAULT_MAX_AGE_MS = 2000
# A HARD ceiling on any caller-supplied max_age_ms: the crucible rejected minutes-on-a-servo,
# so the freshness floor must NOT be caller-optional — a bridge cannot widen the window into
# the rejected footgun via a kwarg. 30s is generous for skew yet nowhere near "minutes".
_MAX_ALLOWED_AGE_MS = 30_000
# A small forward skew tolerated because cross-host clocks lead as well as lag. Module-level
# so the timeliness contract is visible with the other boundary constants, not buried in a fn.
_MAX_FWD_SKEW_MS = 1000

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
    expected_robot_id: str | None = None,
    allowed_commands: set[str] | None = None,
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

    ``ActuationError`` is the SOLE exception this raises for a bad envelope — malformed
    ``sender_pubkey``/``sig`` (which the primitives in ``signing`` report as ``OriginError``)
    are normalized here, so a caller's ``except ActuationError`` fail-closed handler catches
    every deviation, not just the signature miss.

    If ``expected_robot_id`` is given, the envelope's ``robot_id`` MUST equal it — a signed
    command addressed to another robot is rejected at this boundary rather than relying on
    the call site to compare (identity binding; an actuator-agnostic codec still needs it).
    ``verify_and_advance`` (the blessed actuation path) makes this REQUIRED; it is optional
    here only for pure codec use (tests/dry-runs).

    If ``allowed_commands`` is given, ``command`` MUST be in it — command admission at the
    boundary rather than trusting the bridge's downstream table alone. ``None`` keeps the
    codec actuator-agnostic (the bridge's closed table governs), by design.

    Does NOT check the ``seq`` high-water — that is stateful and belongs to
    ``SeqHighWater._check_and_advance`` at the call site, so verification stays pure and
    the durable replay guard is a single explicit gate. The caller MUST run BOTH — or, better,
    call ``verify_and_advance``, the SEALED door that runs them in the only safe order.
    """
    # Caller-surface guards keep the SOLE-exception contract TOTAL: a bad now_ms /
    # allowed_pubkeys would otherwise raise TypeError, escaping `except ActuationError`.
    if isinstance(now_ms, bool) or not isinstance(now_ms, int):
        raise ActuationError("now_ms must be an int (ms since epoch)")
    if not isinstance(allowed_pubkeys, (set, frozenset)):
        raise ActuationError("allowed_pubkeys must be a set of commander Multikey strings")
    # allowed_commands MUST be a set when given — a bare str would make `command in ...`
    # a SUBSTRING match ("wa" in "wave"), silently widening admission on a physical boundary.
    if allowed_commands is not None and not isinstance(allowed_commands, (set, frozenset)):
        raise ActuationError("allowed_commands must be a set of command strings (or None)")
    # The freshness floor is not caller-optional: reject an out-of-range max_age_ms rather
    # than let a bridge kwarg reopen the crucible-rejected minutes window (or invert it).
    if isinstance(max_age_ms, bool) or not isinstance(max_age_ms, int) \
            or max_age_ms < 0 or max_age_ms > _MAX_ALLOWED_AGE_MS:
        raise ActuationError(
            f"max_age_ms {max_age_ms!r} outside [0, {_MAX_ALLOWED_AGE_MS}] — the servo freshness floor is not caller-optional")

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
    if expected_robot_id is not None and robot_id != expected_robot_id:
        raise ActuationError(f"robot_id {robot_id!r} does not match this bridge's {expected_robot_id!r}")

    command = raw["command"]
    if not isinstance(command, str) or not command or len(command) > _MAX_COMMAND:
        raise ActuationError("command must be a non-empty string within the size cap")
    if allowed_commands is not None and command not in allowed_commands:
        raise ActuationError(f"command {command!r} is not in the allowed set — rejected at the boundary")

    seq = raw["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0 or seq > _MAX_SEQ:
        raise ActuationError(f"seq must be a non-negative integer <= 2**53-1 ({_MAX_SEQ}) for JSON interop")

    ts = raw["signed_at_ms"]
    if isinstance(ts, bool) or not isinstance(ts, int) or ts < 0 or ts > MAX_SIGNED_AT_MS:
        raise ActuationError("signed_at_ms must be a sane non-negative integer")

    pubkey_str = raw["sender_pubkey"]
    if not isinstance(pubkey_str, str) or len(pubkey_str) > MAX_PUBKEY_STR:
        raise ActuationError("sender_pubkey must be a string within the size cap")
    # Authorization BEFORE crypto: an un-allowlisted key never reaches signature verify.
    # Compare the canonical string form; decode_multikey also guarantees it is well-formed.
    # Normalize the primitive's OriginError to ActuationError so the caller's fail-closed
    # `except ActuationError` catches a malformed key, not just a bad signature.
    try:
        raw_pubkey = decode_multikey(pubkey_str)
    except OriginError as e:
        raise ActuationError(f"sender_pubkey malformed: {e}") from e
    # Authorize on the CANONICAL encoding of the decoded key, not the raw wire string — so
    # authorization is key-identity, not string-identity. If two Multikey spellings ever
    # decode to the same 32 bytes, a canonical allowlist still matches; the security story
    # rests on the bytes, not on the commander and the config agreeing on spelling.
    if encode_multikey(raw_pubkey) not in allowed_pubkeys:
        raise ActuationError("sender_pubkey is not an allowlisted commander key")

    sig_str = raw["sig"]
    if not isinstance(sig_str, str) or len(sig_str) > _MAX_SIG_STR:
        raise ActuationError("sig must be a string within the size cap")
    try:
        sig = b64url_raw(sig_str, expect_len=SIG_RAW_LEN, field="sig")
    except OriginError as e:
        raise ActuationError(f"sig malformed: {e}") from e

    # The load-bearing check: Ed25519 over the canonical bytes. Fail closed on ANY
    # verification error (bad sig, tampered field → reconstructed bytes differ → invalid).
    # Wrap the construction too: a lone-surrogate str (json can materialize "\ud800") makes
    # .encode() raise UnicodeEncodeError, and an out-of-u64 field would make struct.pack raise
    # struct.error — both would escape `except ActuationError`, breaking the SOLE-exception
    # contract. Normalize them here so no bad envelope leaves this function as another type.
    try:
        message = actuation_signing_bytes(
            raw_pubkey=raw_pubkey, robot_id=robot_id, command=command,
            seq=seq, signed_at_ms=ts)
    except (UnicodeEncodeError, struct.error) as e:
        raise ActuationError(f"envelope fields are not canonically encodable: {e}") from e
    try:
        Ed25519PublicKey.from_public_bytes(raw_pubkey).verify(sig, message)
    except (InvalidSignature, ValueError) as e:
        # InvalidSignature = bad sig; ValueError = a non-32-byte key slipping past
        # decode_multikey. Both normalize to ActuationError so the SOLE-exception
        # contract holds even if an upstream guarantee ever weakens (cheap insurance).
        raise ActuationError("signature does not verify") from e

    # Freshness (defense in depth on top of the seq guard). The window is ASYMMETRIC by
    # design: the stale side is caller-tunable up to _MAX_ALLOWED_AGE_MS (a slightly-old
    # servo command is a routine, low-suspicion event), but the FUTURE side is a small fixed
    # skew — a command timestamped ahead of now is anomalous (clock fault or forgery attempt),
    # so it stays tight and non-tunable. Under the asserted NTP discipline both bridge and
    # commander sit well inside _MAX_FWD_SKEW_MS; a bridge clock lagging > 1s is a fault to fix
    # at the host, not a knob to widen here.
    age = now_ms - ts
    if age > max_age_ms:
        raise ActuationError(f"stale: signed {age}ms ago > max_age {max_age_ms}ms — check clocks")
    if age < -_MAX_FWD_SKEW_MS:
        raise ActuationError(f"from the future by {-age}ms (> {_MAX_FWD_SKEW_MS}ms skew) — check clocks")

    return {k: raw[k] for k in _REQUIRED_KEYS}


class SeqHighWater:
    """Durable per-robot monotonic sequence high-water mark — the replay + staleness
    guard, clock-independent and restart-surviving.

    The advance primitive (``_check_and_advance``) advances iff ``seq`` is STRICTLY greater
    than the stored one; otherwise it does not, and actuation is skipped. It is PRIVATE — the
    only caller is ``verify_and_advance``, so an unverified seq can never advance the guard.
    Persistence is atomic (temp file + ``os.replace`` + directory fsync) so a crash mid-write
    can never corrupt the store or lose the mark — a corrupted store that reset to 0 would
    re-open the replay window.

    Single-process reference implementation (the bridge is one process). Not safe across
    processes; the bridge owns exactly one. Within the process the advance IS safe under
    concurrent callers (a lock serializes the read-modify-write) — LiveKit data handlers can
    fire concurrently, and an unguarded RMW would let two callers both observe
    ``seq > high_water`` and both actuate (a double wave). On a persist failure the in-memory
    mark stays advanced (fail-safe: rejects more, never actuates more) and the error surfaces
    as ``ActuationError``.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._marks: dict[str, int] = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, ValueError, OSError) as e:
                # A corrupt store must NOT silently reset to an empty (replay-open) state:
                # fail closed so the operator sees it, rather than accepting old seqs again.
                raise ActuationError(f"seq high-water store at {path} is corrupt — refusing to start") from e
            # Corruption that still JSON-parses (a list, a scalar, a null, a wrong-typed
            # value) is corruption too — the ONLY safe recovery is fail-closed, never a
            # silent filter (dropping one robot's mark reopens ITS replay window). Validate
            # the whole shape: a dict of str→(non-bool int in u64 range), or refuse to start.
            if not isinstance(data, dict):
                raise ActuationError(
                    f"seq high-water store at {path} is not a JSON object (got {type(data).__name__}) — refusing to start"
                )
            marks: dict[str, int] = {}
            for k, v in data.items():
                if not isinstance(k, str):
                    raise ActuationError(f"seq high-water store at {path} has a non-string robot_id key — refusing to start")
                # bool is an int subclass — a persisted JSON `true` must NOT decay to 1.
                if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > _MAX_SEQ:
                    raise ActuationError(
                        f"seq high-water store at {path} has a non-seq-range mark for {k!r} ({v!r}) — refusing to start"
                    )
                marks[k] = v
            self._marks = marks

    def _check_and_advance(self, robot_id: str, seq: int) -> bool:
        """PRIVATE — the ONLY caller is ``verify_and_advance``. Kept off the public API on
        purpose: a proof-free advance is a permanent-DoS footgun (advancing to seq=2**64-1
        with no signature wedges the robot), so the exported path MUST demand a verified
        envelope. Inputs are re-validated even though the sealed door passes clean ints —
        this is the durable safety store, and a poisoned mark would fail the next restart.
        """
        if not isinstance(robot_id, str) or not robot_id:
            raise ActuationError("robot_id must be a non-empty string")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0 or seq > _MAX_SEQ:
            raise ActuationError(f"seq must be a non-negative integer <= 2**53-1 ({_MAX_SEQ})")
        # Serialize the read-modify-write: concurrent LiveKit handlers must not both pass.
        with self._lock:
            if seq <= self._marks.get(robot_id, -1):
                return False
            self._marks[robot_id] = seq
            try:
                self._persist()
            except OSError as e:
                # The mark stays advanced in memory — that is the FAIL-SAFE direction (it only
                # ever rejects MORE, never actuates more). Surface as ActuationError (not a raw
                # OSError) so the caller's `except ActuationError` catches it and does NOT
                # actuate on a mark we could not durably commit.
                raise ActuationError(f"seq high-water persist failed: {e} — refusing to actuate") from e
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
            # fsync the DIRECTORY so the rename itself survives power loss — without this the
            # replace can be lost on a crash and resurrect the OLD high-water (replay reopens).
            # A robot host loses power precisely when things go wrong, so this is load-bearing.
            dir_fd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def verify_and_advance(
    raw: Any,
    *,
    allowed_pubkeys: set[str],
    seq_store: SeqHighWater,
    now_ms: int,
    expected_robot_id: str,
    allowed_commands: set[str],
    max_age_ms: int = DEFAULT_MAX_AGE_MS,
) -> dict:
    """The SEALED door a bridge should call: verify the envelope, THEN advance the seq —
    in that order — and return the validated dict, or raise ``ActuationError``.

    ``expected_robot_id`` and ``allowed_commands`` are REQUIRED here (unlike the pure,
    actuator-agnostic ``verify_actuation``): the blessed physical-safety path fails closed on
    BOTH admission decisions — WHO may command this robot and WHAT it may be commanded to do.
    A required PARAMETER is not enough (Python would still accept ``None``), so both are
    value-guarded below: an omitted, ``None``, empty, or wrong-typed identity/command-set is
    an ``ActuationError``, never a silent skip. On a shared LiveKit room the transport fans
    bytes to every bridge, so an unbound ``robot_id`` would actuate the wrong arm and an
    unbounded command set would run any signed ``"terminate"`` the commander could emit.

    Why a single door: advancing the durable high-water is a state mutation, and the advance
    primitive persists whatever seq it is handed — so running it BEFORE verification would let
    any room participant (no key needed) submit a huge seq and wedge the robot. The blessed
    path advances the seq ONLY after a valid, allowlisted, fresh signature over that exact
    ``(robot_id, command, seq, signed_at_ms)`` is proven. A replay/stale seq raises
    ``ActuationError`` (not a bare ``False``), so the caller's fail-closed handler treats
    replay like a bad signature.

    Honest scope: the advance primitive (``_check_and_advance``) is private *by convention*
    (Python has no hard privacy), and it only ever ADVANCES the guard — it never actuates, so
    the worst a stray direct call can do is wedge liveness (reject future seqs), never cause a
    mis-actuation. This function is the only path that advances *after proof*; a bridge should
    never call the primitive directly.

    Ordering, not atomicity: verify and advance are not one atomic critical section. Under
    concurrent envelopes the advance is serialized (the store's lock), and a reordered lower
    seq that loses the race is simply dropped — the safe, monotonic bias for a physical
    actuator, never a double actuation.

    ``verify_actuation`` remains public for pure, side-effect-free verification (tests,
    dry-runs); but the actuation path should reach for THIS.
    """
    # Value guards on the blessed door — a REQUIRED parameter is not a value guard (Python
    # accepts None/empty/wrong-typed), and identity/command binding are exactly what "sealed"
    # means here. Fail closed rather than forward a None that verify_actuation would treat as
    # "skip the check".
    if not isinstance(expected_robot_id, str) or not expected_robot_id:
        raise ActuationError("expected_robot_id must be a non-empty string on the sealed door")
    if not isinstance(allowed_commands, (set, frozenset)) or not allowed_commands:
        raise ActuationError("allowed_commands must be a non-empty set on the sealed door")
    # Caller-surface guard: a wrong seq_store would raise AttributeError below, escaping the
    # sole-exception contract this door also sells.
    if not isinstance(seq_store, SeqHighWater):
        raise ActuationError("seq_store must be a SeqHighWater instance")
    parsed = verify_actuation(
        raw,
        allowed_pubkeys=allowed_pubkeys,
        now_ms=now_ms,
        max_age_ms=max_age_ms,
        expected_robot_id=expected_robot_id,
        allowed_commands=allowed_commands,
    )
    # Only a verified envelope can advance the durable replay guard (private primitive).
    if not seq_store._check_and_advance(parsed["robot_id"], parsed["seq"]):
        raise ActuationError(
            f"seq {parsed['seq']} for {parsed['robot_id']!r} is a replay or stale (<= high-water) — dropped"
        )
    return parsed
