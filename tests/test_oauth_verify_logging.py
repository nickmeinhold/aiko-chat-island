"""Social-verify observability (#1491).

`oauth.verify_id_token` flattens every distinct rejection into one
`InvalidProviderToken("...")` that the handler maps to a generic
`401 invalid provider token` — which is exactly why the on-device Google failure
could not be diagnosed from the outside. These tests prove the verifier now emits
a structured `social.verify:` trace naming the REAL reason at each fail-closed
site (so one `docker logs -f` line answers "which check rejected this token"),
and that the trace logs only SAFE shapes — never the full nonce, token, or key
material.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from aiko_gateway.config import settings
from aiko_gateway.domain import oauth

OAUTH_LOGGER = "aiko_gateway.domain.oauth"

_KID = "test-key-1"
_GOOGLE_ISS = "https://accounts.google.com"
_GOOGLE_AUD = "my-google-client-id.apps.googleusercontent.com"


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def wired_google(rsa_key, monkeypatch):
    """Allowlist the test google client id + inject the test public key into the
    google JWKS cache so get_key resolves without network (mirrors
    test_oauth_verify.wired_google)."""
    monkeypatch.setattr(settings, "google_client_ids", [_GOOGLE_AUD])
    cache = oauth._PROVIDERS["google"].jwks
    monkeypatch.setattr(cache, "_keys", {_KID: rsa_key.public_key()})
    monkeypatch.setattr(cache, "_fetched_at", time.monotonic())
    return rsa_key


def _now() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def _make_token(rsa_key, *, kid=_KID, **overrides) -> str:
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
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": kid})


async def test_valid_token_logs_ok(wired_google, caplog):
    """A successful verify emits one `social.verify: OK` line with a safe sub
    prefix — never the email or full sub."""
    token = _make_token(wired_google)
    with caplog.at_level("INFO", logger=OAUTH_LOGGER):
        await oauth.verify_id_token("google", token)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "social.verify: OK provider=Provider.google" in joined or \
           "social.verify: OK provider=google" in joined, joined
    assert "user@example.com" not in joined  # email never logged
    assert "google-sub-123" not in joined    # full sub never logged (prefix only)


async def test_untrusted_issuer_logs_reason(wired_google, caplog):
    """A bad iss now names itself in the trace instead of hiding behind the
    generic 401."""
    token = _make_token(wired_google, iss="https://evil.example.com")
    with caplog.at_level("WARNING", logger=OAUTH_LOGGER):
        with pytest.raises(oauth.InvalidProviderToken):
            await oauth.verify_id_token("google", token)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "social.verify: REJECT" in joined
    assert "untrusted issuer" in joined


async def test_expired_token_logs_jwt_reason(wired_google, caplog):
    """An expired token surfaces PyJWT's own reason (safe — no token material)."""
    token = _make_token(wired_google, exp=_now() - 10, iat=_now() - 600)
    with caplog.at_level("WARNING", logger=OAUTH_LOGGER):
        with pytest.raises(oauth.InvalidProviderToken):
            await oauth.verify_id_token("google", token)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "social.verify: REJECT" in joined
    assert "jwt.decode failed" in joined


async def test_nonce_mismatch_logs_shape_not_value(wired_google, caplog):
    """The #1491 linchpin line: a nonce mismatch logs the SHAPE (lengths +
    prefixes + nonce_hashed) of both sides so a hashed-vs-raw transform
    disagreement is distinguishable from a value mismatch — but NEVER the full
    nonce of either side."""
    expected_nonce = "raw-app-nonce-aaaaaaaaaaaaaaaaaaaa-SECRET"
    token_nonce = "totally-different-bbbbbbbbbbbbbbbbbbbb-SECRET"
    token = _make_token(wired_google, nonce=token_nonce)
    with caplog.at_level("WARNING", logger=OAUTH_LOGGER):
        with pytest.raises(oauth.InvalidProviderToken):
            await oauth.verify_id_token(
                "google", token, expected_nonce=expected_nonce)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "social.verify: REJECT" in joined and "nonce mismatch" in joined
    assert "nonce_hashed=False" in joined          # Google echoes raw
    assert f"len={len(expected_nonce)}" in joined  # shape is logged
    assert f"len={len(token_nonce)}" in joined
    assert expected_nonce not in joined            # ...but never the full values
    assert token_nonce not in joined


async def test_nonce_present_in_token_but_none_expected_does_not_log_mismatch(
        wired_google, caplog):
    """Today's app sends no expected_nonce (social_nonce_required=False default);
    a token carrying a nonce must still verify OK and NOT trip the mismatch trace
    — guards against a regression that would reject every current Google login."""
    token = _make_token(wired_google, nonce="some-nonce-the-app-didnt-send")
    with caplog.at_level("INFO", logger=OAUTH_LOGGER):
        await oauth.verify_id_token("google", token)  # expected_nonce=None
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "nonce mismatch" not in joined
    assert "social.verify: OK" in joined
    assert "nonce_checked=False" in joined
