"""User creation + authentication."""
from __future__ import annotations

import datetime as dt
import math
import re
import secrets

from sqlalchemy import and_, case, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

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

def _new_auto_handle() -> str:
    """The handle register/finish assigns: `aiko-` and twelve hex digits. Migration
    0030's backfill matches this shape; change the format and that history still
    describes the old one, which is fine because it ran once."""
    return f"aiko-{secrets.token_hex(6)}"


def handle_is_auto(user: User) -> bool:
    """Whether `user` still carries the handle register/finish assigned. STORED, not
    inferred: `handle_assigned` is set by create_passkey_account and cleared by the
    first real handle change. An inference from the spelling misread a person who
    chose an `aiko-<12 hex>` handle, and keeping it honest needed a refusal at every
    door that sets a handle (cage-match #435: Carnot, Tesla). The app uses this to ask
    for a handle before letting a user into chat, on every device, including accounts
    made before it asked."""
    return bool(user.handle_assigned)


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
        handle = _new_auto_handle()
        user = User(
            id=new_ulid(),
            username=handle,
            display_name=display_name or handle,
            password_hash=None,
            aiko_username=_sanitize_aiko_username(handle),
            email=None,
            handle_assigned=True,  # the user view's handle_auto, until they choose one
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
    this — only an ACTUAL handle change consults the cooldown, and every one starts
    it except the first claim away from an assigned handle (#437)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("handle change on cooldown")
        self.retry_after = retry_after


class AccountGone(Exception):
    """The account's row is gone: a deletion (its own, from another device) committed
    before this request's write. The caller maps it to the same opaque 401 the auth
    door gives a token whose user no longer exists (`deps.get_current_user`), which is
    what every later request from that token gets anyway (#439). Before, this was a
    500 on the display-name path (the ORM failing to flush or refresh a deleted row)
    and a full-window cooldown 429 on the handle path: a wait for an account that
    will never come back."""


# What `update_profile`'s one statement RETURNS and puts on the instance: every column
# it can write, plus the id the user view reads (so even an expire_on_commit session
# needs no reload to answer).
_PROFILE_COLUMNS = (User.id, User.username, User.aiko_username, User.display_name,
                    User.handle_changed_at, User.handle_assigned)


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
        together and handle_changed_at is stamped, EXCEPT on the first change away
        from the handle register/finish assigned, which leaves it NULL so the next
        change is free too (#437). A UNIQUE clash → HandleTaken.
      * A row that is gone (a deletion committed first) → AccountGone, on either
        path, never a 500 and never a cooldown.
      * display_name: applied whenever provided; never rate-limited on its OWN. In a
        COMBINED body {handle, display_name} the whole update is ATOMIC — if the
        handle change is refused (cooldown/taken) the display_name is NOT applied
        either (the folded UPDATE below is all-or-nothing). A display_name-only
        request is never gated.

    ONE STATEMENT DECIDES AND APPLIES (#439). The cooldown was folded into the write
    by cage-match PR#118 (Carnot+Tesla+Wu); now everything is: one `UPDATE …
    RETURNING` decides change-or-no-op, applies the cooldown and writes the display
    name together. The row it RETURNS is the outcome, and its values go onto `user`
    directly, so nothing reads or writes the row again once the statement is done.
    #438's rounds kept finding a decision taken in the DB and then acted on after
    the lock was released, where a rename or a self-deletion could land in between;
    with no second step there is no in between. The statement matches on the id
    alone and applies the cooldown inside its SET, so zero rows means only that the
    row is gone (AccountGone), and a refusal shows in the returned row itself: its
    username is not the one requested. No outcome needs a second read."""
    if handle is None and display_name is None:
        raise ValueError("update_profile requires handle and/or display_name")
    now = now or dt.datetime.now(dt.timezone.utc)
    # Captured up front: a rollback expires the instance, and reading user.id after
    # one would lazy-load outside the async context (MissingGreenlet).
    uid = user.id

    if handle is None:
        # display_name-only: no cooldown, no handle columns. Still one statement with
        # RETURNING, so a deleted row is a named refusal (AccountGone) rather than the
        # ORM failing to flush or refresh it, a 500.
        values: dict = {"display_name": display_name}
        gate = User.id == uid
    else:
        cutoff = now - dt.timedelta(seconds=cooldown_seconds)
        # THE ROW DECIDES whether this is a change. Every SET expression here reads the
        # row as it was before this statement (SQL evaluates them all against the old
        # values), so `same` is the row's answer, never the loaded user's: that snapshot
        # can be stale either way (cage-match #118: moved away from it; #438: moved onto
        # the very handle requested, two devices both claiming `paul`). A same-handle
        # request changes nothing: every handle column keeps its value, so it never
        # stamps the cooldown, never spends the free first claim, and never rewrites
        # aiko_username (which need not equal the sanitised handle on every row).
        same = User.username == handle
        # `change`: a real change the cooldown allows. It is the only thing that moves
        # a handle column, so a same-handle request and a refused one both leave every
        # handle column as it is, inside the statement, with no WHERE to fail. That is
        # what keeps zero rows meaning ONE thing (the row is gone) and puts a refusal in
        # the returned row instead: its username is still not the one requested (Tesla,
        # #443 r1: a WHERE that answered both "gone" and "refused" needed a second read,
        # and that read was only as good as the lock it ran under).
        change = and_(~same, or_(User.handle_changed_at.is_(None),
                                 User.handle_changed_at <= cutoff))
        cols = User.__table__.c
        values = {
            "username": case(
                (change, literal(handle, cols.username.type)), else_=User.username),
            "aiko_username": case(
                (change, literal(_sanitize_aiko_username(handle), cols.aiko_username.type)),
                else_=User.aiko_username),
            # THE FIRST CLAIM DOES NOT START THE CLOCK (Nick, 2026-10-28, #437). Moving
            # off the handle register/finish ASSIGNED leaves the stamp as it is (NULL),
            # so a sign-up typo can be fixed once more straight away, and that change
            # stamps. Read from the row in the statement that clears the flag, so two
            # devices racing their first claim can't both skip.
            "handle_changed_at": case(
                (and_(change, User.handle_assigned.is_(False)),
                 literal(now, cols.handle_changed_at.type)),
                else_=User.handle_changed_at),
            # A chosen handle: the account no longer carries the one it was assigned.
            "handle_assigned": case(
                (change, literal(False, cols.handle_assigned.type)),
                else_=User.handle_assigned),
        }
        if display_name is not None:
            # All-or-nothing with the handle, by the ROLLBACK, not by a CASE: a refused
            # change rolls the whole statement back below, so the name never lands
            # without its handle. (A CASE here would be a second guard on that, and a
            # mutant removing it survived the suite, #443 r2.)
            values["display_name"] = display_name
        gate = User.id == uid
    stmt = (
        update(User).where(gate).values(**values)
        .returning(*_PROFILE_COLUMNS)
        # synchronize_session=False: the CASEs must be evaluated by the DB against the
        # row as it is (the whole point of folding the rule into the write), never
        # re-run in Python against the loaded object, which holds a stale value.
        .execution_options(synchronize_session=False)
    )
    try:
        row = (await session.execute(stmt)).one_or_none()
    except IntegrityError as e:
        await session.rollback()
        if _is_handle_conflict(e):
            raise HandleTaken() from e
        raise  # a non-handle constraint is NOT a taken handle — don't mislabel it
    if row is None:
        # The gate is the id alone, so zero rows has one meaning: the row is gone, a
        # deletion (its own, from another device) committed before this statement.
        await session.rollback()
        raise AccountGone()
    if handle is not None and row.username != handle:
        # REFUSED BY THE COOLDOWN: the statement left the row as it was, so the returned
        # stamp is the one that refused it, read by the same statement that applied the
        # rule, never a NULL (a NULL stamp is `change`). Nothing to keep: roll back.
        await session.rollback()
        last = row.handle_changed_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
        # Measure elapsed against the time NOW, not the request's start: under a race
        # the request-start `now` can predate the stamp, making elapsed negative and
        # retry_after exceed the window. Cap at cooldown_seconds so Retry-After never
        # over-promises (cage-match #118, Carnot).
        elapsed = (dt.datetime.now(dt.timezone.utc) - last).total_seconds()
        raise HandleChangeCooldown(
            max(1, min(cooldown_seconds, math.ceil(cooldown_seconds - elapsed))))
    await session.commit()
    # The RETURNED values ARE the outcome: put them on the instance as committed
    # state, after the commit so an expire_on_commit session can't discard them. No
    # refresh: re-reading the row after the commit is the window a concurrent
    # deletion fell into (ObjectDeletedError, a 500). The instance answers with what
    # this statement wrote, whatever happens to the row next.
    for key, value in row._mapping.items():
        set_committed_value(user, key, value)
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
