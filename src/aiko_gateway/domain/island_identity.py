"""Island identity — the island's own signed self-manifest (crucible-09 Phase A, A1).

Where ``domain/signing.py`` is the CARRIER for the *app's* per-user message
signatures (the gateway never holds those keys), this module is the island signing
with its OWN long-lived Ed25519 identity key. That key is the trust root for
"who this island is and what moderation posture it runs": it signs a manifest
{id, display_name, base_url, mode, key_version} that a client fetches at connect
(``GET /v1/island``) and — later (A4) — a peer observes at federation handshake.

WHY sign the WHOLE self-entry, not just ``mode``: the directory entry
(peers_service.Island) is unsigned today and defended only by an operator allowlist
(see that module's TRUST MODEL banner). ``base_url`` — where a client is told to
connect — is at least as security-critical as ``mode``. Signing the whole tuple in
one manifest gives the self-entry provenance across untrusted hops (gossip / a
peer relay), closing the "signed mode but forgeable base_url" asymmetry rather than
protecting the wrong field first.

DOMAIN SEPARATION: the signed bytes are prefixed with ``aikochat:island:v1:EdDSA``,
a DIFFERENT tag than the message signer's ``aikochat:msg:v1:EdDSA``. An island
identity signature can never be reinterpreted as a message signature (or vice
versa), even though both are Ed25519 over length-prefixed fields.

The private key is derived from a 32-byte seed supplied via config
(``ISLAND_SIGNING_SEED``; SOPS in deploy, a dev default in dev — prod fail-closed
rejects the dev default, exactly like ``jwt_secret``). It is NEVER persisted or
exposed; only the public Multikey + the signature leave the process.
"""
from __future__ import annotations

import base64
import struct

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from . import signing
from .island_mode import IslandMode

# The domain tag: distinct from signing.DOMAIN_TAG so an island-manifest signature
# is not a valid message signature under any circumstances (domain separation).
DOMAIN_TAG = "aikochat:island:v1:EdDSA"
ALG = "EdDSA"          # the ONLY alg — allowlist, mirroring signing.ALG
# v2 (#2452): added `signed_at_ms` to the signed bytes — a freshness binding so the A4
# peer-federation door can reject a stale, replayed posture (an old signed `moderator`
# manifest otherwise verifies forever after a mode flip). A verifier pins v==V, so a v1
# eternal-signature manifest is a structural reject, never a silent downgrade. Adding a
# field is a version bump, never a silent add (the module's standing discipline).
V = 2                  # manifest envelope version (a change is a v2, never a silent add)

# The Ed25519 seed is exactly 32 bytes; a public key is 32 bytes; a signature 64.
SEED_LEN = 32
SIG_LEN = 64

# The modes the manifest may carry — derived from the SINGLE vocabulary SoT
# (island_mode.IslandMode) so this codec and config.Settings.island_mode can never
# drift. `e2ee` is schema-reserved (Phase B) and refused at BOOT by
# config._harden_for_production; it is still a *valid vocabulary* value a signed
# manifest could name (so a Phase B peer is legible to verify), so verify allowlists
# the whole enum and lets the boot guard — not the codec — own the Phase-A-only
# policy. Anything outside the enum is a malformed manifest. (StrEnum members are str,
# so `"moderator" in VALID_MODES` works by value.)
VALID_MODES = frozenset(IslandMode)

# Field caps on untrusted manifest strings (the A4 peer-federation door takes
# attacker-influenceable input) — bound the work BEFORE crypto, mirroring
# signing.validate_origin's per-field caps. Generous: an island id/name is a short
# slug, a base_url a bounded URL.
_MAX_ID_STR = 64
_MAX_NAME_STR = 64
_MAX_URL_STR = 255
# key_version is packed as a big-endian u32 in the signing bytes.
KEY_VERSION_MAX = 2**32 - 1
# signed_at_ms is packed as a big-endian u64, sharing the message signer's bound so one
# cap spans both Ed25519 signers (a rename can't silently weaken it — the reason
# signing.MAX_SIGNED_AT_MS is public, mirroring MAX_PUBKEY_STR).
MAX_SIGNED_AT_MS = signing.MAX_SIGNED_AT_MS
# The exact key set of a v2 signed manifest (frozen; a change is a v3, never a silent
# add) — mirrors signing._REQUIRED_KEYS for the message envelope.
MANIFEST_KEYS = frozenset({
    "v", "alg", "id", "display_name", "base_url", "mode", "key_version",
    "signed_at_ms", "island_pubkey", "signature",
})

