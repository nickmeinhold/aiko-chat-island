"""The single per-user session gate shared by every token-presenting ingress.

Three ingresses resolve "who is this user" from a presented token — REST
(`get_current_user`), token refresh, and the WS handshake — and each must apply
the SAME per-user session policy (does the user exist? is the token's generation
current? is the account banned?). Historically each ingress re-implemented that
policy inline, so every new per-user gate (ban, then #1914 revocation) had to be
grep-replicated three times and the WS one was repeatedly the one forgotten
(`concept_auth_ingress_fragmentation`, #1927).

This module is that policy, written ONCE. Add a new per-user session gate inside
`resolve_session_user` and it is live at every token-presenting ingress at once.

**Policy centralized, rendering local.** The resolver lives in `domain` and raises
TRANSPORT-NEUTRAL exceptions — it must not import from `rest` (an HTTP exception
here would invert the domain→rest layering). Each ingress catches these and renders
in its own idiom: REST → 401 / structured 403; WS → 1008 close. The credential
login/mint paths (password/passkey/social) do NOT come through here — they resolve
a user by credential, not by a presented session token, and mint a token carrying
the current generation; their ban gate is `auth._deny_if_banned`.
"""
from __future__ import annotations

from typing import Literal

import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from . import security, users_service
from .models import User


class InvalidSession(Exception):
    """No/invalid token, unknown user, or a superseded token generation.

    Deliberately collapses "bad token" and "user gone" and "revoked generation"
    into one neutral signal — the ingresses render it opaquely (no existence leak;
    `concept_existence_hiding_has_a_timing_dimension`)."""


class SessionBanned(Exception):
    """The resolved user is suspended (`users_service.is_banned`)."""


async def resolve_session_user(
    session: AsyncSession, token: str, *, expected_type: Literal["access", "refresh"]
) -> User:
    """Decode → load → gate. Returns the live user or raises a neutral exception.

    THE single place a per-user session gate is enforced. `expected_type` is
    "access" (REST / WS) or "refresh" (the refresh endpoint)."""
    try:
        user_id, token_gen = security.decode_token(token, expected_type=expected_type)
    except jwt.InvalidTokenError as exc:
        raise InvalidSession from exc
    user = await users_service.get_by_id(session, user_id)
    # Existence + revocation (#1914) collapsed into one neutral rejection: an
    # absent row and a stale generation are equally "this session is dead".
    if user is None or token_gen != user.token_generation:
        raise InvalidSession
    # Ban (Piece B): a suspended account is refused even holding a valid token.
    if users_service.is_banned(user):
        raise SessionBanned
    return user
