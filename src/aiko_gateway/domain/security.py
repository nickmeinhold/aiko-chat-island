"""Auth primitives: argon2id password hashing + JWT issue/verify.

Tokens carry `sub` (user_id) + `type` (access|refresh) + `gen` (the user's
token_generation at mint time, #1914) + expiry — NOT roles. Roles/membership AND
the live token_generation are read from the DB on each request (plan §A3) so a
revoked membership OR a bumped generation takes effect immediately, not at next
token refresh.
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


def _issue(user_id: str, token_type: str, ttl_seconds: int, *, gen: int) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": user_id,
        "type": token_type,
        # Session-revocation generation (#1914). The token is valid only while this
        # equals the user's live `users.token_generation`; bumping that column
        # (recovery re-key) invalidates every token minted at an older gen. The
        # equality check happens at each auth ingress (it needs the DB row), not
        # here — decode_token only surfaces the claim.
        "gen": gen,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(seconds=ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def issue_access(user_id: str, *, gen: int = 0) -> str:
    """Mint an access token bound to the user's token_generation. `gen` defaults to
    0 — the un-revoked baseline — so a caller that omits it can only ever produce a
    token that is REJECTED for a revoked user (0 != their bumped gen), never one
    that bypasses revocation. Real mint paths pass the user's live token_generation."""
    return _issue(user_id, "access", settings.jwt_access_ttl_seconds, gen=gen)


def issue_refresh(user_id: str, *, gen: int = 0) -> str:
    """Mint a refresh token bound to token_generation (see issue_access on the
    fail-closed default)."""
    return _issue(user_id, "refresh", settings.jwt_refresh_ttl_seconds, gen=gen)


def decode_token(token: str, *, expected_type: str) -> tuple[str, int]:
    """Return (user_id, generation) if valid and of the expected type, else raise
    jwt.InvalidTokenError.

    A token minted before #1914 carries no `gen` claim; it reads as generation 0
    so it validates against the default users.token_generation=0 (no mass-logout on
    deploy). The `gen` is signed, so a client cannot forge or strip it undetected.
    The caller must compare the returned generation against the live user row — an
    ingress that ignores it leaves revocation unenforced (mirror is_banned)."""
    payload = jwt.decode(
        token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"expected {expected_type} token")
    sub = payload.get("sub")
    if not sub:
        raise jwt.InvalidTokenError("missing sub")
    # Fail CLOSED on a malformed generation. Only we mint these (HS256-signed), so a
    # bad `gen` shouldn't occur — but a trust-boundary decoder must never 500 on a
    # bad claim (the ingresses catch only InvalidTokenError; anything else escapes as
    # a 500 / WS crash). Missing → 0 (legacy pre-#1914 tokens). Present must be a
    # non-negative int; a bool / null / string / negative is an INVALID token, not a
    # server error — same fail-closed posture as signing.validate_origin's type guards
    # (bool is an int subclass, so exclude it explicitly).
    gen = payload.get("gen", 0)
    if isinstance(gen, bool) or not isinstance(gen, int) or gen < 0:
        raise jwt.InvalidTokenError("invalid gen claim")
    return sub, gen


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


# --- OAuth broker state (#21) ---------------------------------------------- #
# The broker's authorization-code flow `state` parameter is NO LONGER a signed
# self-contained token (cage-match #30, Finding 1). It is now a SERVER-SIDE
# single-use nonce (domain/state_service.py + the OAuthState model). Two reasons
# the JWT was removed:
#
#   * PKCE — a stateless JWT had to carry the PKCE code_verifier through the
#     browser/provider (base64-readable in the URL), defeating PKCE. The verifier
#     now lives only in the server-side state row; only the code_challenge crosses
#     the wire.
#   * REPLAY / login-CSRF — a signed-stateless state is replayable within its exp
#     window. The nonce is single-use (consumed + expires_at, atomic redemption),
#     so a captured callback URL can't be replayed at the state layer.
#
# The earlier NAMED TRADEOFF ("no server-side single-use state store; the
# provider code's single-use property is the only backstop") is therefore
# RETIRED — the store now exists and IS the single-use guard.
