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
