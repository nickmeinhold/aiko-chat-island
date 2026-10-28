"""WebAuthn passkey ceremonies — the passwordless sign-in security boundary (#1471).

A passkey is a CREDENTIAL (an authenticator-held keypair), not a federated
identity. The device holds the private key and never reveals it; the gateway
stores only the PUBLIC key and verifies signatures. So unlike password auth there
is no shared secret to steal, and unlike social sign-in there is no third-party
token to trust — the trust root is a signature the device produced over a
challenge WE issued.

Two ceremonies, each a start/finish pair:
  * REGISTER  — the authenticator mints a new keypair (attestation). finish
    verifies the attestation and yields the credential material; the account +
    credential are then created directly (users_service.create_passkey_account,
    Design 04 Step 1) — no provisioning token, no /social/claim handle pick, so a
    credential can never be orphaned by a rejected handle claim (#1728).
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
import unicodedata

import webauthn
from cryptography.exceptions import InvalidSignature
from webauthn.helpers import (
    base64url_to_bytes, bytes_to_base64url, decode_credential_public_key,
    decoded_public_key_to_cryptography, parse_authentication_credential_json,
    verify_signature,
)
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse, InvalidBackupFlags, InvalidJSONStructure,
    InvalidRegistrationResponse, UnsupportedPublicKey,
)
from webauthn.helpers.structs import (
    AuthenticationCredential,
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
        delete(PasskeyChallenge).where(PasskeyChallenge.expires_at <= _utcnow())
        .execution_options(synchronize_session=False))
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
        # synchronize_session=False (claude-tasks#4486): this consume folds the
        # predicate into the write, so the DB must evaluate it — re-running it in
        # Python against the identity map both breaks that atomicity and compares
        # a SQLite-naive expires_at to an aware _utcnow(), raising TypeError.
        #
        # `False`, not `'fetch'`: a copy of this row already in the map keeps
        # `consumed=False` after the UPDATE has burned it. Safe while this
        # function reads only `rowcount` — a stale-object guarantee, not an
        # absent one.
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return None
    return base64url_to_bytes(state)


# --- ceremony start (mint options + challenge) ----------------------------- #

# The longest label a caller may put on its own passkey. Authenticators may truncate
# user.name to 64 BYTES; a 64-character cap keeps the label recognisable where they do.
_PASSKEY_LABEL_MAX = 64
# Control characters, plus the bidi overrides and isolates (U+202A-202E, U+2066-2069),
# which can make a label read differently from what it is. ZWJ (U+200D) is NOT here:
# it is what joins emoji sequences, and a handle may be one.
_LABEL_STRIP = frozenset(map(chr, [*range(0x202A, 0x202F), *range(0x2066, 0x206A)]))


def passkey_label(raw: object) -> str | None:
    """The label a caller asked for on its passkey, cleaned, or None to use the random
    default. Absent, non-string, empty after cleaning, or longer than
    _PASSKEY_LABEL_MAX all answer None: the label is cosmetic, so a bad one falls back
    rather than failing a ceremony that is also add-a-device's start."""
    if not isinstance(raw, str):
        return None
    cleaned = "".join(
        ch for ch in raw
        if unicodedata.category(ch) != "Cc" and ch not in _LABEL_STRIP
    ).strip()
    if not cleaned or len(cleaned) > _PASSKEY_LABEL_MAX:
        return None
    return cleaned


