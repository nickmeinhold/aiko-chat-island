"""WebAuthn passkey ceremonies — the passwordless sign-in security boundary (#1471).

A passkey is a CREDENTIAL (an authenticator-held keypair), not a federated
identity. The device holds the private key and never reveals it; the gateway
stores only the PUBLIC key and verifies signatures. So unlike password auth there
is no shared secret to steal, and unlike social sign-in there is no third-party
token to trust — the trust root is a signature the device produced over a
challenge WE issued.

Two ceremonies, each a start/finish pair:
  * REGISTER  — the authenticator mints a new keypair (attestation). finish
    verifies the attestation and yields the credential material; the credential is
    persisted at /social/claim (carried in the gateway-signed provisioning token),
    so an abandoned handle-pick leaves no orphan credential row.
  * AUTHENTICATE — the authenticator signs the challenge with an existing key
    (assertion). finish looks the credential up by id, verifies the signature
    against the stored public key, and issues a session.

Security shape:
  * The challenge is server-issued, single-use, TTL'd (PasskeyChallenge), consumed
    by an atomic guarded UPDATE — never a read-then-write TOCTOU. consume DEFERS
    its commit (atomic-with-outcome, #24): finish does only LOCAL crypto (no
    network) and the channel is retryable, so the burn is made durable only after
    the sign-in OUTCOME succeeds — a transient post-consume failure rolls back and
    the app may retry the SAME ceremony. (Contrast the broker's eager state burn,
    which has a network exchange in the critical section — state_service.)
  * operation pins a challenge to its ceremony (a register challenge cannot
    complete an authenticate) — enforced IN the consume WHERE clause so it is
    atomic, and again by a DB CHECK.
  * expected_origin is a LIST (web origin derived from rp_id + the Android
    apk-key-hash); rp_id is hard-pinned. Verification is delegated to py_webauthn
    (Duo) — we do NOT hand-roll attestation/assertion checking.

SIGN-COUNT / clone detection — a DELIBERATE divergence from the handoff: the
handoff says "require sign_count strictly greater than stored". That is WRONG for
platform passkeys (iOS/Android), which report sign_count == 0 and never increment;
a strict-greater rule (0 > 0 is False) would reject EVERY authentication. The
WebAuthn spec treats a non-increase as clone evidence ONLY when the counts are
nonzero (a counter the authenticator actually supports). py_webauthn implements
exactly that, so we delegate to it and persist the returned new_sign_count — never
impose the literal rule. For a 0/0 authenticator clone detection is simply
unavailable (the credential's own non-exportability is the defense).
"""
from __future__ import annotations

import datetime as dt
import json
import secrets

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from .models import PasskeyChallenge, PasskeyCredential, PasskeyOperation


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _expected_origins() -> list[str]:
    """The web origin is DERIVED from rp_id (https://<rp_id>) so it can never drift
    from the RP; extra origins (the Android apk-key-hash) come from config. A LIST
    because a native app presents a different origin per platform — pinning a single
    browser origin locks one platform out (the §4 gotcha in the handoff)."""
    return [f"https://{settings.passkey_rp_id}", *settings.passkey_extra_origins]


def _uv_requirement() -> UserVerificationRequirement:
    """REQUIRED vs PREFERRED for the ceremony request, from config (default
    REQUIRED — a passwordless primary factor demands user verification)."""
    return (UserVerificationRequirement.REQUIRED
            if settings.passkey_require_user_verification
            else UserVerificationRequirement.PREFERRED)


# --- challenge store (single-use, TTL'd, atomic consume) ------------------- #

async def _store_challenge(
    session: AsyncSession, *, raw: bytes, operation: PasskeyOperation,
) -> str:
    """Persist a single-use challenge and return the opaque `state` handle the app
    round-trips. state == base64url(raw); its decode IS the WebAuthn challenge, so
    the row is both the DB key and the expected_challenge (no separate column)."""
    state = bytes_to_base64url(raw)
    # Opportunistic bounded cleanup (same piggy-back as nonce_service) so an
    # unauthenticated /start flood can't grow the table without bound.
    await session.execute(
        delete(PasskeyChallenge).where(PasskeyChallenge.expires_at <= _utcnow()))
    session.add(PasskeyChallenge(
        state=state,
        operation=operation.value,
        expires_at=_utcnow() + dt.timedelta(
            seconds=settings.passkey_challenge_ttl_seconds),
        consumed=False,
    ))
    await session.commit()
    return state


async def consume_challenge(
    session: AsyncSession, state: str, operation: PasskeyOperation,
) -> bytes | None:
    """Atomically claim a challenge exactly once, WITHOUT committing. Returns the
    raw challenge bytes iff the row exists, is unexpired, unconsumed, AND matches
    the operation; otherwise None (missing / expired / consumed / wrong-ceremony →
    caller fails closed). The single-use + operation guard is the WHERE clause on
    the conditional UPDATE, arbitrated by rowcount — two concurrent finishes race
    and at most one gets rowcount==1, never a read-then-write TOCTOU.

    Does NOT commit (atomic-with-outcome, #24): the caller commits only after the
    sign-in outcome succeeds, so a transient failure rolls the burn back and the
    app may retry the same ceremony. Safe to defer because finish does no network
    IO between this claim and the commit."""
    result = await session.execute(
        update(PasskeyChallenge)
        .where(
            PasskeyChallenge.state == state,
            PasskeyChallenge.consumed == False,  # noqa: E712 (SQL boolean)
            PasskeyChallenge.expires_at > _utcnow(),
            PasskeyChallenge.operation == operation.value,
        )
        .values(consumed=True)
    )
    if result.rowcount != 1:
        return None
    return base64url_to_bytes(state)