# Default A4 freshness policy for is_fresh(). A manifest older than max_age is a stale
# posture; a small skew tolerates honest clock drift between islands. Both are params so
# the A4 door can tune per threat — these are just the sane starting point.
DEFAULT_MAX_AGE_MS = 5 * 60 * 1000   # 5 minutes
DEFAULT_CLOCK_SKEW_MS = 60 * 1000    # 1 minute


class IslandIdentityError(ValueError):
    """A malformed island signing seed (wrong length / not decodable). Raised at
    config/boot time so a broken identity key fails closed, never serves an island
    that cannot sign its own manifest."""


def decode_seed(seed_b64url: str) -> bytes:
    """Decode the configured ``ISLAND_SIGNING_SEED`` (unpadded base64url of 32 raw
    bytes) into the raw seed. Raises IslandIdentityError on anything that is not a
    32-byte value — so a truncated / mis-encoded seed refuses boot rather than
    silently deriving a different key than the operator intended."""
    try:
        # ONE canonical strict decoder across every Ed25519 boundary — charset-gates
        # unpadded base64url AND asserts the exact 32-byte length. Reusing the public
        # signing.b64url_raw (not a private-regex reach) means a rename can't silently
        # fracture this trust-boundary glue.
        return signing.b64url_raw(
            (seed_b64url or "").strip(), expect_len=SEED_LEN, field="ISLAND_SIGNING_SEED")
    except signing.OriginError as e:
        raise IslandIdentityError(str(e)) from e


def private_key_from_seed(seed_b64url: str) -> Ed25519PrivateKey:
    """Derive the island's Ed25519 private key from the configured seed."""
    return Ed25519PrivateKey.from_private_bytes(decode_seed(seed_b64url))


def public_multikey(priv: Ed25519PrivateKey) -> str:
    """The island's public key as a ``z…`` ed25519 Multikey — the same shape as a
    user signing key, so one verifier format spans the ecosystem."""
    raw = priv.public_key().public_bytes_raw()
    return signing.encode_multikey(raw)


