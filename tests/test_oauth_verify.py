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


def test_registry_keyed_by_provider_enum():
    # The refactor's structural invariant (RED on the old string-keyed registry,
    # GREEN now): every _PROVIDERS key is a Provider member, and both members are
    # present. This is what actually proves the rekey — passing the enum or a junk
    # value already behaved correctly under the old StrEnum string keys.
    assert set(oauth._PROVIDERS) == set(oauth.Provider)
    assert all(isinstance(k, oauth.Provider) for k in oauth._PROVIDERS)


async def test_enum_provider_accepted(wired_google):
    # Contract lock: passing the Provider enum (as rest/auth.py does — req.provider
    # is already coerced by Pydantic) verifies identically to the raw string.
    token = _make_token(wired_google)
    identity = await oauth.verify_id_token(oauth.Provider.google, token)
    assert identity.provider == "google"
    assert identity.sub == "google-sub-123"


async def test_unknown_provider_coercion_fails_closed(wired_google):
    # Contract lock: a value outside the closed set must surface as UnknownProvider
    # (a clean 4xx), never the bare ValueError that Provider("...") raises — which
    # would otherwise escape as a 500. Covers a non-string junk value too.
    token = _make_token(wired_google)
    with pytest.raises(oauth.UnknownProvider):
        await oauth.verify_id_token("microsoft", token)
    with pytest.raises(oauth.UnknownProvider):
        await oauth.verify_id_token(123, token)  # type: ignore[arg-type]


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


async def test_jwks_first_refresh_not_starved_at_low_uptime(monkeypatch):
    """REGRESSION: the never-fetched sentinel (`_fetched_at == 0.0`) must bypass the
    floor. `time.monotonic()`'s zero point is near host boot, so on a low-uptime host
    (a fresh CI runner, a just-booted container) `monotonic() - 0.0` can be BELOW the
    floor and wrongly block the very FIRST JWKS fetch — failing every token closed
    until uptime exceeds min_refresh_interval. We pin monotonic() below the floor so
    this is caught EVERYWHERE, not only on a fresh runner: the two floor tests around
    this one pass on a high-uptime dev box for the wrong reason (monotonic happens to
    exceed the floor), which is exactly how this bug reached main green-locally."""
    monkeypatch.setattr(oauth.time, "monotonic", lambda: 5.0)  # 5s "uptime" << floor
    cache = oauth._JwksCache("http://x", min_refresh_interval=1000.0, max_age=10000.0)
    calls = {"n": 0}

    async def fake_refresh():
        calls["n"] += 1
        cache._keys = {"kid-0": object()}
        cache._fetched_at = oauth.time.monotonic()

    cache._refresh = fake_refresh
    assert await cache.get_key("kid-0") is not None  # first fetch NOT starved by the floor
    assert calls["n"] == 1


@pytest.mark.parametrize("bad_body", [
    [],                    # bare list, not an object
    {"keys": None},        # keys present but null
    {"keys": "nope"},      # keys not a list
    {"keys": ["bad"]},     # a non-object key entry
    {"no_keys_field": 1},  # missing keys → treated as empty → kid never resolves
])
async def test_malformed_jwks_shape_does_not_500(wired_google, monkeypatch, bad_body):
    """Valid-JSON-wrong-SHAPE must fail closed as 503/401, never crash with an
    uncaught AttributeError/TypeError (→ 500). cage-match PR#15 r2, Carnot."""
    cache = oauth._PROVIDERS["google"].jwks

    async def fake_refresh():
        # Mimic the real _refresh's shape validation over the bad body.
        if not isinstance(bad_body, dict):
            raise ValueError("not an object")
        keys = bad_body.get("keys")
        if "keys" in bad_body and not isinstance(keys, list):
            raise ValueError("keys not a list")
        cache._fetched_at = time.monotonic()  # a successful (possibly empty) refresh

    monkeypatch.setattr(cache, "_keys", {})
    monkeypatch.setattr(cache, "_fetched_at", 0.0)
    monkeypatch.setattr(cache, "_refresh", fake_refresh)
    token = _make_token(wired_google, kid="any-kid")
    with pytest.raises((oauth.ProviderUnavailable, oauth.InvalidProviderToken)):
        await oauth.verify_id_token("google", token)


async def test_real_refresh_rejects_malformed_shapes(monkeypatch):
    """Drive the REAL _refresh (not a stub) against bad shapes by stubbing only
    the HTTP layer — proves the shape guards live in production code, not the test."""
    import httpx

    class _Resp:
        def __init__(self, payload): self._payload = payload
        def raise_for_status(self): pass
        def json(self): return self._payload

    for bad in ([], {"keys": None}, {"keys": "x"}):
        cache = oauth._JwksCache("http://x")

        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url): return _Resp(bad)

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        with pytest.raises(ValueError):
            await cache._refresh()


