"""Island signed self-manifest (crucible-09 Phase A, A1) — identity + endpoint tests.

The trust-boundary guarantees pinned here:
  * a manifest built by the island VERIFIES against its own declared public key;
  * ANY tamper of a signed field (mode, base_url, id, …) makes verification FAIL —
    the whole self-entry has provenance, not just `mode`;
  * the island Multikey is the canonical `z6Mk…` ed25519 shape and round-trips
    through the EXISTING message-signer decoder (a cross-implementation check, not a
    self-inverse — dodging self-referential-test blindness);
  * the seed decoder fails closed on a wrong-length / mis-encoded key;
  * `GET /v1/island` serves a manifest whose signature verifies.
"""
from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aiko_gateway.domain import island_identity as ii
from aiko_gateway.domain import signing
from aiko_gateway.main import app

# A fixed non-dev seed for deterministic unit tests (32 bytes, base64url unpadded).
_TEST_SEED = base64.urlsafe_b64encode(b"unit-test-island-seed-32-bytes!!").rstrip(b"=").decode()

_FIELDS = dict(
    id="test-island",
    display_name="Test Island",
    base_url="https://test.example",
    mode="moderator",
    key_version=1,
)


def _manifest(**over) -> dict:
    return ii.build_signed_manifest(**{**_FIELDS, **over, "seed_b64url": _TEST_SEED})


# --- signature correctness + tamper detection -------------------------------- #

def test_manifest_verifies():
    assert ii.verify_manifest(_manifest()) is True


def test_manifest_has_expected_shape():
    m = _manifest()
    assert m["v"] == ii.V and m["alg"] == ii.ALG
    assert set(m) == {"v", "alg", "id", "display_name", "base_url", "mode",
                      "key_version", "island_pubkey", "signature"}
    assert m["island_pubkey"].startswith("z6Mk")  # canonical ed25519 Multikey prefix


@pytest.mark.parametrize("field,bad", [
    ("mode", "e2ee"),                       # the mislabel we most care about
    ("base_url", "https://evil.example"),   # the field the design almost left unsigned
    ("id", "other-island"),
    ("display_name", "Impersonator"),
    ("key_version", 2),
])
def test_tamper_of_any_signed_field_fails_verification(field, bad):
    m = _manifest()
    m[field] = bad
    assert ii.verify_manifest(m) is False


def test_tampered_signature_fails():
    m = _manifest()
    # Flip the signature to a valid-length but wrong value (all-zero 64 bytes).
    m["signature"] = base64.urlsafe_b64encode(b"\x00" * 64).rstrip(b"=").decode()
    assert ii.verify_manifest(m) is False


def test_signature_from_a_different_key_fails():
    # A manifest whose FIELDS say key A but whose pubkey/signature are key B's is a
    # forgery attempt; verify must reject it. Build with a second seed, then splice
    # the first manifest's pubkey in.
    other_seed = base64.urlsafe_b64encode(b"a-different-island-seed-32-byte!").rstrip(b"=").decode()
    m = _manifest()
    m["island_pubkey"] = ii.public_multikey(ii.private_key_from_seed(other_seed))
    assert ii.verify_manifest(m) is False


def test_malformed_manifest_raises_not_returns_false():
    # A structurally-unusable manifest (missing a field) is distinguishable from a
    # verified-false one: it RAISES, so a caller can tell "not a manifest" from "bad sig".
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest({"island_pubkey": "znope", "signature": "x"})


# --- Multikey codec: cross-implementation round-trip ------------------------- #

def test_pubkey_roundtrips_through_the_message_signer_decoder():
    # encode_multikey (new) -> decode_multikey (existing, prod-exercised) == identity.
    # Uses the INDEPENDENT decoder as the oracle, not encode's own inverse.
    priv = ii.private_key_from_seed(_TEST_SEED)
    raw = priv.public_key().public_bytes_raw()
    mk = signing.encode_multikey(raw)
    assert signing.decode_multikey(mk) == raw


def test_encode_multikey_rejects_wrong_length():
    with pytest.raises(signing.OriginError):
        signing.encode_multikey(b"\x00" * 31)


# --- seed decoding: fail closed ---------------------------------------------- #

