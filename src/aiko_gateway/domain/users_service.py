"""User creation + authentication."""
from __future__ import annotations

import re
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .ids import new_ulid
from .models import PasskeyCredential, SocialIdentity, User
from .security import hash_password, verify_password

_AIKO_USERNAME_RE = re.compile(r"[^A-Za-z0-9_]")

# Bound on auto-handle regeneration. A handle is aiko-<8 hex> (~4.3e9 space) against
# a near-empty user table, so a collision is astronomically unlikely; the retry is a
# correctness belt-and-braces, not a hot path. Exhausting it means something is very
# wrong (RNG stuck / table saturated), so it fails loudly rather than looping forever.
_MAX_HANDLE_ATTEMPTS = 5


class CredentialAlreadyRegistered(Exception):
    """A passkey register/finish presented a credential_id that is ALREADY stored.
    That is not an account creation — the device should AUTHENTICATE. Carries the
    owning user_id so the caller can decide (a 409 today)."""

    def __init__(self, user_id: str) -> None:
        super().__init__("passkey credential already registered")
        self.user_id = user_id


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


async def create_passkey_user(
    session: AsyncSession, *, handle: str, display_name: str,
    email: str | None, material: dict,
) -> User:
    """Create a passkey-only user + its first credential in ONE transaction (#1471).

    A passkey is a CREDENTIAL, not a federated identity, so there is NO
    SocialIdentity row and NO password — the user is identified solely by their
    passkey(s). `material` is the verified credential carried in the provisioning
    token. Atomic + replay-safe: a replayed claim re-inserts the same credential_id,
    trips the UNIQUE constraint, and the WHOLE transaction (the new user included)
    rolls back → IntegrityError, which the caller maps to 409. There is therefore no
    window where a user exists without their credential."""
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
        handle = f"aiko-{secrets.token_hex(4)}"
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
        except IntegrityError:
            # The savepoint rolled back; the outer txn (challenge consume) is intact.
            # Distinguish the two UNIQUE constraints that can trip: a now-present
            # credential_id is a same-credential race → conflict; otherwise it was a
            # username/aiko_username auto-handle clash → mint a new handle and retry.
            owner = await _credential_owner(session, material["credential_id"])
            if owner is not None:
                raise CredentialAlreadyRegistered(owner)
    raise RuntimeError("could not allocate a unique passkey handle")


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