async def start_registration(session: AsyncSession, *, label: str | None = None) -> dict:
    """Mint a registration ceremony for an ANONYMOUS caller (first-passkey-creates-
    account). Returns {state, options} — options is the raw WebAuthn-JSON the
    platform authenticator parses. resident_key REQUIRED so the credential is
    discoverable (usernameless authentication later).

    `label` (from `passkey_label`) names the passkey in the phone's password manager:
    the handle a new user is about to choose, or a signed-in user's handle when adding
    a device. It is a label on the caller's own device and grants nothing."""
    raw = secrets.token_bytes(32)
    # The WebAuthn user_name/display_name are COSMETIC ceremony metadata (shown in the
    # OS passkey list) — distinct from the account handle, which register/finish
    # auto-generates when it creates the account (create_passkey_account). Without a
    # caller label, a random provisional name just avoids collisions in that OS list.
    # (That random name is `aiko-<8 hex>` and the account's auto handle is
    # `aiko-<12 hex>`: they were never the same string, which is why a label is worth
    # sending.)
    name = label or f"aiko-{secrets.token_hex(4)}"
    #
    # user_id is the WebAuthn user HANDLE. It is EPHEMERAL here: we never persist it
    # and authenticate looks credentials up by credential_id, so its only constraint
    # is the spec's 1..64-byte range. py_webauthn's default (when user_id is omitted)
    # is generate_user_handle() = 64 bytes — the spec MAXIMUM — which Android's
    # Credential Manager rejects at the boundary with TYPE_DATA_ERROR (#1957). Pin an
    # explicit 16-byte handle (22 base64url chars, well under Android's limit).
    options = webauthn.generate_registration_options(
        rp_id=settings.passkey_rp_id,
        rp_name=settings.passkey_rp_name,
        user_name=name,
        user_display_name=name,
        user_id=secrets.token_bytes(16),
        challenge=raw,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=_uv_requirement(),
        ),
        # Use py_webauthn's default alg set {ES256(-7), EdDSA(-8), RS256(-257)}.
        # PR#98 narrowed this to drop -8, on the theory that Android's Credential
        # Manager hard-rejects EdDSA at the create boundary — but that diagnosis
        # was REFUTED (#52): the real Android CREATE blocker was the assetlinks
        # `handle_all_urls` relation ([50152] RP-ID, fixed 2026-07-28), not the alg
        # set. With the narrowing's justification gone, the default is restored so
        # EdDSA-capable authenticators aren't needlessly excluded. Broadening back
        # can't regress iOS/browsers (they already pick ES256). Sole registration-
        # options call; the add-passkey path reuses this ceremony.
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
    (base64url strings) ready to persist directly via create_passkey_account.
    The expected_challenge/origin/rp_id are OURS, never read from the response.

    Raises exactly two things, both the caller's fault (the route maps them to 4xx):
    InvalidJSONStructure when the credential isn't a registration response at all
    (missing rawId/response, wrong type), and InvalidRegistrationResponse for
    everything else. Everything else is broad ON PURPOSE: py_webauthn 3.0.0 leaves
    the COSE key decode, the attestation-statement verifiers and the backup-flag
    parse unwrapped, so a fmt:"none" body that needs no signature can raise
    KeyError, TypeError, ValueError, binascii.Error, UnsupportedPublicKeyType or
    InvalidBackupFlags (measured), and each attestation format fails its own way,
    so a list of types would leak whichever one nobody measured. py_webauthn
    already wraps its own clientDataJSON and attestationObject parses like this.

    Only py_webauthn's frames are inside the catch. OUR inputs (the serialised
    body, rp_id, origins, the UV setting) are computed first, so a fault BUILDING
    them stays a loud 500 instead of a 401 that blames the device. A well-formed
    but WRONG value (a mistyped rp_id) still reads as a refused response: at
    request time it is indistinguishable from a device presenting the wrong RP."""
    payload = json.dumps(credential)
    rp_id = settings.passkey_rp_id
    origins = _expected_origins()
    require_uv = settings.passkey_require_user_verification
    try:
        verified = webauthn.verify_registration_response(
            credential=payload,
            expected_challenge=raw_challenge,
            expected_rp_id=rp_id,
            expected_origin=origins,
            require_user_verification=require_uv,
        )
        _assert_can_verify(verified.credential_public_key)
    except (InvalidJSONStructure, InvalidRegistrationResponse):
        raise
    except Exception as exc:
        raise InvalidRegistrationResponse(
            f"registration response rejected ({type(exc).__name__}: {str(exc)[:120]})"
        ) from exc
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


def _assert_can_verify(credential_public_key: bytes) -> None:
    """Refuse a key that no sign-in could ever verify (#421). py_webauthn's
    registration decodes the COSE key's fields but never uses the key, so an
    off-curve point, an unknown curve, or an `alg` that doesn't fit the key type
    (an EC2 key labelled RS256) all register and then fail every sign-in.

    Run the SAME three calls authentication runs (verify_authentication_response
    .py:174-183: decode, load, verify_signature with the key's own alg) against an
    empty signature. The one acceptable answer is InvalidSignature: the key could
    be asked. Anything else propagates and is folded into a refusal by the caller.
    Being the same calls, not a list of known-bad shapes, is what makes "stored"
    imply "verifiable"."""
    decoded = decode_credential_public_key(credential_public_key)
    try:
        verify_signature(
            public_key=decoded_public_key_to_cryptography(decoded),
            signature_alg=decoded.alg, signature=b"", data=b"")
    except InvalidSignature:
        return
    raise UnsupportedPublicKey("an empty signature verified")  # unreachable in practice


def parse_assertion(credential: dict) -> AuthenticationCredential:
    """Parse the caller's assertion body: the caller-only phase of authentication,
    run BEFORE the stored credential is looked up. Raises InvalidJSONStructure on
    any failure (the route maps it to 400), whatever py_webauthn raised: 3.0.0
    base64-decodes `userHandle` outside its own wrapper, so `"userHandle": "a"`
    raised a bare binascii.Error. Nothing here touches anything of ours, so
    folding every failure is safe. Parsing first also means a malformed body gets
    the same answer whether or not its credential id exists."""
    try:
        return parse_authentication_credential_json(json.dumps(credential))
    except InvalidJSONStructure:
        raise
    except Exception as exc:
        raise InvalidJSONStructure(
            f"assertion could not be parsed ({type(exc).__name__})") from exc


def verify_authentication(
    *, raw_challenge: bytes, assertion: AuthenticationCredential,
    public_key_b64: str, current_sign_count: int,
) -> int:
    """Verify a parsed assertion against the STORED public key and return the new
    sign_count to persist. Raises webauthn InvalidAuthenticationResponse on a bad
    signature / origin / rp_id / challenge, AND on a clone-indicating sign_count
    regression (py_webauthn enforces the spec-correct rule — only when the counts
    are nonzero; a 0/0 platform authenticator is permitted, see module docstring).

    Unlike registration this is NOT broadened: the public key here is the one WE
    stored, so a key that fails to decode is a server-side fault and must stay a
    loud 500, never a 401 that blames the device. The caller's body was already
    parsed by parse_assertion; the one caller-controlled escape left is the
    backup-flag check, which py_webauthn runs AFTER the signature (BS set without
    BE), folded into InvalidAuthenticationResponse."""
    try:
        verified = webauthn.verify_authentication_response(
            credential=assertion,
            expected_challenge=raw_challenge,
            expected_rp_id=settings.passkey_rp_id,
            expected_origin=_expected_origins(),
            credential_public_key=base64url_to_bytes(public_key_b64),
            credential_current_sign_count=current_sign_count,
            require_user_verification=settings.passkey_require_user_verification,
        )
    except InvalidBackupFlags as exc:
        raise InvalidAuthenticationResponse(str(exc)) from exc
    return verified.new_sign_count


# --- credential persistence ------------------------------------------------ #

def credential_id_of(assertion: AuthenticationCredential) -> str:
    """The parsed assertion's raw id in the same unpadded-base64url form we store
    at registration (so the lookup key matches regardless of the client's
    padding). py_webauthn rejects an assertion whose `id` and `rawId` disagree."""
    return bytes_to_base64url(assertion.raw_id)


async def get_credential(
    session: AsyncSession, credential_id_b64: str,
) -> PasskeyCredential | None:
    return (await session.execute(
        select(PasskeyCredential).where(
            PasskeyCredential.credential_id == credential_id_b64)
    )).scalar_one_or_none()


async def purge_user_credentials(session: AsyncSession, user_id: str) -> None:
    """Delete all passkey credentials for a user — called from account deletion
    (children-before-parent; this codebase has no ON DELETE CASCADE). Does NOT
    commit: the caller owns the deletion transaction (mirrors
    devices_service.purge_user_devices / moderation_service.purge_user_moderation_rows).
    A leaked DB yields only PUBLIC keys, but a credential row outliving its user is
    still residual account data — and with FK enforcement on it would block the
    final User delete. Challenges need no purge: passkey_challenges has no user_id."""
    await session.execute(
        delete(PasskeyCredential).where(PasskeyCredential.user_id == user_id)
    )
