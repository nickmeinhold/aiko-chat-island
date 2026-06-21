"""User creation + authentication."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .ids import new_ulid
from .models import User
from .security import hash_password, verify_password

_AIKO_USERNAME_RE = re.compile(r"[^A-Za-z0-9_]")


def _sanitize_aiko_username(username: str) -> str:
    """aiko wire usernames must be simple tokens (no spaces/separators)."""
    return _AIKO_USERNAME_RE.sub("_", username)


async def create_user(
    session: AsyncSession, *, username: str, display_name: str, password: str
) -> User:
    user = User(
        id=new_ulid(),
        username=username,
        display_name=display_name or username,
        password_hash=hash_password(password),
        aiko_username=_sanitize_aiko_username(username),
    )
    session.add(user)
    await session.commit()
    return user


async def get_by_id(session: AsyncSession, user_id: str) -> User | None:
    return await session.get(User, user_id)


async def authenticate(session: AsyncSession, username: str, password: str) -> User | None:
    user = (await session.execute(
        select(User).where(User.username == username)
    )).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user
