"""The GitHub Actions OIDC verify boundary (Citizenship H1) — exercised, not asserted.

Mirrors test_oauth_verify: craft REAL RS256 tokens with a test RSA keypair and run the
actual github_oidc.verify_github_oidc_token path, injecting the public key into the
GitHub JWKS cache so no network is touched. Every attack family (alg-confusion, alg=none,
iss-spoof, expiry, unknown-kid, bad-sig, missing required claim) is a token the verifier
must REJECT — proven by running it.
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

from aiko_gateway.domain import github_oidc

_KID = "gh-test-key-1"
_ISS = "https://token.actions.githubusercontent.com"
_AUD = "aiko-island"
_REPO = "nickmeinhold/aiko-chat-app"
_REF = "refs/heads/main"
_WORKFLOW_REF = "nickmeinhold/aiko-chat-app/.github/workflows/agent.yml@refs/heads/main"


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def wired(rsa_key, monkeypatch):
    """Inject the test public key into the GitHub JWKS cache (keyed by _KID) so
    get_key resolves without network. _fetched_at = now so the fresh key isn't seen
    as stale by the max_age ceiling (which would force a real refresh)."""
    cache = github_oidc._jwks
    monkeypatch.setattr(cache, "_keys", {_KID: rsa_key.public_key()})
    monkeypatch.setattr(cache, "_fetched_at", time.monotonic())
    return rsa_key


def _now() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def _make_token(rsa_key, *, alg="RS256", kid=_KID, key=None, **overrides) -> str:
    """A well-formed GitHub Actions OIDC token, with knobs to forge each field."""
    claims = {
        "iss": _ISS,
        "aud": _AUD,
        "sub": f"repo:{_REPO}:ref:{_REF}",
        "repository": _REPO,
        "ref": _REF,
        "workflow_ref": _WORKFLOW_REF,
        "iat": _now(),
        "exp": _now() + 600,
    }
    claims.update(overrides)
    # allow deleting a claim by passing it as the sentinel ...
    for k in [k for k, v in list(claims.items()) if v is ...]:
        del claims[k]
    signing_key = key if key is not None else rsa_key
    return jwt.encode(claims, signing_key, algorithm=alg, headers={"kid": kid})


async def test_valid_token_returns_claims(wired):
    claims = await github_oidc.verify_github_oidc_token(_make_token(wired))
    assert claims.repository == _REPO
    assert claims.ref == _REF
    assert claims.workflow_ref == _WORKFLOW_REF
    assert claims.aud == _AUD
    assert claims.sub.startswith("repo:")


def _b64url(b: bytes) -> bytes:
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def _handcraft_hs256(payload: dict, secret: bytes, kid=_KID) -> str:
    """Hand-roll an HS256 JWT — the faithful alg-confusion payload (PyJWT's encode
    refuses to use an asymmetric key as an HMAC secret; a real attacker forges bytes)."""
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    seg = (_b64url(json.dumps(header).encode()) + b"."
           + _b64url(json.dumps(payload).encode()))
    sig = hmac.new(secret, seg, hashlib.sha256).digest()
    return (seg + b"." + _b64url(sig)).decode()


async def test_alg_confusion_hs256_with_public_key_rejected(wired):
    """Flip alg to HS256 and sign with the provider's PUBLIC key as the HMAC secret.
    The header alg pre-check rejects it before any key is consulted."""
    pub_pem = wired.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    forged = _handcraft_hs256(
        {"iss": _ISS, "aud": _AUD, "sub": "x", "repository": _REPO, "ref": _REF,
         "workflow_ref": _WORKFLOW_REF, "iat": _now(), "exp": _now() + 600},
        secret=pub_pem)
    with pytest.raises(github_oidc.InvalidOidcToken):
        await github_oidc.verify_github_oidc_token(forged)


async def test_alg_none_rejected(wired):
    forged = jwt.encode(
        {"iss": _ISS, "aud": _AUD, "sub": "x", "repository": _REPO, "ref": _REF,
         "workflow_ref": _WORKFLOW_REF, "iat": _now(), "exp": _now() + 600},
        key=None, algorithm="none", headers={"kid": _KID})
    with pytest.raises(github_oidc.InvalidOidcToken):
        await github_oidc.verify_github_oidc_token(forged)


async def test_bad_signature_rejected(wired):
    """A token signed by a DIFFERENT RSA key (attacker's) fails signature verify
    against the injected provider key."""
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = _make_token(wired, key=attacker)  # header kid=_KID but signed by attacker
    with pytest.raises(github_oidc.InvalidOidcToken):
        await github_oidc.verify_github_oidc_token(forged)


async def test_untrusted_issuer_rejected(wired):
    token = _make_token(wired, iss="https://evil.example.com")
    with pytest.raises(github_oidc.InvalidOidcToken):
        await github_oidc.verify_github_oidc_token(token)


async def test_expired_token_rejected(wired):
    token = _make_token(wired, iat=_now() - 1200, exp=_now() - 600)
    with pytest.raises(github_oidc.InvalidOidcToken):
        await github_oidc.verify_github_oidc_token(token)


async def test_unknown_kid_rejected(wired):
    token = _make_token(wired, kid="some-other-kid")
    with pytest.raises(github_oidc.InvalidOidcToken):
        await github_oidc.verify_github_oidc_token(token)


@pytest.mark.parametrize("missing", ["repository", "ref", "workflow_ref", "sub"])
async def test_missing_required_claim_rejected(wired, missing):
    token = _make_token(wired, **{missing: ...})  # ... deletes the claim
    with pytest.raises(github_oidc.InvalidOidcToken):
        await github_oidc.verify_github_oidc_token(token)


async def test_jwks_outage_maps_to_provider_unavailable(rsa_key, monkeypatch):
    """A JWKS fetch failure is a 503-class outage, not a bad credential."""
    cache = github_oidc._jwks
    monkeypatch.setattr(cache, "_keys", {})
    monkeypatch.setattr(cache, "_fetched_at", None)  # never fetched → refresh attempted

    async def _boom(kid):
        raise __import__("httpx").HTTPError("network down")

    monkeypatch.setattr(cache, "get_key", _boom)
    with pytest.raises(github_oidc.OidcProviderUnavailable):
        await github_oidc.verify_github_oidc_token(_make_token(rsa_key))
