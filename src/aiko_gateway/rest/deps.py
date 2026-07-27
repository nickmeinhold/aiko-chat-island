"""FastAPI dependencies: DB session + authenticated current user.

`get_current_user` is the I1 enforcement point for REST (the WS handshake reuses
`decode_token` directly). It verifies the access JWT and loads the live user row
— roles/membership are NOT trusted from the token (plan §A3).
"""
from __future__ import annotations

from typing import Annotated, AsyncIterator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal
from ..domain import auth_session, moderation_service
from ..domain.models import User
from .errors import AccountSuspended

_bearer = HTTPBearer(auto_error=True)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    # Thin adapter over the shared session resolver (auth_session): the per-user
    # gate policy (exists / generation-current / not-banned) lives there so every
    # token-presenting ingress applies it identically. This ingress renders the
    # neutral outcomes as HTTP: opaque 401 for any invalid session, structured 403
    # for a ban. WS handshake + refresh are the sibling adapters.
    try:
        return await auth_session.resolve_session_user(
            session, creds.credentials, expected_type="access")
    except auth_session.InvalidSession:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    except auth_session.SessionBanned:
        raise AccountSuspended()


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_session)]


async def require_moderator(user: CurrentUser) -> User:
    """Gate for the site-wide moderation endpoints (Piece B). Depends on
    CurrentUser (so auth + ban are already enforced), then requires the caller be
    a configured site moderator. Parallel to the per-channel admin gate
    (memberships_service._require_admin) but island-wide, sourced from
    settings.moderator_user_ids (fail-closed empty). 403 for a non-moderator —
    same opaque code as any other forbidden action, no existence leak."""
    # Route through the SAME moderation_service.is_moderator predicate the /me flag
    # reads, so the enforced gate and the shown flag can never drift (cage-match
    # Tesla — one function, not two inlined copies of the config lookup).
    if not moderation_service.is_moderator(user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a moderator")
    return user


ModeratorUser = Annotated[User, Depends(require_moderator)]
