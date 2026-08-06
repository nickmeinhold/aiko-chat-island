"""User creation + authentication."""
from __future__ import annotations

import datetime as dt
import math
import re
import secrets

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .ids import new_ulid
from .models import PasskeyCredential, SocialIdentity, User
from .security import hash_password, verify_password

_AIKO_USERNAME_RE = re.compile(r"[^A-Za-z0-9_]")

# Bound on auto-handle regeneration. A handle is aiko-<12 hex> (~2.8e14 space)
# against a near-empty user table, so a genuine collision is vanishingly unlikely;
# the retry is a correctness belt-and-braces, not a hot path. Exhausting it means
# something is very wrong (RNG stuck / table saturated), so the caller maps it to a
# deliberate 503 rather than letting it escape as an uncategorised 500.
_MAX_HANDLE_ATTEMPTS = 5


class HandleAllocationExhausted(Exception):
    """create_passkey_account could not find a free auto-handle within
    _MAX_HANDLE_ATTEMPTS. Only reachable under a stuck RNG or a saturated table —
    the caller owns this as a deliberate 503, not a bare 500."""


class CredentialAlreadyRegistered(Exception):
    """A passkey register/finish presented a credential_id that is ALREADY stored.
    That is not an account creation — the device should AUTHENTICATE. Carries the
    owning user_id when known (may be None if a concurrent winner's row is not yet
    visible to this transaction's snapshot — the caller 409s regardless)."""

    def __init__(self, user_id: str | None) -> None:
        super().__init__("passkey credential already registered")
        self.user_id = user_id


def _is_credential_id_conflict(err: IntegrityError) -> bool:
    """True iff the IntegrityError is the passkey_credentials.credential_id UNIQUE
    violation (vs a users.username / users.aiko_username auto-handle clash). SQLite
    names the column in the message ('UNIQUE constraint failed: <table>.<col>'), so
    we classify DETERMINISTICALLY from the constraint rather than inferring it from a
    follow-up SELECT — a snapshot-isolated read can miss a concurrent winner's row
    and misclassify a credential race as a handle clash (Tesla, cage-match PR#68).
    SQLite is the sole engine in dev AND prod (see CLAUDE.md), so matching the SQLite
    message is safe here."""
    return "passkey_credentials.credential_id" in str(getattr(err, "orig", err))


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


def is_banned(user: User) -> bool:
    """Whether `user` is suspended from this island (moderation ban, Piece B).

    A pure predicate on the already-loaded row (no DB round-trip) so every auth
    ingress can apply the SAME check: REST (get_current_user), the WS handshake,
    token refresh, and each login/mint path. `banned_at` set = suspended; NULL =
    active. The single source of truth for 'is this account allowed to act'."""
    return user.banned_at is not None


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


async def create_passkey_user(
    session: AsyncSession, *, handle: str, display_name: str,
    email: str | None, material: dict,
) -> User:
    """Create a passkey-only user with an EXPLICIT handle + its first credential in
    ONE transaction (#1471).

    The explicit-handle sibling of create_passkey_account (which auto-generates the
    handle for the live register/finish path). Retained as a test/seed helper — the
    live passkey flow no longer routes through a caller-chosen handle. A passkey is a
    CREDENTIAL, not a federated identity, so there is NO SocialIdentity row and NO
    password — the user is identified solely by their passkey(s). `material` is the
    verified credential from verify_registration. Atomic + replay-safe: a replayed
    insert re-inserts the same credential_id, trips the UNIQUE constraint, and the
    WHOLE transaction (the new user included) rolls back → IntegrityError, which the
    caller maps to 409. There is therefore no window where a user exists without their
    credential."""
    user = User(
        id=new_ulid(),
        username=handle,
        display_name=display_name or handle,
        password_hash=None,
        aiko_username=_sanitize_aiko_username(handle),
        email=email,
    )
    session.add(user)
    session.add(_passkey_credential_row(user.id, material))
    await session.commit()
    return user


