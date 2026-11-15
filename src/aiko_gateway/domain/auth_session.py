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

import datetime as dt
import logging
from typing import Literal

import jwt
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from . import security, users_service
from .models import RefreshSession, User

log = logging.getLogger(__name__)


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


# --- refresh-token rotation (#420) ------------------------------------------ #

class RefreshReused(InvalidSession):
    """A refresh token older than the session's previous one was presented: two
    holders, so the session has ended. Rendered exactly as any InvalidSession (the
    same opaque 401), so the wire gains no new code and an old app's existing
    sign-out path is the whole client behaviour."""


class RefreshContended(Exception):
    """Every compare-and-swap attempt lost to a concurrent refresh of the same
    session. NOT an InvalidSession: the presented token was never judged, so it must
    not render as the 401 a client answers by signing out. Rendered as a 503 to
    retry."""


# Tombstones are pruned only this long after their newest token's `exp`. Expiry is
# checked by PyJWT against the wall clock, the same clock as `expires_at`; the
# slack is for a backward step of that clock, which would otherwise let a token of
# a pruned (possibly revoked) session verify again with no row to stop it.
_PRUNE_SLACK = dt.timedelta(days=1)

_MAX_ATTEMPTS = 3


async def rotate_refresh(session: AsyncSession, token: str, user: User) -> str:
    """Rotate a refresh token that `resolve_session_user` has already accepted, and
    return its successor. Owns its transaction: commits on success AND when it ends
    a session (the revocation must persist while the 401 is raised).

    The session's row names two tokens. Presenting:
      * the CURRENT one → it becomes previous, and a new current is minted;
      * the PREVIOUS one → a retry: the device never kept the current one (a lost
        response, a locked keychain). The current one is discarded and a
        replacement minted;
      * any other → a second holder: the session ends (RefreshReused).
    No timer decides a retry: a phone that dozes for hours after a lost response
    still holds the previous token, and that works. What it cannot survive is
    presenting a token OLDER than the previous one, which only a copy can do.

    A retry is never handed the current token back. Two holders of one token would
    then converge on one chain and each one's refresh would look like the other's
    retry, so a copy would never show. Replacing the current token forks them, and
    the holder left behind presents a token older than previous: the fork is the
    alarm. (A 30 s re-issue window once sat here; it let two holders refreshing in
    step stay converged forever. The app keeps one refresh in flight, so nothing
    needs it: aiko_chat_app#427.)

    A session's first refresh writes its row: a sign-in writes nothing, so every
    mint path stays a pure function and only this door stores session state.
    Every write is a compare-and-swap on the state it read; a lost race re-reads."""
    try:
        sid, jti = security.refresh_ids(token)
    except jwt.InvalidTokenError as exc:
        raise InvalidSession from exc   # a malformed claim is a 401, never a 500
    for _ in range(_MAX_ATTEMPTS):
        now = dt.datetime.now(dt.timezone.utc)
        expires_at = now + dt.timedelta(seconds=settings.jwt_refresh_ttl_seconds)
        await session.execute(delete(RefreshSession).where(
            RefreshSession.user_id == user.id,
            RefreshSession.expires_at < now - _PRUNE_SLACK))
        row = (await session.execute(
            select(RefreshSession).where(RefreshSession.id == sid)
            .execution_options(populate_existing=True))).scalar_one_or_none()

        if row is None:
            new_jti = security.new_token_id()
            inserted = (await session.execute(
                sqlite_insert(RefreshSession).values(
                    id=sid, user_id=user.id, current_jti=new_jti,
                    previous_jti=jti, expires_at=expires_at, revoked_at=None)
                .on_conflict_do_nothing())).rowcount
            if inserted == 1:
                return await _commit_and_mint(session, user, sid, new_jti, now)
            continue  # a concurrent first refresh made the row: judge against it

        if row.user_id != user.id or row.revoked_at is not None:
            await session.commit()
            raise InvalidSession

        if jti == row.current_jti:
            new_jti = security.new_token_id()
            moved = await _swap(session, row, current_jti=new_jti,
                                previous_jti=jti, expires_at=expires_at)
        elif jti == row.previous_jti:
            new_jti = security.new_token_id()
            moved = await _swap(session, row, current_jti=new_jti,
                                expires_at=expires_at)
        else:
            ended = (await session.execute(
                update(RefreshSession)
                .where(RefreshSession.id == sid, RefreshSession.revoked_at.is_(None))
                .values(revoked_at=now))).rowcount
            await session.commit()
            if ended:
                # No ids: the island declines to know whose session it was.
                log.warning("refresh token presented after its successor was used; "
                            "that session has ended")
            raise RefreshReused

        if moved:
            return await _commit_and_mint(session, user, sid, new_jti, now)
    await session.rollback()
    raise RefreshContended


async def _swap(session: AsyncSession, row: RefreshSession, **values) -> bool:
    """UPDATE the row only if it is still exactly the state this refresh read."""
    result = await session.execute(
        update(RefreshSession).where(
            RefreshSession.id == row.id,
            RefreshSession.current_jti == row.current_jti,
            RefreshSession.previous_jti == row.previous_jti,
            RefreshSession.revoked_at.is_(None),
        ).values(**values))
    return result.rowcount == 1


async def _commit_and_mint(session: AsyncSession, user: User, sid: str,
                           jti: str, now: dt.datetime) -> str:
    # Mint from values read BEFORE commit (expire_on_commit would lazy-reload `user`).
    user_id, gen = user.id, user.token_generation
    await session.commit()
    return security.issue_refresh(user_id, gen=gen, sid=sid, jti=jti, now=now)


async def purge_user_refresh_sessions(session: AsyncSession, user_id: str) -> None:
    """Account deletion's teardown of this FK child of `users` (no commit: the
    deletion transaction owns it)."""
    await session.execute(
        delete(RefreshSession).where(RefreshSession.user_id == user_id))