def test_decode_seed_rejects_wrong_length():
    short = base64.urlsafe_b64encode(b"too-short").rstrip(b"=").decode()
    with pytest.raises(ii.IslandIdentityError):
        ii.decode_seed(short)


def test_decode_seed_rejects_non_base64url():
    with pytest.raises(ii.IslandIdentityError):
        ii.decode_seed("not base64url! has spaces and =")


def test_decode_seed_accepts_valid():
    assert len(ii.decode_seed(_TEST_SEED)) == ii.SEED_LEN


# --- domain separation ------------------------------------------------------- #

def test_island_domain_tag_differs_from_message_signer():
    # An island-manifest signature must never be a valid message signature — the
    # domain tags being distinct is what guarantees it at the byte level.
    assert ii.DOMAIN_TAG != signing.DOMAIN_TAG
    assert ii.signing_bytes(**_FIELDS).startswith(
        __import__("struct").pack(">I", len(ii.DOMAIN_TAG)) + ii.DOMAIN_TAG.encode())


# --- the endpoint ------------------------------------------------------------ #

@pytest_asyncio.fixture
async def client():
    # ASGITransport does not trigger lifespan, so the aiko bus never starts and no DB
    # is needed — /v1/island reads only config + the process directory singleton.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_get_island_returns_a_verifying_signed_manifest(client):
    r = await client.get("/v1/island")
    assert r.status_code == 200
    m = r.json()
    # The test harness runs on the config defaults: mode=moderator, dev seed.
    assert m["mode"] == "moderator"
    assert m["island_pubkey"].startswith("z6Mk")
    assert ii.verify_manifest(m) is True


async def test_get_island_is_public_no_auth(client):
    # Identity + mode are public discovery info (like /v1/islands) — no auth header.
    r = await client.get("/v1/island")
    assert r.status_code == 200


async def test_get_island_503_when_no_self_identity(client, monkeypatch):
    # No valid self identity configured → there is nothing authentic to sign, so the
    # endpoint fails closed with 503 rather than emitting an unsigned/partial manifest.
    from aiko_gateway.domain.peers_service import directory
    monkeypatch.setattr(directory, "_self", None)
    r = await client.get("/v1/island")
    assert r.status_code == 503


# --- verify is the strict mirror of validate_origin (fail-closed) ------------ #

def test_verify_rejects_bad_alg_before_crypto():
    # A forged manifest can carry a VALID signature over the identity tuple while
    # advertising alg="none" (v/alg are outside the signed bytes) — the JWT
    # alg-confusion class. Verify must refuse it, not return True/False from crypto.
    m = _manifest()
    m["alg"] = "none"
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)


def test_verify_rejects_bad_v():
    m = _manifest()
    m["v"] = 999
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)


def test_verify_rejects_extra_and_missing_keys():
    m = _manifest()
    m["surprise"] = "x"
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)
    m2 = _manifest()
    del m2["base_url"]
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m2)


def test_verify_rejects_wrong_length_signature():
    # A 32-byte (not 64) signature must be a structural reject, not an uncaught
    # ValueError out of Ed25519.verify.
    m = _manifest()
    m["signature"] = base64.urlsafe_b64encode(b"\x00" * 32).rstrip(b"=").decode()
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)


def test_verify_rejects_padded_signature():
    # Permissive base64 would accept '='-padded / standard-alphabet; the strict
    # decoder must not.
    m = _manifest()
    m["signature"] = base64.urlsafe_b64encode(b"\x00" * 64).decode()  # keeps '=' padding
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)


def test_verify_rejects_bool_and_oob_key_version():
    # True is an int subclass that would pack as 1; an out-of-range int would raise
    # struct.error. Both must be structural rejects.
    for bad in (True, -1, 2**32):
        m = _manifest()
        m["key_version"] = bad
        with pytest.raises(ii.IslandIdentityError):
            ii.verify_manifest(m)


def test_verify_rejects_bad_mode():
    m = _manifest()
    m["mode"] = "plaintext-lol"
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)


def test_build_rejects_bad_mode_and_key_version():
    # The signing door itself refuses to mint a manifest for an unknown mode or an
    # out-of-range key_version — a Phase B helper can't sign unvalidated vocabulary.
    with pytest.raises(ii.IslandIdentityError):
        _manifest(mode="bogus")
    with pytest.raises(ii.IslandIdentityError):
        _manifest(key_version=2**32)
