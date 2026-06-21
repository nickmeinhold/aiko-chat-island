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
from ..domain import security, users_service
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
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
