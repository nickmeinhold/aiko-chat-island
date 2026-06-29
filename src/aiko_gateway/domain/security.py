"""Auth primitives: argon2id password hashing + JWT issue/verify.

Tokens carry only `sub` (user_id) + `type` (access|refresh) + expiry — NOT
roles. Roles/membership are read live from Postgres on each request (plan §A3)
so a revoked membership takes effect immediately, not at next token refresh.
"""
from __future__ import annotations

import datetime as dt

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from ..config import settings

_ph = PasswordHasher()  # argon2id defaults


def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, plain)
    except VerifyMismatchError:
        return False


def _issue(user_id: str, token_type: str, ttl_seconds: int) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": user_id,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(seconds=ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def issue_access(user_id: str) -> str:
    return _issue(user_id, "access", settings.jwt_access_ttl_seconds)


def issue_refresh(user_id: str) -> str:
    return _issue(user_id, "refresh", settings.jwt_refresh_ttl_seconds)


def decode_token(token: str, *, expected_type: str) -> str:
    """Return the user_id (sub) if valid and of the expected type, else raise
    jwt.InvalidTokenError."""
    payload = jwt.decode(
        token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"expected {expected_type} token")
    sub = payload.get("sub")
    if not sub:
        raise jwt.InvalidTokenError("missing sub")
    return sub


# --- social sign-in provisioning token (#13) ------------------------------- #
# A brand-new social user has no local account yet, so the "pending" state can't
# be a User row (username + aiko_username are NOT NULL UNIQUE). Instead the verify
# step mints this short-lived token — signed by US with the SAME HS256 secret, so
# the client cannot forge the (provider, provider_sub) it carries — and the claim
# step verifies it and creates the user atomically. No DB row, no TTL sweeper.
_PROVISIONING_TYPE = "provisioning"


def issue_provisioning(
    provider: str, provider_sub: str, *,
    suggested_name: str | None = None, email: str | None = None,
) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "type": _PROVISIONING_TYPE,
        "provider": provider,
        "provider_sub": provider_sub,
        "suggested_name": suggested_name,
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(
            seconds=settings.provisioning_ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_provisioning(token: str) -> dict:
    """Return {provider, provider_sub, suggested_name, email} from a valid
    provisioning token, else raise jwt.InvalidTokenError. Verified with OUR
    secret + algorithm — never RS256, never JWKS (this is our token, not a
    provider's)."""
    payload = jwt.decode(
        token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )
    if payload.get("type") != _PROVISIONING_TYPE:
        raise jwt.InvalidTokenError("expected provisioning token")
    provider = payload.get("provider")
    provider_sub = payload.get("provider_sub")
    if not provider or not provider_sub:
        raise jwt.InvalidTokenError("missing provider identity")
    return {
        "provider": provider,
        "provider_sub": provider_sub,
        "suggested_name": payload.get("suggested_name"),
        "email": payload.get("email"),
    }


# --- OAuth broker state token (#21) ---------------------------------------- #
# The broker's authorization-code flow has a /start -> provider -> /callback
# round-trip. The `state` parameter carries integrity across it. We make state a
# short-lived HS256 token signed by US (the SAME secret as everything else):
#
#   * CSRF — a forged callback can't fabricate a state we'd accept (no valid
#     signature), so an attacker can't stitch a victim's session to an
#     attacker-controlled provider code without first owning our secret.
#   * Integrity — it carries the provider slug (the callback asserts it matches
#     the path provider) and, for PKCE providers, the code_verifier (kept
#     server-side via the signed state rather than client-held).
#
# NAMED TRADEOFF (v1): there is NO server-side single-use state store. CSRF /
# integrity rest on the signature + a short exp ALONE. A captured full callback
# URL replayed within the exp window is NOT blocked at the state layer — but it
# fails anyway because the provider authorization `code` it carries is single-use
# at the provider's token endpoint (the second exchange returns an error → our
# exchange_code raises → graceful redirect-with-error, no session minted). A
# server-side single-use state nonce store is a deliberate follow-up; for v1 the
# code's single-use property at the provider is the backstop and is sufficient.
_OAUTH_STATE_TYPE = "oauth_state"


def issue_oauth_state(
    provider: str, *, code_verifier: str | None = None, nonce: str | None = None,
) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "type": _OAUTH_STATE_TYPE,
        "provider": provider,
        "code_verifier": code_verifier,
        "nonce": nonce,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(
            seconds=settings.oauth_state_ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_oauth_state(token: str) -> dict:
    """Return {provider, code_verifier, nonce} from a valid state token, else
    raise jwt.InvalidTokenError (bad type/sig/exp). Verified with OUR secret +
    algorithm — this is our token, never a provider's."""
    payload = jwt.decode(
        token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )
    if payload.get("type") != _OAUTH_STATE_TYPE:
        raise jwt.InvalidTokenError("expected oauth_state token")
    provider = payload.get("provider")
    if not provider:
        raise jwt.InvalidTokenError("oauth_state missing provider")
    return {
        "provider": provider,
        "code_verifier": payload.get("code_verifier"),
        "nonce": payload.get("nonce"),
    }
