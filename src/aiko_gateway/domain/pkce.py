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
import secrets


def _b64url_nopad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for the S256 method.

    verifier: 32 random bytes -> 43-char base64url (within RFC 7636's 43..128).
    challenge: BASE64URL(SHA256(verifier)).
    """
    verifier = _b64url_nopad(secrets.token_bytes(32))
    challenge = _b64url_nopad(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge
