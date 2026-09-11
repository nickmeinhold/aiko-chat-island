"""FastAPI dependencies: DB session + authenticated current user.

`get_current_user` is the REST adapter over the shared session resolver
(`domain.auth_session`) — the single per-user gate that REST, token refresh, and
the WS handshake all funnel through. It maps the resolver's neutral outcomes to
HTTP (opaque 401 / structured 403); roles/membership are NOT trusted from the
token (plan §A3).
"""
from __future__ import annotations

from typing import Annotated, AsyncIterator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from ..domain import auth_session, moderation_service
from ..domain.rate_limit import limiter
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


def rate_limit_user(bucket: str, *, limit: int | None = None, window: int | None = None):
    """Rate-limit an AUTHENTICATED route by USER rather than by client IP.

    REMOVING A COUPLING, NOT WIDENING A GUARD (part A item 2 of the #167 cage-match,
    decided with Nick 2026-09-12).

    The call-occupancy poll carried a per-IP budget of 150/60s, and that number was
    never a judgement about abuse — it was arithmetic to accommodate the fact that a
    caller and a callee are frequently behind ONE NAT (same house, same office) and so
    SHARE an IP budget while polling about once a second at each other. The limit had
    to be widened until a legitimate ring fit underneath it. That is a guard around a
    window: it holds until a third party, a retry storm, or a slightly longer ring
    walks through the same shared key, and it fails as a call that dies for no reason
    the user can see.

    The coupling IS the shared key, and this endpoint never needed it. The route is
    authenticated, so the island already knows exactly whose poll this is. Keyed on the
    user, two people in one house have two budgets, the NAT arithmetic disappears
    entirely, and a real ring cannot reach the limit rather than being sized to sit
    below it.

    THE COST, STATED RATHER THAN GLOSSED: per-user keying WIDENS the per-IP blast
    radius, because one address holding N accounts now holds N budgets where it held
    one. So this does not replace the IP bound — the occupancy route carries both, and
    its call site shows the arithmetic. Per-user is the limit a real ring must never
    touch; per-IP is the ceiling only a spray can reach. Neither is both.

    ``get_current_user`` is depended on explicitly, so ordering is not luck: the limiter
    cannot be consulted before the caller has been authenticated, and an unauthenticated
    request is rejected by the 401 rather than consuming somebody's budget.

    The key is prefixed ``u:`` so a user id can never be read as an IP in the limiter's
    state, even if a bucket name is ever shared between the two flavours.
    """
    async def _dependency(user=Depends(get_current_user)) -> None:
        # Read through the settings OBJECT, exactly as the IP-keyed sibling does, so
        # the suite's rate_limit_enabled switch flips both flavours together.
        if not settings.rate_limit_enabled:
            return
        allowed, retry_after = limiter.hit(
            bucket,
            f"u:{user.id}",
            settings.auth_rate_limit if limit is None else limit,
            settings.auth_rate_limit_window_seconds if window is None else window,
        )
        if not allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded; slow down",
                headers={"Retry-After": str(retry_after)},
            )

    return Depends(_dependency)