async def test_jwks_cache_floor_starves_rotation_within_window():
    """ACCEPTED tradeoff: a NEW (legit) kid arriving within the floor window after
    a recent refresh is NOT refreshed — it fails closed (returns None → 401).
    Documents the amplification/rotation tension. cage-match PR#15 r2, Carnot."""
    cache = oauth._JwksCache("http://x", min_refresh_interval=1000.0, max_age=10000.0)
    calls = {"n": 0}

    async def fake_refresh():
        calls["n"] += 1
        cache._keys = {"old-kid": object()}
        cache._fetched_at = time.monotonic()

    cache._refresh = fake_refresh
    assert await cache.get_key("old-kid") is not None and calls["n"] == 1  # initial refresh
    # A freshly-rotated new kid within the floor window: no refresh, fail closed.
    assert await cache.get_key("new-rotated-kid") is None
    assert calls["n"] == 1  # floor blocked the refresh — the accepted 401 window


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


# --- nonce binding / replay defense (#13) ----------------------------------- #
# These exercise the REAL provider-aware comparison: Google echoes the nonce raw,
# Apple echoes SHA-256(nonce) hex. The app always supplies the RAW nonce.

_APPLE_ISS = "https://appleid.apple.com"
_APPLE_AUD = "cc.imagineering.aikoChatApp"


@pytest.fixture
def wired_apple(rsa_key, monkeypatch):
    """Mirror of wired_google for the Apple provider (hashed-nonce path)."""
    monkeypatch.setattr(settings, "apple_client_ids", [_APPLE_AUD])
    cache = oauth._PROVIDERS["apple"].jwks
    monkeypatch.setattr(cache, "_keys", {_KID: rsa_key.public_key()})
    monkeypatch.setattr(cache, "_fetched_at", time.monotonic())
    return rsa_key


async def test_nonce_not_supplied_skips_check(wired_google):
    """Back-compat: no expected_nonce ⇒ the claim is never inspected (today's app
    path). A token WITHOUT a nonce claim still verifies."""
    token = _make_token(wired_google)  # no nonce claim
    identity = await oauth.verify_id_token("google", token)
    assert identity.sub == "google-sub-123"


async def test_google_raw_nonce_matches(wired_google):
    token = _make_token(wired_google, nonce="nonce-abc")
    identity = await oauth.verify_id_token(
        "google", token, expected_nonce="nonce-abc")
    assert identity.sub == "google-sub-123"


async def test_google_nonce_mismatch_rejected(wired_google):
    token = _make_token(wired_google, nonce="nonce-abc")
    with pytest.raises(oauth.InvalidProviderToken):
        await oauth.verify_id_token("google", token, expected_nonce="WRONG")


async def test_apple_hashed_nonce_matches(wired_apple):
    """Apple echoes SHA-256(raw) hex; the app supplies the RAW nonce and the
    verifier must hash before comparing. A naive equality check would reject this."""
    raw = "device-random-xyz"
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    token = _make_token(wired_apple, iss=_APPLE_ISS, aud=_APPLE_AUD,
                        sub="apple-sub-1", nonce=hashed)
    identity = await oauth.verify_id_token(
        "apple", token, expected_nonce=raw)
    assert identity.sub == "apple-sub-1"


async def test_apple_raw_nonce_in_claim_rejected(wired_apple):
    """Defends the asymmetry: if an Apple token carried the RAW nonce (wrong shape,
    or a downgrade attempt), the hashed comparison rejects it."""
    raw = "device-random-xyz"
    token = _make_token(wired_apple, iss=_APPLE_ISS, aud=_APPLE_AUD,
                        sub="apple-sub-1", nonce=raw)  # raw, not hashed
    with pytest.raises(oauth.InvalidProviderToken):
        await oauth.verify_id_token("apple", token, expected_nonce=raw)


async def test_expected_nonce_but_token_has_none_rejected(wired_google):
    """The replay case: an attacker replays a token that carries NO nonce while
    enforcement expects one. A missing claim can never satisfy the expectation."""
    token = _make_token(wired_google)  # no nonce claim
    with pytest.raises(oauth.InvalidProviderToken):
        await oauth.verify_id_token("google", token, expected_nonce="nonce-abc")


async def test_non_ascii_nonce_fails_closed_not_typeerror(wired_google):
    """Kelvin (cage-match PR#32): hmac.compare_digest on str raises TypeError for
    non-ASCII — a crafted token nonce would 500 instead of 401. Byte comparison
    must fail CLOSED (InvalidProviderToken), never TypeError."""
    token = _make_token(wired_google, nonce="café-über")  # non-ASCII claim
    with pytest.raises(oauth.InvalidProviderToken):
        await oauth.verify_id_token("google", token, expected_nonce="mismatch")


async def test_non_ascii_nonce_matches_on_bytes(wired_google):
    """The same non-ASCII value compares equal once both sides are utf-8 bytes."""
    raw = "café-über"
    token = _make_token(wired_google, nonce=raw)
    identity = await oauth.verify_id_token("google", token, expected_nonce=raw)
    assert identity.sub == "google-sub-123"
