"""Device-token registration (#16) — the push-notification roster.

This is the persistence half of push notifications: WHO to push to, and WHAT each
token is for. The sending half lives in ``push_service`` behind one door, with
``apns`` and ``fcm`` as its two pure transports.

The single invariant: a device push token routes to exactly ONE user (its current
owner). ``register_device`` is therefore an upsert keyed on the globally-unique
token, not an insert — see ``DeviceToken`` for why reassign-on-conflict is the
correct model for a device that changes hands (logout/login on one phone).

TWO ROWS PER HANDSET IS NORMAL, NOT A DUPLICATE (design 12 Decision 2). An alert
token and a VoIP token for one phone come from different registries and are
different strings, so they upsert independently and ``UNIQUE(token)`` needs no
change to accommodate them.
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from .models import DeviceToken, ApnsEnvironment, TokenKind, _utcnow


def default_apns_environment() -> str:
    """The APNs environment a client gets when it does not declare one (#3386).

    The island's own `APNS_USE_SANDBOX`, which is the honest answer while there is
    only one kind of build reaching a given box: the token was minted by whatever
    build the operator pinned the box for. It stops being honest the moment a
    debug build and a TestFlight build register against the same island — which is
    exactly when the client starts declaring it and this default stops being read.
    """
    return (ApnsEnvironment.SANDBOX if settings.apns_use_sandbox
            else ApnsEnvironment.PRODUCTION).value


async def register_device(
    session: AsyncSession, *, user_id: str, platform: str, token: str,
    apns_environment: ApnsEnvironment | None = None,
    token_kind: TokenKind | None = None,
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

    ``apns_environment`` (#3386) is the APNs world the token was minted in, or None
    for "whatever this island is pinned to" — resolved HERE rather than at the
    router so the in-process and test paths get the same default as the wire path
    (one door). Typed as the enum, not ``str``: the closed set is the point, and a
    caller holding a bare string has already lost the guarantee.

    OMISSION PRESERVES, DECLARATION WINS (cage-match, Carnot + Maxwell). The
    reassign branch does NOT re-resolve the default over an existing row. The two
    directions have wildly asymmetric risk once you know how APNs mints tokens: a
    sandbox token and a production token for the same app+device are DIFFERENT
    STRINGS, so "same token, environment changed" — the case a blind refresh would
    defend — is close to unreachable. Whereas "a client stops sending the field"
    (an app rollback, an older code path) is an ordinary regression, and a blind
    refresh would answer it by resetting an explicitly-declared production token to
    the island default, breaking a live device until it re-registers. So an
    explicit value always wins; an omitted one leaves the stored value alone.

    ``is None``, not falsy (cage-match, Carnot HIGH). ``or`` would silently convert
    an empty string into the island default — an invalid closed-set value quietly
    becoming a valid one, inside the module that claims to be the single door. With
    ``is None`` a bad value reaches the DB CHECK and is REJECTED, which is the
    fail-closed direction.

    ``token_kind`` (design 12 Decision 2) is what the token is FOR — see TokenKind
    for why that is a different axis from ``platform``. Enum-typed, following
    ``apns_environment`` rather than the stringly-typed ``platform`` one line
    above, for the reason the paragraph above already argues: a caller holding a
    bare string has already lost the guarantee.

    ITS TWO RESOLUTION RULES LOOK CONTRADICTORY AND ARE NOT — they answer
    different questions, and a reviewer will (rightly) raise it, so it is written
    down here rather than inferred from two lines forty apart.

      * ON INSERT, ABSENT MEANS ALERT. That is the wire contract, and it is what
        makes 0025 backfill-free: an existing row is exactly a row whose client
        never declared a kind, and ``server_default='alert'`` says the same thing
        in DDL.
      * ON REASSIGN, OMISSION PRESERVES — and the argument is STRONGER here than
        for ``apns_environment``. "Same token string, kind changed" is not merely
        close to unreachable, it is IMPOSSIBLE BY CONSTRUCTION: PushKit and UIKit
        mint from different registries and one string cannot be both. Whereas "a
        client stopped sending the field" (an app rollback to a pre-``token_kind``
        build) is an ordinary regression, and absent-means-alert on reassign would
        answer it by silently downgrading a live VoIP row to alert — which then
        sends an ALERT push to a VoIP token: 400 DeviceTokenNotForTopic, REJECTED,
        never reaped, one WARNING line, and no ring. That is this feature's own
        failure mode, self-inflicted.

    NO ``default_token_kind()`` HELPER AND NO SETTING. ``apns_environment`` needs a
    function because its default is a per-island fact; ``token_kind``'s default is
    a CONSTANT that is part of the wire contract, and a per-island setting would
    permit an island whose absent-means-voip."""
    declared = apns_environment.value if apns_environment is not None else None
    resolved = declared if declared is not None else default_apns_environment()
    # FAIL LOUDLY AND AT THE BOUNDARY on a non-enum (Carnot, cage-match PR#170).
    # This read `token_kind.value if token_kind is not None`, so an in-process
    # caller passing a bare `"voip"` got an AttributeError from deep inside the
    # service — while the docstring above implies the DB CHECK arbitrates. The route
    # is pydantic-validated so no HTTP path reaches this, but "no caller does that
    # today" is exactly the guarantee a second caller removes, and this module has
    # in-process callers by design. A closed type is not a String; refusing one here
    # is the same argument TokenKind itself makes, applied to the door.
    if token_kind is not None and not isinstance(token_kind, TokenKind):
        raise TypeError(
            f"token_kind must be a TokenKind, got {type(token_kind).__name__} "
            f"({token_kind!r}). The closed set has one definition; a bare string "
            "here would reach the DB as an unvalidated value.")
    declared_kind = token_kind.value if token_kind is not None else None
    resolved_kind = (declared_kind if declared_kind is not None
                     else TokenKind.ALERT.value)
    # Passed EXPLICITLY rather than left to the column's server_default: the caller
    # reads this row back immediately (the route echoes the resolved value), and a
    # server-default column is unpopulated on the instance until something reloads
    # it. Same reason `apns_environment` is passed explicitly one line up.
    row = DeviceToken(user_id=user_id, platform=platform, token=token,
                      apns_environment=resolved, token_kind=resolved_kind)
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
        if declared is not None:
            existing.apns_environment = declared
        if declared_kind is not None:
            existing.token_kind = declared_kind
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
    hands case.

    SCOPED TO ONE TOKEN, AND IT MUST NEVER INFER THE OTHER (design 12 Decision 2a).
    The app half keeps a durable unregister debt recording a SET OF TOKENS PER
    ISLAND WITH NO KIND, so a sign-out can discharge one kind and leave the other
    routable. Today that mis-delivers a silent data push; once VoIP is live it is a
    STRANGER'S PHONE RINGING FULL-SCREEN for the previous owner. The island half of
    the answer is exactly this signature: tear down by ``(user_id, token)`` as now,
    and never treat discharging one kind as having discharged the other. No code
    change was needed — the discipline is not to add the inference."""
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
