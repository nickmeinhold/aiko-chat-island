"""Sovereign message-signing carriage — the gateway's CARRIER role (#1816).

The app signs every message client-side (Ed25519 over a length-prefixed,
domain-separated byte layout — the interop contract is frozen in the app's
`docs/crucible/sovereign-message-signing/SIGNING-SPEC.md`, pinned by a golden
vector). This gateway is a **carrier, not a verifier**: it validates the SHAPE
of the `origin` envelope at the trust boundary, persists it, and echoes it
verbatim on every read path so a recipient can reconstruct the signed bytes and
verify. It never checks the signature itself — verification is the recipient's
job and is gated behind a trust root that does not exist yet (app TEMPER T4: no
"verified sender" UI).

WHAT THIS DOES AND DOES NOT BIND (read before trusting an echoed origin): the
only binding this carrier makes is `origin.client_msg_id == the frame's
client_msg_id` — i.e. the signed id is the id the message is stored under. It
does NOT bind `sender_pubkey` to the authenticated account: shape-valid means
"*some* key signed *these* bytes", never "*this user's* key". Any authenticated
client may attach any well-formed Multikey + any 64-byte sig, and the gateway
will carry it. So an echoed origin is *attestation of a signature over the body*,
NOT proof of *who* signed — treating echo as identity is forgery-as-echo. The
pubkey->account binding (contemporaneous, at send time) is #1816 PR B
(`signing_keys`); until it lands and a trust root exists, no consumer may render
"verified sender" from origin alone (app TEMPER T4).

Why validate shape but not signature: an unverifiable-but-well-formed envelope
is still safe to carry (absent/garbage origin = "unverified", never "invalid").
But malformed input on a wire boundary is rejected fail-closed — never trust the
claimed `alg` (JWT alg-confusion class), never let `origin.client_msg_id` diverge
from the frame's (envelope-vs-payload confusion), cap every field (DoS).

`signing_bytes()` is the canonical reconstruction. It is NOT used in the
production carry path (we don't verify), but it is the exact function a verifier
would use, exercised by the golden-vector test so our reconstruction can never
silently drift from the app's signer.
"""
from __future__ import annotations

import base64
import re
import struct
from typing import Any

DOMAIN_TAG = "aikochat:msg:v1:EdDSA"
ALG = "EdDSA"                       # the ONLY accepted alg — allowlist, never trust the envelope's claim
SUPPORTED_V = 1
_MULTICODEC_ED25519 = b"\xed\x01"  # varint multicodec prefix inside an ed25519 Multikey
PUBKEY_RAW_LEN = 32
SIG_RAW_LEN = 64

# Field caps (untrusted client input on a wire boundary). Generous but finite —
# a Multikey pubkey is ~48 chars, a raw-64 sig is ~86 base64url chars.
_MAX_PUBKEY_STR = 128
_MAX_SIG_STR = 128
_MAX_CLIENT_MSG_ID = 64            # matches the messages.client_msg_id column width
_MAX_SIGNED_AT_MS = 1 << 62        # sane u64-ish upper bound (well past any real clock)

# Unpadded base64url charset — the spec pins sig to base64url-unpadded, so we
# reject `=` padding and any non-[A-Za-z0-9_-] byte BEFORE decoding. Python's
# base64.urlsafe_b64decode is permissive (ignores some junk, tolerates padding),
# so a length check alone would let a non-canonical string decode to 64 bytes and
# be echoed verbatim across the trust boundary. Charset-gate first.
_B64URL_UNPADDED_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_B58_STR = 128                 # decode_multikey input cap (defense-in-depth for the bigint loop)

# Exactly these keys, no more (frozen v1 shape; a change is a v2, never a silent add).
_REQUIRED_KEYS = frozenset(
    {"v", "alg", "key_version", "sender_pubkey", "client_msg_id", "signed_at_ms", "sig"}
)

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


class OriginError(ValueError):
    """A malformed/inconsistent signing `origin` envelope. Caller replies with an
    `error` frame — fail closed, never persist an unvalidated envelope."""


def _b58decode(s: str) -> bytes:
    """Minimal base58btc decode (no external dep). Raises OriginError on a
    non-alphabet character."""
    num = 0
    for ch in s:
        idx = _B58_INDEX.get(ch)
        if idx is None:
            raise OriginError("sender_pubkey is not valid base58btc")
        num = num * 58 + idx
    # Reconstruct big-endian bytes, preserving leading-zero (leading '1') bytes.
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def decode_multikey(s: str) -> bytes:
    """Decode an ed25519 Multikey (`z` + base58btc(0xed01 ‖ 32 raw bytes)) to the
    raw 32-byte public key. The signed bytes use the RAW key, so this is what a
    verifier feeds into `signing_bytes` field #2. Raises OriginError if the string
    is not a well-formed ed25519 Multikey."""
    if not s or s[0] != "z":
        raise OriginError("sender_pubkey must be a multibase-base58btc Multikey (z…)")
    # Length-guard the base58 body before the O(n^2) bigint decode — defense in
    # depth so the decoder is safe even if a future caller forgets the field cap.
    if len(s) > _MAX_B58_STR:
        raise OriginError("sender_pubkey too long")
    decoded = _b58decode(s[1:])
    if not decoded.startswith(_MULTICODEC_ED25519):
        raise OriginError("sender_pubkey is not an ed25519 Multikey (bad multicodec)")
    raw = decoded[len(_MULTICODEC_ED25519):]
    if len(raw) != PUBKEY_RAW_LEN:
        raise OriginError(f"sender_pubkey raw length {len(raw)} != {PUBKEY_RAW_LEN}")
    return raw


def _b64url_raw(s: str, *, expect_len: int, field: str) -> bytes:
    """Strictly decode UNPADDED base64url and assert an exact decoded length.
    Charset-gate before decoding — `base64.urlsafe_b64decode` is permissive
    (tolerates `=` padding and silently skips some junk), so without this an
    `=`-padded or standard-alphabet string could decode to the right length and
    be echoed across the trust boundary as if canonical."""
    if not _B64URL_UNPADDED_RE.match(s):
        raise OriginError(f"{field} must be unpadded base64url ([A-Za-z0-9_-], no '=')")
    try:
        raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError) as e:
        raise OriginError(f"{field} is not valid base64url") from e
    if len(raw) != expect_len:
        raise OriginError(f"{field} decoded length {len(raw)} != {expect_len}")
    return raw


def signing_bytes(
    *, raw_pubkey: bytes, channel_id: str, client_msg_id: str,
    signed_at_ms: int, body: str, reply_to: str | None,
) -> bytes:
    """The canonical, length-prefixed, domain-separated bytes an Ed25519 signature
    is computed over — reproduced EXACTLY per SIGNING-SPEC.md. Every variable-length
    field is preceded by a big-endian u32 length; `signed_at_ms` is a fixed-width
    big-endian u64 (no length prefix); `reply_to` is the empty string when absent.
    A verifier that produces different bytes for the spec's golden vector is
    non-conformant — that is exactly what the golden-vector test guards."""
    def lp(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b

    return b"".join((
        lp(DOMAIN_TAG.encode()),
        lp(raw_pubkey),
        lp(channel_id.encode()),
        lp(client_msg_id.encode()),
        struct.pack(">Q", signed_at_ms),
        lp(body.encode()),
        lp((reply_to or "").encode()),
    ))


def validate_origin(raw: Any, *, frame_client_msg_id: str) -> dict | None:
    """Validate an inbound `origin` envelope at the trust boundary and return the
    validated dict to persist + echo, or None when absent (an unsigned message —
    legal; unsigned history predates the feature). Raises OriginError on any
    malformation. Shape only — the signature is NOT verified here.

    Trust-boundary rules, all fail-closed:
      * exactly the frozen v1 key set (no unknown keys, no missing ones);
      * `alg` allowlisted to EdDSA (never trust the envelope's claimed alg);
      * `sender_pubkey` a well-formed ed25519 Multikey (decodes to 32 raw bytes);
      * `sig` unpadded base64url decoding to exactly 64 bytes;
      * `signed_at_ms` a sane non-negative integer;
      * `origin.client_msg_id` == the frame's client_msg_id (envelope-vs-payload
        confusion defense — the signed id must be the id the message is stored under).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OriginError("origin must be a JSON object")
    keys = set(raw.keys())
    if keys != _REQUIRED_KEYS:
        missing = _REQUIRED_KEYS - keys
        extra = keys - _REQUIRED_KEYS
        raise OriginError(f"origin key set invalid (missing={sorted(missing)}, unexpected={sorted(extra)})")

    # bool is an int subclass — exclude it everywhere an int is expected so a JSON
    # `true`/`false` can't satisfy `== 1` (True == 1) and slip past. `v` is the
    # frozen envelope discriminator, so it gets the guard too.
    if isinstance(raw["v"], bool) or raw["v"] != SUPPORTED_V:
        raise OriginError(f"origin.v {raw['v']!r} unsupported (expected {SUPPORTED_V})")
    if raw["alg"] != ALG:
        raise OriginError(f"origin.alg {raw['alg']!r} not allowed (only {ALG!r})")
    kv = raw["key_version"]
    if isinstance(kv, bool) or not isinstance(kv, int) or kv < 1:
        raise OriginError("origin.key_version must be an integer >= 1")

    pubkey = raw["sender_pubkey"]
    if not isinstance(pubkey, str) or len(pubkey) > _MAX_PUBKEY_STR:
        raise OriginError("origin.sender_pubkey must be a string within the size cap")
    decode_multikey(pubkey)  # raises OriginError if not a well-formed ed25519 Multikey

    cmid = raw["client_msg_id"]
    if not isinstance(cmid, str) or len(cmid) > _MAX_CLIENT_MSG_ID:
        raise OriginError("origin.client_msg_id must be a string within the size cap")
    if cmid != frame_client_msg_id:
        raise OriginError("origin.client_msg_id does not match the frame client_msg_id")

    ts = raw["signed_at_ms"]
    if isinstance(ts, bool) or not isinstance(ts, int) or ts < 0 or ts > _MAX_SIGNED_AT_MS:
        raise OriginError("origin.signed_at_ms must be a sane non-negative integer")

    sig = raw["sig"]
    if not isinstance(sig, str) or len(sig) > _MAX_SIG_STR:
        raise OriginError("origin.sig must be a string within the size cap")
    _b64url_raw(sig, expect_len=SIG_RAW_LEN, field="origin.sig")

    # Return a FRESH closed projection (exactly the required keys), not the
    # caller's dict — so the persisted/echoed JSON can't be mutated through a
    # lingering reference to the inbound frame.
    return {k: raw[k] for k in _REQUIRED_KEYS}