# --- ceremony start (mint options + challenge) ----------------------------- #

async def start_registration(session: AsyncSession) -> dict:
    """Mint a registration ceremony for an ANONYMOUS caller (first-passkey-creates-
    account). Returns {state, options} — options is the raw WebAuthn-JSON the
    platform authenticator parses. resident_key REQUIRED so the credential is
    discoverable (usernameless authentication later)."""
    raw = secrets.token_bytes(32)
    # No real username yet (the user picks a handle at /social/claim); the WebAuthn
    # user_name/display_name are cosmetic (shown in the OS passkey list). A random
    # provisional name avoids collisions in that list.
    options = webauthn.generate_registration_options(
        rp_id=settings.passkey_rp_id,
        rp_name=settings.passkey_rp_name,
        user_name=f"aiko-{secrets.token_hex(4)}",
        challenge=raw,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=_uv_requirement(),
        ),
    )
    state = await _store_challenge(
        session, raw=raw, operation=PasskeyOperation.REGISTER)
    return {"state": state, "options": json.loads(webauthn.options_to_json(options))}


async def start_authentication(session: AsyncSession) -> dict:
    """Mint a usernameless/discoverable authentication ceremony — empty
    allow_credentials, so the authenticator offers whatever resident credential the
    user has for this RP. Returns {state, options}."""
    raw = secrets.token_bytes(32)
    options = webauthn.generate_authentication_options(
        rp_id=settings.passkey_rp_id,
        challenge=raw,
        user_verification=_uv_requirement(),
    )
    state = await _store_challenge(
        session, raw=raw, operation=PasskeyOperation.AUTHENTICATE)
    return {"state": state, "options": json.loads(webauthn.options_to_json(options))}


# --- ceremony finish (verify — delegated to py_webauthn) -------------------- #

def verify_registration(*, raw_challenge: bytes, credential: dict) -> dict:
    """Verify an attestation response and return JSON-safe credential material
    (base64url strings) ready to ride in the provisioning token and be persisted at
    claim. Raises webauthn InvalidRegistrationResponse on any failure (the caller
    maps to a 4xx). The expected_challenge/origin/rp_id are OURS, never read from
    the response."""
    verified = webauthn.verify_registration_response(
        credential=json.dumps(credential),
        expected_challenge=raw_challenge,
        expected_rp_id=settings.passkey_rp_id,
        expected_origin=_expected_origins(),
        require_user_verification=settings.passkey_require_user_verification,
    )
    # Transports are reported by the authenticator in the RESPONSE (not on the
    # verified result) — a hint for future allowCredentials. Best-effort: store
    # them as a JSON array if present and well-formed, else None.
    transports = None
    raw_transports = (credential.get("response") or {}).get("transports")
    if isinstance(raw_transports, list) and raw_transports:
        transports = json.dumps(raw_transports)
    return {
        "credential_id": bytes_to_base64url(verified.credential_id),
        "public_key": bytes_to_base64url(verified.credential_public_key),
        "sign_count": verified.sign_count,
        "transports": transports,
        "aaguid": verified.aaguid,
    }


def verify_authentication(
    *, raw_challenge: bytes, credential: dict, public_key_b64: str,
    current_sign_count: int,
) -> int:
    """Verify an assertion against the STORED public key and return the new
    sign_count to persist. Raises webauthn InvalidAuthenticationResponse on a bad
    signature / origin / rp_id / challenge, AND on a clone-indicating sign_count
    regression (py_webauthn enforces the spec-correct rule — only when the counts
    are nonzero; a 0/0 platform authenticator is permitted, see module docstring)."""
    verified = webauthn.verify_authentication_response(
        credential=json.dumps(credential),
        expected_challenge=raw_challenge,
        expected_rp_id=settings.passkey_rp_id,
        expected_origin=_expected_origins(),
        credential_public_key=base64url_to_bytes(public_key_b64),
        credential_current_sign_count=current_sign_count,
        require_user_verification=settings.passkey_require_user_verification,
    )
    return verified.new_sign_count


# --- credential persistence ------------------------------------------------ #

def credential_id_of(credential: dict) -> str:
    """Normalise the credential id from an assertion response to the same
    unpadded-base64url form we store at registration (so the lookup key matches
    regardless of the client's padding)."""
    return bytes_to_base64url(base64url_to_bytes(credential["id"]))


async def get_credential(
    session: AsyncSession, credential_id_b64: str,
) -> PasskeyCredential | None:
    return (await session.execute(
        select(PasskeyCredential).where(
            PasskeyCredential.credential_id == credential_id_b64)
    )).scalar_one_or_none()