async def create_passkey_account(
    session: AsyncSession, *, material: dict, display_name: str = "",
) -> User:
    """Create a passkey-only account with NO caller-chosen handle (Design 04 Step 1).

    register/finish calls this directly: a device that completes attestation gets its
    own account immediately, identified solely by the passkey. The handle is
    auto-generated (``aiko-<hex>``) and cosmetic — you authenticate by passkey, never
    by name — so account creation can never collide with a pre-existing account. This
    is what closes #1728: persistence is no longer gated on a handle claim that could
    be rejected and orphan the device credential forever.

    Transaction: this DOES NOT commit — the caller owns the commit so the credential
    write lands atomically with the deferred challenge burn (the atomic-with-outcome
    contract, #24). Each account-INSERT attempt runs in a SAVEPOINT so a handle
    collision rolls back ONLY that attempt, never the outer transaction's challenge
    consume. Re-registering an already-stored credential_id is not a create — it
    raises CredentialAlreadyRegistered (the device should authenticate); the
    credential_id UNIQUE constraint is the real arbiter of a concurrent race."""
    # Friendly pre-check (UX, not correctness): surface an existing credential as a
    # clean typed conflict rather than an opaque IntegrityError. The UNIQUE constraint
    # inside the savepoint is what actually arbitrates a concurrent same-credential race.
    existing = await _credential_owner(session, material["credential_id"])
    if existing is not None:
        raise CredentialAlreadyRegistered(existing)

    for _ in range(_MAX_HANDLE_ATTEMPTS):
        handle = f"aiko-{secrets.token_hex(6)}"
        user = User(
            id=new_ulid(),
            username=handle,
            display_name=display_name or handle,
            password_hash=None,
            aiko_username=_sanitize_aiko_username(handle),
            email=None,
        )
        try:
            async with session.begin_nested():  # SAVEPOINT — isolates this attempt
                session.add(user)
                session.add(_passkey_credential_row(user.id, material))
            return user
        except IntegrityError as e:
            # The savepoint rolled back; the outer txn (challenge consume) is intact.
            # Classify by WHICH UNIQUE constraint fired — deterministically, from the
            # error, NOT from a follow-up SELECT (which a snapshot-isolated read can
            # get wrong under a concurrent same-credential winner). credential_id
            # conflict → the device already has an account (409); anything else is a
            # username/aiko_username auto-handle clash → mint a fresh handle and retry.
            if _is_credential_id_conflict(e):
                # owner may be None if the concurrent winner's row isn't visible to
                # our snapshot yet — the caller 409s on the exception regardless.
                raise CredentialAlreadyRegistered(
                    await _credential_owner(session, material["credential_id"]))
    raise HandleAllocationExhausted()


async def _credential_owner(session: AsyncSession, credential_id: str) -> str | None:
    """user_id that owns credential_id, or None if unregistered."""
    return (await session.execute(
        select(PasskeyCredential.user_id).where(
            PasskeyCredential.credential_id == credential_id)
    )).scalar_one_or_none()


def _passkey_credential_row(user_id: str, material: dict) -> PasskeyCredential:
    """The SINGLE place a PasskeyCredential row is built from verified material —
    shared by create_passkey_user (new account) and link_passkey_credential
    (existing account, #1727) so the persisted shape can never drift between the
    two doors."""
    return PasskeyCredential(
        credential_id=material["credential_id"],
        user_id=user_id,
        public_key=material["public_key"],
        sign_count=material["sign_count"],
        transports=material.get("transports"),
        aaguid=material.get("aaguid"),
    )


async def link_passkey_credential(
    session: AsyncSession, *, user_id: str, material: dict,
) -> None:
    """Attach a verified passkey credential to an EXISTING, authenticated user
    (#1727 — the missing link-to-existing path).

    Unlike create_passkey_user this creates NO user and claims NO handle: an
    already-signed-in user (typically a social account) adds a passkey, and it is
    persisted DIRECTLY against their user_id. Atomic + replay-safe: a duplicate
    credential_id trips the UNIQUE constraint → IntegrityError, which the caller
    maps to 409 (the credential is already registered, to this or another account).
    This closes the gap where an existing user was forced through register→claim,
    where a handle conflict with their OWN account orphaned the device credential."""
    session.add(_passkey_credential_row(user_id, material))
    await session.commit()


class HandleTaken(Exception):
    """The requested handle is already in use by another account (username or its
    derived aiko_username UNIQUE constraint). The caller maps this to 409. Classified
    from the IntegrityError rather than a pre-SELECT so a concurrent same-handle
    winner can't slip through a TOCTOU gap — the UNIQUE constraint is the arbiter,
    mirroring create_passkey_account's handling."""


class HandleChangeCooldown(Exception):
    """A handle change was refused because the previous change was within the
    cooldown window (settings.handle_change_cooldown_seconds). Carries retry_after
    (whole seconds until the window lifts) which the caller surfaces as 429 +
    Retry-After. display_name-only edits and no-op same-handle writes never raise
    this — only an ACTUAL handle change starts/consults the cooldown."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("handle change on cooldown")
        self.retry_after = retry_after


async def update_profile(
    session: AsyncSession, user: User, *,
    handle: str | None = None, display_name: str | None = None,
    cooldown_seconds: int, now: dt.datetime | None = None,
) -> User:
    """THE single mutate door for a user's own profile (PATCH /v1/me, #2631).

    Route, in-process, and test paths all pass through here so the identity
    invariants live in ONE place (the project's "seal the mutator, not the caller"
    rule). Identity is the KEY (user.id) — this only ever rewrites the mutable
    labels (username/aiko_username/display_name), never the id, so no @-mention or
    DM bound to the key is ever orphaned.

    Rules:
      * At least one of handle/display_name must be provided (the caller 400s on
        neither; asserted here as the single door's own guard).
      * handle: applied ONLY when it differs from the current handle (`username`,
        the user-VISIBLE handle — the label @-mentions/DMs render; a change that
        merely re-sanitizes to the same wire `aiko_username`, e.g. "a b"->"a_b",
        still changes the visible handle, so it legitimately consumes the cooldown).
        A change is refused with HandleChangeCooldown while within cooldown_seconds
        of the last change; on success username + aiko_username are rewritten
        together and handle_changed_at is stamped. A UNIQUE clash → HandleTaken.
      * display_name: applied whenever provided; never rate-limited on its OWN. In a
        COMBINED body {handle, display_name} the whole update is ATOMIC — if the
        handle change is refused (cooldown/taken) the display_name is NOT applied
        either (the folded UPDATE below is all-or-nothing). A display_name-only
        request is never gated.

    Cooldown folded INTO the write (cage-match PR#118, Carnot+Tesla+Wu): the handle
    change is a single conditional UPDATE whose WHERE carries the cooldown predicate,
    so a concurrent double-change can't both pass a stale in-memory read of
    handle_changed_at (the observe-then-write TOCTOU the project forbids —
    concept_visibility_gate_atomic_with_write). rowcount==0 ⇒ the predicate rejected
    the write; the row always exists (it is the authed user), so that is
    unambiguously the cooldown, not a missing row (the SQLite rowcount==0 ambiguity
    doesn't arise here)."""
    if handle is None and display_name is None:
        raise ValueError("update_profile requires handle and/or display_name")
    now = now or dt.datetime.now(dt.timezone.utc)

    if handle is None or handle == user.username:
        # display_name-only, or a no-op same-handle write: no cooldown, no atomic
        # gate. A no-op handle does NOT stamp handle_changed_at (the form-resubmit
        # lockout footgun stays closed). ACCEPTED residual (cage-match #118, Tesla):
        # the "same handle" test is against the request-loaded user.username, so in a
        # narrow self-race (this user's handle was concurrently changed after load)
        # a genuine change could read as a no-op and silently not apply. Outcome is
        # benign — no wrong state persists, the user simply retries — so we don't
        # fold this into the DB at the cost of tangling the no-op/cooldown semantics.
        if display_name is not None:
            user.display_name = display_name
        await session.commit()
        # Symmetric with the handle path's post-commit refresh. Not strictly required
        # today (SessionLocal is expire_on_commit=False — db.py:79 — so the ORM object
        # stays live after commit), but making BOTH success paths end the same way
        # removes the asymmetry cross-family reviewers repeatedly read as a
        # MissingGreenlet risk, and keeps the door correct if expire_on_commit ever
        # flips (cage-match #118, Tesla+Wu).
        await session.refresh(user)
        return user

    # Handle CHANGE — fold the cooldown predicate into the write.
    cutoff = now - dt.timedelta(seconds=cooldown_seconds)
    values: dict = {
        "username": handle,
        "aiko_username": _sanitize_aiko_username(handle),
        "handle_changed_at": now,
    }
    if display_name is not None:  # atomic with the handle change (all-or-nothing)
        values["display_name"] = display_name
    stmt = (
        update(User)
        .where(
            User.id == user.id,
            or_(User.handle_changed_at.is_(None), User.handle_changed_at <= cutoff),
        )
        .values(**values)
        # synchronize_session=False: the cooldown predicate must be evaluated by the
        # DB (the whole point of folding it into the write), NOT re-evaluated in
        # Python against the loaded ORM object — that in-memory eval compares a
        # SQLite-naive handle_changed_at against the tz-aware cutoff and TypeErrors,
        # and would also defeat the atomicity. We refresh() the row after a hit.
        .execution_options(synchronize_session=False)
    )
    try:
        result = await session.execute(stmt)
    except IntegrityError as e:
        await session.rollback()
        if _is_handle_conflict(e):
            raise HandleTaken() from e
        raise  # a non-handle constraint is NOT a taken handle — don't mislabel it
    if result.rowcount == 0:
        # The cooldown predicate rejected the write — the row's handle_changed_at is
        # set AND newer than cutoff (a NULL stamp would have matched the WHERE, so
        # rowcount==0 is never the never-changed case). Re-read the CURRENT stamp to
        # compute retry_after. Capture the id BEFORE rollback — rollback expires every
        # instance unconditionally, so reading user.id afterwards would trigger a lazy
        # load outside the async context (MissingGreenlet).
        uid = user.id
        await session.rollback()
        last = (await session.execute(
            select(User.handle_changed_at).where(User.id == uid))).scalar_one_or_none()
        if last is None:
            # The row vanished between the UPDATE and this read — a concurrent
            # self-account-deletion. Don't scalar_one()-500 (cage-match #118,
            # Carnot+Tesla); report the full window, non-crashing — the account is
            # going away regardless.
            raise HandleChangeCooldown(cooldown_seconds)
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
        # Recompute now AFTER the read: under a concurrent race the request-start `now`
        # can predate the winner's stamp, making elapsed negative and retry_after
        # exceed the window. Cap at cooldown_seconds so Retry-After never over-promises
        # (cage-match #118, Carnot).
        elapsed = (dt.datetime.now(dt.timezone.utc) - last).total_seconds()
        raise HandleChangeCooldown(
            max(1, min(cooldown_seconds, math.ceil(cooldown_seconds - elapsed))))
    await session.commit()
    await session.refresh(user)  # Core UPDATE bypassed the ORM — reload the labels
    return user


def _is_handle_conflict(err: IntegrityError) -> bool:
    """True iff the IntegrityError is the users.username / users.aiko_username UNIQUE
    violation (the handle pair), vs any OTHER constraint that might later land on the
    users row. SQLite names the column ('UNIQUE constraint failed: <table>.<col>'),
    so we classify DETERMINISTICALLY from the constraint — the same discipline as
    _is_credential_id_conflict. Today the handle pair is the only unique surface a
    self-profile edit can hit; this makes a FUTURE unique column (e.g. users.email)
    fail as itself rather than mis-singing as 'handle already taken'."""
    msg = str(getattr(err, "orig", err))
    return "users.username" in msg or "users.aiko_username" in msg
