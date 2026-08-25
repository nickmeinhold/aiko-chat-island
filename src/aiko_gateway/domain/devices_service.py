"""Device-token registration (#16, increment 1) — the push-notification roster.

This is the persistence half of push notifications: WHO to push to. The actual
sending (APNs/FCM clients + a hook in the message fanout) is increment 2, blocked
on provider credentials. Increment 1 stands alone — fully testable with no
external dependency.

The single invariant: a device push token routes to exactly ONE user (its current
owner). ``register_device`` is therefore an upsert keyed on the globally-unique
token, not an insert — see ``DeviceToken`` for why reassign-on-conflict is the
correct model for a device that changes hands (logout/login on one phone).
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from .models import DeviceToken, PushEnvironment, _utcnow


def default_push_environment() -> str:
    """The APNs environment a client gets when it does not declare one (#3386).

    The island's own `APNS_USE_SANDBOX`, which is the honest answer while there is
    only one kind of build reaching a given box: the token was minted by whatever
    build the operator pinned the box for. It stops being honest the moment a
    debug build and a TestFlight build register against the same island — which is
    exactly when the client starts declaring it and this default stops being read.
    """
    return (PushEnvironment.SANDBOX if settings.apns_use_sandbox
            else PushEnvironment.PRODUCTION).value


async def register_device(
    session: AsyncSession, *, user_id: str, platform: str, token: str,
    push_environment: str | None = None,
) -> DeviceToken:
    """Register (or re-register) a push token for ``user_id``. Idempotent and
    race-safe: keyed on the globally-unique token.

    First registration inserts. A token that already exists — the same device
    re-registering, OR a device that changed hands to a new account — is
    REASSIGNED to the current user (and its platform/updated_at refreshed) rather
    than duplicated. UNIQUE(token) is the authority, not a pre-check (which would
    have a TOCTOU window under concurrent registrations).

    Insert inside a SAVEPOINT so a unique violation rolls back ONLY the failed
    insert, leaving the outer txn/greenlet intact — a plain commit-then-rollback
    breaks the subsequent re-fetch with MissingGreenlet on aiosqlite (the same
    hazard handled in memberships_service._insert_idempotent).

    ``push_environment`` (#3386) is the APNs world the token was minted in, or None
    for "whatever this island is pinned to" — resolved HERE rather than at the
    router so the in-process and test paths get the same default as the wire path
    (one door). It is refreshed on the reassign branch for the same reason
    ``platform`` is: a phone that switches from a debug build to TestFlight
    re-registers the SAME token string, and a row that kept the old environment
    would route a live credential to the wrong host until the row was deleted."""
    push_environment = push_environment or default_push_environment()
    row = DeviceToken(user_id=user_id, platform=platform, token=token,
                      push_environment=push_environment)
    try:
        async with session.begin_nested():
            session.add(row)
        await session.commit()
        return row
    except IntegrityError:
        # Token already registered — the SAVEPOINT is rolled back, the outer txn
        # is still live. Reassign the existing row to this user (eagerly populated
        # so a later sync attribute access can't trigger a lazy refresh).
        existing = (
            await session.execute(
                select(DeviceToken)
                .where(DeviceToken.token == token)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if existing is None:
            # Either the conflicting row vanished between the failed insert and
            # this re-fetch (a register racing an unregister of the same token),
            # or the IntegrityError was NOT the token-unique violation (a bad FK /
            # CHECK). Both are non-recoverable here: re-raise the real error rather
            # than masking it as a NoResultFound (cage-match Carnot, PR#28; mirrors
            # memberships_service._insert_idempotent).
            raise
        existing.user_id = user_id
        existing.platform = platform
        existing.push_environment = push_environment
        existing.updated_at = _utcnow()  # explicit: onupdate fires only on a changed-col flush
        await session.commit()
        return existing


async def unregister_device(
    session: AsyncSession, *, user_id: str, token: str
) -> bool:
    """Remove the caller's device token (app logout). Returns True if a row was
    deleted, False otherwise.

    Scoped to (user_id, token), NOT token alone (cage-match Maxwell+Carnot, PR#28).
    Unregistering is purely "clear MY registration" — it never legitimately crosses
    users — so scoping to the authenticated user closes a cross-user delete vector
    (an authed caller who learns another user's token could otherwise push-DoS
    them) at zero cost: if the token was already reassigned to someone else, this
    correctly no-ops (it's no longer the caller's). Note the asymmetry with
    register, whose reassign-on-conflict MUST cross users for the device-changes-
    hands case."""
    result = await session.execute(
        delete(DeviceToken).where(
            DeviceToken.token == token, DeviceToken.user_id == user_id
        )
    )
    await session.commit()
    return result.rowcount > 0


async def tokens_for_user(
    session: AsyncSession, user_id: str
) -> list[DeviceToken]:
    """Every registered device token for a user — the fanout target list the
    increment-2 push sender will iterate. Ordered by id for deterministic tests."""
    rows = (
        await session.execute(
            select(DeviceToken)
            .where(DeviceToken.user_id == user_id)
            .order_by(DeviceToken.id)
        )
    ).scalars()
    return list(rows)


async def purge_user_devices(session: AsyncSession, user_id: str) -> None:
    """Delete all device tokens for a user — called from account deletion
    (children-before-parent, no ON DELETE CASCADE in this codebase). Does NOT
    commit: the caller owns the deletion transaction (mirrors
    moderation_service.purge_user_moderation_rows)."""
    await session.execute(
        delete(DeviceToken).where(DeviceToken.user_id == user_id)
    )
