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
import re
import secrets

# A base64url-nopad SHA-256 digest is EXACTLY 43 url-safe chars. Validating the
# app_challenge against this at ingress (cage-match #37 r2, Carnot LOW) rejects
# wrong-length / non-b64url / non-ASCII before it is ever stored, so the binding
# store only ever holds well-formed challenges (defence-in-depth on top of the
# fail-closed verify_app_challenge below).
_S256_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def _b64url_nopad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def is_valid_app_challenge(challenge: str) -> bool:
    """True iff `challenge` has the exact shape of a base64url-nopad SHA-256 digest
    (43 url-safe chars). Used to fail-closed-reject a malformed app_challenge at
    /start ingress rather than storing it for a later 401."""
    return bool(_S256_CHALLENGE_RE.match(challenge))


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
    """Constant-time check that `verifier` hashes to the stored `challenge`.

    TOTAL + fail-closed on malformed input (cage-match #37 / the PR#32 precedent):
    both `verifier` (from the /exchange body) and `challenge` (stored from /start)
    are attacker-influenced. A non-ASCII `verifier` would blow up `.encode("ascii")`
    inside app_challenge_for, and a non-ASCII `challenge` would make hmac.compare_
    digest raise TypeError — either an unhandled 500 on the auth path. Neither can
    ever be a legitimate S256 base64url value, so we reject them as a non-match
    rather than letting them raise. compare_digest then only ever sees two ASCII
    strings (the secret comparison itself stays constant-time)."""
    try:
        computed = app_challenge_for(verifier)
    except UnicodeEncodeError:
        return False  # a non-ASCII verifier can't hash to an ASCII b64url challenge
    if not challenge.isascii():
        return False  # a non-ASCII challenge is not a valid S256 b64url digest
    return hmac.compare_digest(computed, challenge)


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for the S256 method.

    verifier: 32 random bytes -> 43-char base64url (within RFC 7636's 43..128).
    challenge: BASE64URL(SHA256(verifier)).
    """
    verifier = _b64url_nopad(secrets.token_bytes(32))
    challenge = _b64url_nopad(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge
