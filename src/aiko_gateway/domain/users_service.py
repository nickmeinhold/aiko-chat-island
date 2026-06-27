"""User creation + authentication."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .ids import new_ulid
from .models import SocialIdentity, User
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
    # THE SOCIAL BYPASS GUARD (#13): a social-only account has password_hash=None.
    # This check MUST precede verify_password — argon2 on a None hash would raise,
    # and any "treat None as match" slip would turn a passwordless account into a
    # password-auth shortcut. No password is ever valid for a null-hash account.
    if user is None or user.password_hash is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def get_user_by_social(
    session: AsyncSession, provider: str, provider_sub: str
) -> User | None:
    """Resolve the local user for a verified (provider, sub) identity, or None
    if this federated identity has never been claimed."""
    row = (await session.execute(
        select(SocialIdentity).where(
            SocialIdentity.provider == provider,
            SocialIdentity.provider_sub == provider_sub,
        )
    )).scalar_one_or_none()
    if row is None:
        return None
    return await session.get(User, row.user_id)


async def create_social_user(
    session: AsyncSession, *, provider: str, provider_sub: str,
    handle: str, display_name: str, email: str | None = None,
) -> User:
    """Create a local user for a verified federated identity and link it, in ONE
    transaction. password_hash stays None (social-only). Raises IntegrityError if
    the handle is taken (username/aiko_username UNIQUE) OR the (provider, sub) is
    already claimed (uq_social_provider_sub) — the caller maps both to 409."""
    user = User(
        id=new_ulid(),
        username=handle,
        display_name=display_name or handle,
        password_hash=None,
        aiko_username=_sanitize_aiko_username(handle),
        email=email,
    )
    session.add(user)
    session.add(SocialIdentity(
        id=new_ulid(),
        provider=provider,
        provider_sub=provider_sub,
        user_id=user.id,
    ))
    await session.commit()
    return user
