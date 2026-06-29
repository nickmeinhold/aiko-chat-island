"""PKCE (RFC 7636) verifier/challenge minting for the OAuth broker (#21).

GitHub OAuth Apps don't support PKCE (the first broker provider uses the
confidential client_secret exchange instead), but the broker is built
PKCE-capable so the follow-up providers that DO support it need no plumbing
change — `build_authorize_url` includes the challenge only when the provider
declares supports_pkce.

S256 only (the `plain` method is a downgrade we never offer).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


def _b64url_nopad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def app_challenge_for(verifier: str) -> str:
    """The S256 challenge BASE64URL(SHA256(verifier)) for an APP-supplied verifier.

    Distinct from the provider-PKCE pair below: this binds the broker HANDOFF to
    the app instance that started the flow (cage-match #37, Carnot P1). The app
    generates a verifier, sends ONLY this challenge to /start, and presents the
    verifier at /exchange — so a handoff code intercepted via a hijacked custom
    scheme on Android is useless without the verifier, which never leaves the
    originating app."""
    return _b64url_nopad(hashlib.sha256(verifier.encode("ascii")).digest())


def verify_app_challenge(verifier: str, challenge: str) -> bool:
    """Constant-time check that `verifier` hashes to the stored `challenge`."""
    return hmac.compare_digest(app_challenge_for(verifier), challenge)


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for the S256 method.

    verifier: 32 random bytes -> 43-char base64url (within RFC 7636's 43..128).
    challenge: BASE64URL(SHA256(verifier)).
    """
    verifier = _b64url_nopad(secrets.token_bytes(32))
    challenge = _b64url_nopad(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge
