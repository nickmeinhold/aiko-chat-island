"""FastAPI dependencies: DB session + authenticated current user.

`get_current_user` is the I1 enforcement point for REST (the WS handshake reuses
`decode_token` directly). It verifies the access JWT and loads the live user row
— roles/membership are NOT trusted from the token (plan §A3).
"""
from __future__ import annotations

from typing import Annotated, AsyncIterator

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal
from ..domain import moderation_service, security, users_service
from ..domain.models import User

_bearer = HTTPBearer(auto_error=True)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    try:
        user_id = security.decode_token(creds.credentials, expected_type="access")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    user = await users_service.get_by_id(session, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found")
    # Ban enforcement (Piece B): a suspended account is rejected on EVERY
    # authenticated REST route here, even if it still holds a valid (unexpired)
    # access token. This is one of the enumerated ingresses — the WS handshake,
    # refresh, and each login/mint path apply the same is_banned predicate.
    if users_service.is_banned(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "account suspended")
    return user


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
