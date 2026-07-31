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

# The domain tag: distinct from signing.DOMAIN_TAG so an island-manifest signature
# is not a valid message signature under any circumstances (domain separation).
DOMAIN_TAG = "aikochat:island:v1:EdDSA"
ALG = "EdDSA"          # the ONLY alg — allowlist, mirroring signing.ALG
V = 1                  # manifest envelope version (a change is a v2, never a silent add)

# The Ed25519 seed is exactly 32 bytes; a public key is 32 bytes; a signature 64.
SEED_LEN = 32
SIG_LEN = 64

# The modes the manifest may carry. `e2ee` is schema-reserved (Phase B) and refused
# at BOOT by config._harden_for_production; it is still a *valid vocabulary* value a
# signed manifest could name, so verify allowlists both and lets the boot guard — not
# the codec — own the Phase-A-only policy. Anything else is a malformed manifest.
VALID_MODES = frozenset({"moderator", "e2ee"})
# key_version is packed as a big-endian u32 in the signing bytes.
KEY_VERSION_MAX = 2**32 - 1
# The exact key set of a v1 signed manifest (frozen; a change is a v2, never a silent
# add) — mirrors signing._REQUIRED_KEYS for the message envelope.
MANIFEST_KEYS = frozenset({
    "v", "alg", "id", "display_name", "base_url", "mode", "key_version",
    "island_pubkey", "signature",
})


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
    *, id: str, display_name: str, base_url: str, mode: str, key_version: int
) -> bytes:
    """The canonical, domain-separated, length-prefixed bytes the island signature is
    computed over. Every variable-length field is preceded by a big-endian u32
    length; ``key_version`` is a fixed-width big-endian u32 (no length prefix). A
    verifier reconstructing different bytes for the same manifest is non-conformant —
    exactly what the manifest round-trip test guards."""
    def lp(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b

    return b"".join((
        lp(DOMAIN_TAG.encode()),
        lp(id.encode()),
        lp(display_name.encode()),
        lp(base_url.encode()),
        lp(mode.encode()),
        struct.pack(">I", key_version),
    ))


def _check_identity_tuple(
    *, id: str, display_name: str, base_url: str, mode: str, key_version: int
) -> None:
    """Fail-closed validation of the signed identity tuple, shared by the emit
    (build) and verify doors so they can never drift apart. Mirrors
    signing.validate_origin's discipline: exact types, an allowlisted `mode` (never a
    free-text string at the crypto door), and a `bool`-excluded u32 `key_version`
    (`True` is an int subclass that would otherwise pack as 1). Raises
    IslandIdentityError on any violation — the caller decides whether that is a boot
    refusal (build) or a rejected manifest (verify)."""
    for name, val in (("id", id), ("display_name", display_name),
                      ("base_url", base_url), ("mode", mode)):
        if not isinstance(val, str):
            raise IslandIdentityError(f"manifest {name} must be a string")
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


def build_signed_manifest(
    *, id: str, display_name: str, base_url: str, mode: str,
    key_version: int, seed_b64url: str,
) -> dict:
    """Build THIS island's signed self-manifest: the identity tuple + the island
    public Multikey + an Ed25519 signature over ``signing_bytes(...)``. Pure w.r.t.
    its inputs (no I/O) so the endpoint can cache it and tests can build one from
    arbitrary fields. The signature is unpadded base64url — the same encoding the
    message signer uses for ``origin.sig``."""
    _check_identity_tuple(id=id, display_name=display_name, base_url=base_url,
                          mode=mode, key_version=key_version)
    priv = private_key_from_seed(seed_b64url)
    msg = signing_bytes(
        id=id, display_name=display_name, base_url=base_url,
        mode=mode, key_version=key_version)
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
      * the identity tuple validated by _check_identity_tuple (types, `mode`
        allowlist, `bool`-excluded u32 `key_version` — so a wrong `key_version`
        cannot reach struct.pack and raise an uncaught struct.error);
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
    # signed bytes, so the signature does not protect them — a verifier must).
    if manifest["v"] != V:
        raise IslandIdentityError(f"manifest v {manifest['v']!r} unsupported (expected {V})")
    if manifest["alg"] != ALG:
        raise IslandIdentityError(
            f"manifest alg {manifest['alg']!r} not allowed (only {ALG!r})")
    try:
        _check_identity_tuple(
            id=manifest["id"], display_name=manifest["display_name"],
            base_url=manifest["base_url"], mode=manifest["mode"],
            key_version=manifest["key_version"])
        pub_str = manifest["island_pubkey"]
        sig_str = manifest["signature"]
        if not isinstance(pub_str, str) or not isinstance(sig_str, str):
            raise IslandIdentityError("island_pubkey and signature must be strings")
        pub_raw = signing.decode_multikey(pub_str)
        sig = signing.b64url_raw(sig_str, expect_len=SIG_LEN, field="signature")
        msg = signing_bytes(
            id=manifest["id"], display_name=manifest["display_name"],
            base_url=manifest["base_url"], mode=manifest["mode"],
            key_version=manifest["key_version"])
    except signing.OriginError as e:
        raise IslandIdentityError(f"not a well-formed island manifest: {e}") from e
    try:
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(sig, msg)
        return True
    except InvalidSignature:
        return False
