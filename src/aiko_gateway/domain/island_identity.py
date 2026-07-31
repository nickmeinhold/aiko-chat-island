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


class IslandIdentityError(ValueError):
    """A malformed island signing seed (wrong length / not decodable). Raised at
    config/boot time so a broken identity key fails closed, never serves an island
    that cannot sign its own manifest."""


def decode_seed(seed_b64url: str) -> bytes:
    """Decode the configured ``ISLAND_SIGNING_SEED`` (unpadded base64url of 32 raw
    bytes) into the raw seed. Raises IslandIdentityError on anything that is not a
    32-byte value — so a truncated / mis-encoded seed refuses boot rather than
    silently deriving a different key than the operator intended."""
    s = (seed_b64url or "").strip()
    if not signing._B64URL_UNPADDED_RE.match(s):
        raise IslandIdentityError(
            "ISLAND_SIGNING_SEED must be unpadded base64url ([A-Za-z0-9_-], no '=')")
    try:
        raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError) as e:
        raise IslandIdentityError("ISLAND_SIGNING_SEED is not valid base64url") from e
    if len(raw) != SEED_LEN:
        raise IslandIdentityError(
            f"ISLAND_SIGNING_SEED decodes to {len(raw)} bytes, expected {SEED_LEN}")
    return raw


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


def build_signed_manifest(
    *, id: str, display_name: str, base_url: str, mode: str,
    key_version: int, seed_b64url: str,
) -> dict:
    """Build THIS island's signed self-manifest: the identity tuple + the island
    public Multikey + an Ed25519 signature over ``signing_bytes(...)``. Pure w.r.t.
    its inputs (no I/O) so the endpoint can cache it and tests can build one from
    arbitrary fields. The signature is unpadded base64url — the same encoding the
    message signer uses for ``origin.sig``."""
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
    trust, not here."""
    try:
        pub_raw = signing.decode_multikey(manifest["island_pubkey"])
        sig_str = manifest["signature"]
        sig = base64.urlsafe_b64decode(sig_str + "=" * (-len(sig_str) % 4))
        msg = signing_bytes(
            id=manifest["id"],
            display_name=manifest["display_name"],
            base_url=manifest["base_url"],
            mode=manifest["mode"],
            key_version=manifest["key_version"],
        )
    except (KeyError, TypeError, ValueError, signing.OriginError) as e:
        raise IslandIdentityError(f"not a well-formed island manifest: {e}") from e
    try:
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(sig, msg)
        return True
    except InvalidSignature:
        return False
