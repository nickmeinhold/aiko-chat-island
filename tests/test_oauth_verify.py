"""The social-verify security boundary (#13) — exercised, not asserted.

These craft REAL RS256 tokens with a test RSA keypair and run the actual
`oauth.verify_id_token` path, injecting the public key into the provider JWKS
cache so no network is touched. Every attack family the cage-match cares about
(alg-confusion, aud-bypass, iss-spoof, expiry, unknown-kid, outage) is a token
the verifier must REJECT — proven by running it, not by reading the code.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from aiko_gateway.config import settings
from aiko_gateway.domain import oauth

_KID = "test-key-1"
_GOOGLE_ISS = "https://accounts.google.com"
_GOOGLE_AUD = "my-google-client-id.apps.googleusercontent.com"


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def wired_google(rsa_key, monkeypatch):
    """Allowlist our google client id + inject the test public key into the
    google JWKS cache (keyed by _KID) so get_key resolves without network.
    _fetched_at is set to NOW so the freshly-injected key isn't seen as stale by
    the max_age ceiling (which would otherwise force a real network refresh and
    wipe the test key)."""
    monkeypatch.setattr(settings, "google_client_ids", [_GOOGLE_AUD])
    cache = oauth._PROVIDERS["google"].jwks
    monkeypatch.setattr(cache, "_keys", {_KID: rsa_key.public_key()})
    monkeypatch.setattr(cache, "_fetched_at", time.monotonic())
    return rsa_key


def _now() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def _make_token(rsa_key, *, alg="RS256", kid=_KID, key=None, **overrides) -> str:
    """A well-formed Google ID token, with knobs to forge each field."""
    claims = {
        "iss": _GOOGLE_ISS,
        "aud": _GOOGLE_AUD,
        "sub": "google-sub-123",
        "email": "user@example.com",
        "name": "Test User",
        "iat": _now(),
        "exp": _now() + 600,
    }
    claims.update(overrides)
    headers = {"kid": kid}
    signing_key = key if key is not None else rsa_key
    return jwt.encode(claims, signing_key, algorithm=alg, headers=headers)


async def test_valid_token_returns_identity(wired_google):
    token = _make_token(wired_google)
    identity = await oauth.verify_id_token("google", token)
    assert identity.provider == "google"
    assert identity.sub == "google-sub-123"
    assert identity.email == "user@example.com"
    assert identity.suggested_name == "Test User"


def _b64url(b: bytes) -> bytes:
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def _handcraft_hs256(payload: dict, secret: bytes, kid=_KID) -> str:
    """Hand-roll an HS256 JWT with raw HMAC — bypasses PyJWT's encode-side guard
    that refuses to use an asymmetric key as an HMAC secret. A real attacker
    forges bytes directly, so this is the faithful alg-confusion payload."""
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    seg = (_b64url(json.dumps(header).encode()) + b"."
           + _b64url(json.dumps(payload).encode()))
    sig = hmac.new(secret, seg, hashlib.sha256).digest()
    return (seg + b"." + _b64url(sig)).decode()


async def test_alg_confusion_hs256_with_public_key_rejected(wired_google):
    """The classic attack: flip alg to HS256 and sign with the provider's PUBLIC
    key as the HMAC secret, hoping the verifier uses that public key to verify an
    HMAC. Our header alg pre-check rejects it before any key is consulted — and
    this hand-crafts the token (PyJWT itself refuses to BUILD it), so the test
    proves the DEFENDER rejects it, not that PyJWT won't construct it."""
    pub_pem = wired_google.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    forged = _handcraft_hs256(
        {"iss": _GOOGLE_ISS, "aud": _GOOGLE_AUD, "sub": "attacker",
         "iat": _now(), "exp": _now() + 600},
        secret=pub_pem,
    )
    with pytest.raises(oauth.InvalidProviderToken):
        await oauth.verify_id_token("google", forged)


async def test_alg_none_rejected(wired_google):
    forged = jwt.encode(
        {"iss": _GOOGLE_ISS, "aud": _GOOGLE_AUD, "sub": "x",
         "iat": _now(), "exp": _now() + 600},
        key=None, algorithm="none", headers={"kid": _KID},
    )
    with pytest.raises(oauth.InvalidProviderToken):
        await oauth.verify_id_token("google", forged)


async def test_wrong_signing_key_rejected(wired_google):
    """A correctly-RS256 token signed by an attacker's OWN key fails signature
    verification against the cached public key."""
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _make_token(wired_google, key=attacker)
    with pytest.raises(oauth.InvalidProviderToken):
        await oauth.verify_id_token("google", token)


async def test_aud_mismatch_rejected(wired_google):
    token = _make_token(wired_google, aud="some-other-app.apps.googleusercontent.com")
    with pytest.raises(oauth.InvalidProviderToken):
        await oauth.verify_id_token("google", token)


async def test_empty_aud_allowlist_rejects_all(wired_google, monkeypatch):
    """Fail-closed: no configured client IDs ⇒ reject every token, even a
    perfectly-signed one."""
    monkeypatch.setattr(settings, "google_client_ids", [])
    token = _make_token(wired_google)
    with pytest.raises(oauth.InvalidProviderToken):
        await oauth.verify_id_token("google", token)