def signing_bytes(
    *, id: str, display_name: str, base_url: str, mode: str, key_version: int,
    signed_at_ms: int,
) -> bytes:
    """The canonical, domain-separated, length-prefixed bytes the island signature is
    computed over. Every variable-length field is preceded by a big-endian u32
    length; ``key_version`` is a fixed-width big-endian u32 and ``signed_at_ms`` a
    fixed-width big-endian u64 (no length prefix — the SAME u64 layout the message
    signer uses for its ``signed_at_ms``). A verifier reconstructing different bytes
    for the same manifest is non-conformant — exactly what the manifest round-trip
    test guards. ``signed_at_ms`` is INSIDE the signed bytes, so a replayed manifest
    cannot be silently re-dated to look fresh (the whole point of #2452)."""
    def lp(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b

    return b"".join((
        lp(DOMAIN_TAG.encode()),
        lp(id.encode()),
        lp(display_name.encode()),
        lp(base_url.encode()),
        lp(mode.encode()),
        struct.pack(">I", key_version),
        struct.pack(">Q", signed_at_ms),
    ))


def _check_identity_tuple(
    *, id: str, display_name: str, base_url: str, mode: str, key_version: int,
    signed_at_ms: int,
) -> None:
    """Fail-closed validation of the signed manifest fields, shared by the emit
    (build) and verify doors so they can never drift apart. Mirrors
    signing.validate_origin's discipline: exact types, an allowlisted `mode` (never a
    free-text string at the crypto door), a `bool`-excluded u32 `key_version` and a
    `bool`-excluded u64 `signed_at_ms` (`True` is an int subclass that would otherwise
    pack as 1). Raises IslandIdentityError on any violation — the caller decides
    whether that is a boot refusal (build) or a rejected manifest (verify)."""
    for name, val, cap in (("id", id, _MAX_ID_STR),
                           ("display_name", display_name, _MAX_NAME_STR),
                           ("base_url", base_url, _MAX_URL_STR),
                           ("mode", mode, _MAX_NAME_STR)):
        if not isinstance(val, str):
            raise IslandIdentityError(f"manifest {name} must be a string")
        if len(val) > cap:
            raise IslandIdentityError(f"manifest {name} too long ({len(val)} > {cap})")
        # Non-empty (symmetric with peers_service.coerce_island, which rejects empty
        # id/name/url for a directory peer): a hollow id/base_url is a vacuous
        # navigation target, never a valid island — fail closed at the codec so the
        # A4 door can't sign or accept one.
        if not val:
            raise IslandIdentityError(f"manifest {name} must be non-empty")
    if mode not in VALID_MODES:
        raise IslandIdentityError(
            f"manifest mode {mode!r} is not one of {sorted(VALID_MODES)}")
    # bool before int: bool is an int subclass, so a JSON true/false must not satisfy
    # the u32 range check and pack as 0/1 (the alg-confusion-adjacent footgun
    # validate_origin already kills for v/key_version/signed_at_ms).
    if isinstance(key_version, bool) or not isinstance(key_version, int) \
            or not (1 <= key_version <= KEY_VERSION_MAX):
        raise IslandIdentityError(
            f"manifest key_version must be an int in [1, {KEY_VERSION_MAX}]")
    # signed_at_ms: same bool-before-int discipline (a JSON true/false must not pack as
    # a u64 1), non-negative, and bounded — an out-of-range value would otherwise raise
    # an uncaught struct.error out of struct.pack(">Q", ...) rather than a clean reject.
    if isinstance(signed_at_ms, bool) or not isinstance(signed_at_ms, int) \
            or not (0 <= signed_at_ms <= MAX_SIGNED_AT_MS):
        raise IslandIdentityError(
            f"manifest signed_at_ms must be an int in [0, {MAX_SIGNED_AT_MS}]")


def build_signed_manifest(
    *, id: str, display_name: str, base_url: str, mode: str,
    key_version: int, signed_at_ms: int, seed_b64url: str,
) -> dict:
    """Build THIS island's signed self-manifest: the identity tuple + a
    ``signed_at_ms`` freshness stamp + the island public Multikey + an Ed25519
    signature over ``signing_bytes(...)``. Pure w.r.t. its inputs (no I/O) so the
    endpoint can stamp ``now`` per request and tests can build one from arbitrary
    fields. The signature is unpadded base64url — the same encoding the message signer
    uses for ``origin.sig``."""
    _check_identity_tuple(id=id, display_name=display_name, base_url=base_url,
                          mode=mode, key_version=key_version, signed_at_ms=signed_at_ms)
    priv = private_key_from_seed(seed_b64url)
    msg = signing_bytes(
        id=id, display_name=display_name, base_url=base_url,
        mode=mode, key_version=key_version, signed_at_ms=signed_at_ms)
    sig = priv.sign(msg)
    sig_b64url = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return {
        "v": V,
        "alg": ALG,
        "id": id,
        "display_name": display_name,
        "base_url": base_url,
        "mode": mode,
        "key_version": key_version,
        "signed_at_ms": signed_at_ms,
        "island_pubkey": public_multikey(priv),
        "signature": sig_b64url,
    }


def verify_manifest(manifest: dict) -> bool:
    """Verify a signed island manifest: reconstruct the canonical bytes from its
    identity fields and check the signature against its declared ``island_pubkey``.
    Returns True iff the signature is valid for THIS manifest under THAT key. Used by
    tests now and by peer-side federation verification later (A4). Never raises on a
    bad signature — a forgery/tamper is a clean False, not an exception; only a
    structurally-unusable manifest (missing field / undecodable key) raises so a
    caller can distinguish "verified-false" from "not a manifest at all".

    NOTE: this proves the manifest is internally consistent (these bytes were signed
    by whoever holds THAT key). It does NOT establish that the key is the island you
    meant to reach — that is key distribution / TOFU, which arrives with A4's peer
    trust, not here.

    STRICT, FAIL-CLOSED — the mirror of signing.validate_origin, because this is the
    A4 peer-federation trust door and an inbound manifest is UNTRUSTED input:
      * exactly the frozen v1 key set (no missing, no unexpected keys);
      * `v` == V and `alg` == ALG, ALLOWLISTED and refused BEFORE any crypto — `v`
        and `alg` are outside the signed bytes (as with the message envelope), so a
        forged manifest can carry a valid Ed25519 signature over the identity tuple
        while advertising `alg: "none"` / `v: 999`; pinning them here is what stops
        that JWT-alg-confusion-class read;
      * the manifest fields validated by _check_identity_tuple (types, `mode`
        allowlist, `bool`-excluded u32 `key_version` and u64 `signed_at_ms` — so a
        wrong numeric cannot reach struct.pack and raise an uncaught struct.error);
      * `signed_at_ms` is INSIDE the signed bytes, so it cannot be re-dated on a
        replayed manifest; enforcing RECENCY on it is a separate policy the A4 door
        applies via ``is_fresh`` (Phase A's self-fetch must not reject on age);
      * `signature` strict-decoded to EXACTLY 64 bytes via the shared canonical
        decoder (no permissive/padded/wrong-length signature masquerading as valid).
    Only a genuine crypto MISMATCH on a well-formed manifest returns False; any
    structural malformation raises IslandIdentityError, so a caller can tell
    "verified-false" from "not a manifest at all"."""
    if not isinstance(manifest, dict):
        raise IslandIdentityError("manifest must be a dict")
    keys = set(manifest.keys())
    if keys != MANIFEST_KEYS:
        missing = MANIFEST_KEYS - keys
        extra = keys - MANIFEST_KEYS
        raise IslandIdentityError(
            f"manifest key set invalid (missing={sorted(missing)}, "
            f"unexpected={sorted(extra)})")
    # Allowlist the envelope discriminators BEFORE any crypto (they are outside the
    # signed bytes, so the signature does not protect them — a verifier must). `v` gets
    # the bool-excluded int guard (True == 1 and 1.0 == 1 would otherwise satisfy
    # `!= V`), the same discipline signing.validate_origin applies to its discriminators.
    v = manifest["v"]
    if isinstance(v, bool) or not isinstance(v, int) or v != V:
        raise IslandIdentityError(f"manifest v {v!r} unsupported (expected int {V})")
    if manifest["alg"] != ALG:
        raise IslandIdentityError(
            f"manifest alg {manifest['alg']!r} not allowed (only {ALG!r})")
    try:
        _check_identity_tuple(
            id=manifest["id"], display_name=manifest["display_name"],
            base_url=manifest["base_url"], mode=manifest["mode"],
            key_version=manifest["key_version"],
            signed_at_ms=manifest["signed_at_ms"])
        pub_str = manifest["island_pubkey"]
        sig_str = manifest["signature"]
        if not isinstance(pub_str, str) or not isinstance(sig_str, str):
            raise IslandIdentityError("island_pubkey and signature must be strings")
        if len(pub_str) > signing.MAX_PUBKEY_STR:
            raise IslandIdentityError("island_pubkey too long")
        pub_raw = signing.decode_multikey(pub_str)
        sig = signing.b64url_raw(sig_str, expect_len=SIG_LEN, field="signature")
        msg = signing_bytes(
            id=manifest["id"], display_name=manifest["display_name"],
            base_url=manifest["base_url"], mode=manifest["mode"],
            key_version=manifest["key_version"],
            signed_at_ms=manifest["signed_at_ms"])
    except signing.OriginError as e:
        raise IslandIdentityError(f"not a well-formed island manifest: {e}") from e
    try:
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(sig, msg)
        return True
    except InvalidSignature:
        return False


def is_fresh(
    manifest: dict, *, now_ms: int,
    max_age_ms: int = DEFAULT_MAX_AGE_MS,
    skew_ms: int = DEFAULT_CLOCK_SKEW_MS,
) -> bool:
    """The A4 peer-federation RECENCY policy: is this manifest's ``signed_at_ms`` close
    enough to ``now_ms`` to be a CURRENT posture rather than a replayed stale one?

    Returns True iff ``-skew_ms <= (now_ms - signed_at_ms) <= max_age_ms`` — i.e. not
    older than the freshness window AND not implausibly far in the future (a manifest
    that predates the verifier's clock by more than the tolerated skew is rejected, so
    a forged-future timestamp can't buy unbounded validity).

    A PURE function: the caller supplies ``now_ms`` (no clock inside), so it is
    deterministic and testable and the codec keeps its no-I/O contract. It is a policy
    layered ON TOP of ``verify_manifest`` — call verify FIRST; a structurally-bad
    manifest here (missing / malformed ``signed_at_ms``) RAISES IslandIdentityError
    rather than silently returning a freshness verdict, so a caller can never skip
    signature verification and mistake "unusable" for "not fresh".

    WHY recency and not strict monotonicity: recency needs no per-peer durable state
    (that store is A4's, and doesn't exist yet), and the per-request signing in
    ``rest/island.py`` makes the honest self-fetch path always-fresh for free. The
    residual — a captured manifest can be replayed for up to ``max_age_ms`` after a
    mode flip — is bounded and named; strict epoch monotonicity that kills it
    permanently is a clean future v3 when the A4 high-water store lands (#2452)."""
    if not isinstance(manifest, dict) or "signed_at_ms" not in manifest:
        raise IslandIdentityError(
            "is_fresh requires a verified manifest with signed_at_ms "
            "(call verify_manifest first)")
    ts = manifest["signed_at_ms"]
    if isinstance(ts, bool) or not isinstance(ts, int) \
            or not (0 <= ts <= MAX_SIGNED_AT_MS):
        raise IslandIdentityError("manifest signed_at_ms is not a valid timestamp")
    age = now_ms - ts
    return -skew_ms <= age <= max_age_ms