async def test_untrusted_issuer_rejected(wired_google):
    token = _make_token(wired_google, iss="https://evil.example.com")
    with pytest.raises(oauth.InvalidProviderToken):
        await oauth.verify_id_token("google", token)


async def test_expired_token_rejected(wired_google):
    token = _make_token(wired_google, iat=_now() - 1200, exp=_now() - 600)
    with pytest.raises(oauth.InvalidProviderToken):
        await oauth.verify_id_token("google", token)


async def test_google_accepts_bare_issuer_spelling(wired_google):
    """Google legitimately uses BOTH 'accounts.google.com' and the https form."""
    token = _make_token(wired_google, iss="accounts.google.com")
    identity = await oauth.verify_id_token("google", token)
    assert identity.sub == "google-sub-123"


async def test_unknown_kid_rejected_after_single_refresh(wired_google, monkeypatch):
    """A token with a kid not in the cache triggers exactly one refresh; if the
    refreshed set still lacks it, fail closed (no loop, no per-request refetch)."""
    cache = oauth._PROVIDERS["google"].jwks
    refreshes = {"n": 0}

    async def fake_refresh():
        refreshes["n"] += 1  # refresh runs but doesn't add the bogus kid

    monkeypatch.setattr(cache, "_keys", {})  # force a miss
    monkeypatch.setattr(cache, "_fetched_at", 0.0)  # ensure the floor permits a refresh
    monkeypatch.setattr(cache, "_refresh", fake_refresh)
    token = _make_token(wired_google, kid="bogus-kid")
    with pytest.raises(oauth.InvalidProviderToken):
        await oauth.verify_id_token("google", token)
    assert refreshes["n"] == 1


async def test_provider_outage_maps_to_unavailable(wired_google, monkeypatch):
    """A JWKS fetch failure is an OUTAGE (→ ProviderUnavailable → 503), never a
    bad-credential (401)."""
    import httpx
    cache = oauth._PROVIDERS["google"].jwks

    async def boom():
        raise httpx.ConnectError("jwks endpoint down")

    monkeypatch.setattr(cache, "_keys", {})
    monkeypatch.setattr(cache, "_fetched_at", 0.0)
    monkeypatch.setattr(cache, "_refresh", boom)
    token = _make_token(wired_google, kid="any-kid")
    with pytest.raises(oauth.ProviderUnavailable):
        await oauth.verify_id_token("google", token)


async def test_malformed_jwks_body_maps_to_unavailable(wired_google, monkeypatch):
    """A JWKS endpoint returning a non-JSON body makes resp.json() raise
    ValueError — a provider-side failure (503), not a bad credential (401)."""
    cache = oauth._PROVIDERS["google"].jwks

    async def bad_json():
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(cache, "_keys", {})
    monkeypatch.setattr(cache, "_fetched_at", 0.0)
    monkeypatch.setattr(cache, "_refresh", bad_json)
    token = _make_token(wired_google, kid="any-kid")
    with pytest.raises(oauth.ProviderUnavailable):
        await oauth.verify_id_token("google", token)


async def test_unknown_provider_rejected(wired_google):
    token = _make_token(wired_google)
    with pytest.raises(oauth.UnknownProvider):
        await oauth.verify_id_token("facebook", token)


# --- JWKS cache time-policy (cage-match PR#15: floor + ceiling) ------------- #

async def test_jwks_cache_floor_bounds_bogus_kid_amplification():
    """The FLOOR: a storm of bogus kids must trigger at most ONE refresh per
    window, not one fetch per request (the unauthenticated amplification DoS)."""
    cache = oauth._JwksCache("http://x", min_refresh_interval=1000.0, max_age=10000.0)
    calls = {"n": 0}

    async def fake_refresh():
        calls["n"] += 1
        cache._fetched_at = time.monotonic()  # a successful (empty) refresh

    cache._refresh = fake_refresh
    for i in range(6):
        assert await cache.get_key(f"bogus-{i}") is None
    assert calls["n"] == 1  # first miss refreshed; floor blocked the next five


async def test_jwks_cache_ceiling_forces_refresh_when_stale():
    """The CEILING: once the cached set is older than max_age, even a kid HIT
    forces a refresh so a revoked/rotated key stops being trusted."""
    cache = oauth._JwksCache("http://x", min_refresh_interval=0.0, max_age=100.0)
    calls = {"n": 0}

    async def fake_refresh():
        calls["n"] += 1
        cache._keys = {"k1": object()}
        cache._fetched_at = time.monotonic()

    cache._refresh = fake_refresh
    assert await cache.get_key("k1") is not None and calls["n"] == 1  # miss → refresh
    await cache.get_key("k1")
    assert calls["n"] == 1                                            # fresh hit → no refresh
    cache._fetched_at = time.monotonic() - 200                       # now stale (> max_age)
    await cache.get_key("k1")
    assert calls["n"] == 2                                            # stale hit → refresh
