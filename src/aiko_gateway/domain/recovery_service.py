"""Social recovery — guardian approval quorum (Design 05).

Recovery is an ACCOUNT-TAKEOVER primitive by construction: whoever can re-bind a
passkey owns the account. This module makes the authority to do that a LIVE quorum
of the owner's chosen guardians — never the island. The gateway stores only n opaque
approver PUBLIC keys + a threshold k, VERIFIES approval signatures, and never holds
anything that can recover an account alone.

The five properties the design temper (Design 05 §3) demands, each realized here:

  * BINDING (fixes C1) — every guardian signature is over domain-separated bytes that
    COMMIT to the specific new credential (approval_bytes below). The gateway
    RECOMPUTES those bytes from the attestation it just verified, so a captured
    approval cannot authorize a DIFFERENT credential.
  * GUARDED FINALIZE (fixes C2) — finalize is a single guarded DELETE with the
    veto_deadline folded into the WHERE, arbitrated by rowcount (never observe-then-
    write). A cancel that lands first deletes the row → finalize matches 0 rows →
    fail closed. There is no interleaving where both an owner-cancel and a finalize
    win. Mirrors passkey_service.consume_challenge / signing_keys._capped_insert.
  * WRITE-ONCE DEADLINE — veto_deadline is set at row birth and never advanced; a
    second finish while a row is live is rejected, and an EXPIRED-unfinalized row is
    reclaimable by a fresh recovery, so UNIQUE(user_id) can't be weaponized into a
    lockout.
  * ONE CLOCK — server wall-clock _utcnow() (tz-aware) is the sole deadline clock in
    both finish and finalize; no client time, no monotonic (recall #64).
  * SINGLE DOOR — the staged credential is enrolled through the SAME
    users_service.link_passkey_credential door as a normal add, so credential_id
    uniqueness + clone detection can't drift.

CONSTANT-SHAPE ERRORS on the finish path: a no-such-approver-pubkey and a bad
signature raise the SAME QuorumRejected, so the response can't be a
commitment-existence oracle ("is THIS key one of my guardians?").

COMMIT CONVENTION: the mutators here DO NOT commit (the route owns the transaction),
EXCEPT where a comment says otherwise. purge_user_recovery mirrors
signing_keys_service.purge_user_keys (no commit — the account-deletion caller owns
the txn).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import struct

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from . import (
    passkey_service, security, signing, signing_keys_service, users_service)
from .ids import new_ulid
from .models import (
    PasskeyOperation, PendingRecovery, RecoveryApprover, RecoveryPolicy, User,
)

# Domain tag for the recovery approval signature. DISTINCT from signing.DOMAIN_TAG
# ("aikochat:msg:v1:EdDSA") so an approval is structurally un-replayable into the
# message-signing path (cross-protocol reuse defense, Design 05 §5).
RECOVER_DOMAIN_TAG = "aikochat:recover:v1"

# Bounds on the enroll payload (belt-and-braces; the route caps too). A policy with
# more than this many approvers is almost certainly abuse, not a real guardian set.
MAX_APPROVERS = 32


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --- errors (constant-shape on the finish path) ---------------------------- #

class RecoveryError(Exception):
    """Base for recovery-domain failures the route maps to a 4xx."""


class NoRecoveryPolicy(RecoveryError):
    """The account has no recovery policy — nothing to recover against."""


class QuorumRejected(RecoveryError):
    """The presented approvals did not satisfy the policy: fewer than k DISTINCT
    valid signatures over the recomputed approval_bytes. DELIBERATELY the SAME
    exception for a non-approver pubkey AND a bad signature — the finish response
    must not leak WHICH check failed (a commitment-existence oracle)."""


class RecoveryAlreadyPending(RecoveryError):
    """A live (unexpired) pending recovery already exists for this user — a second
    finish must not advance the write-once deadline (§7)."""


class InvalidEnrollment(RecoveryError):
    """The enroll payload is not a valid policy (k out of range, empty/oversized
    approver set, non-distinct approvers)."""


# --- signed bytes (the binding — fixes C1) --------------------------------- #

def approval_bytes(
    *, server_nonce: bytes, account_id: str, new_credential_id: str,
    new_public_key: str,
) -> bytes:
    """The domain-separated, length-prefixed bytes each guardian signs — mirrors
    signing.signing_bytes (u32 big-endian length prefix per field), with a DISTINCT
    domain tag. Committing to new_credential_id + new_public_key is what makes an
    approval authorize THIS recovery installing THIS credential, so a captured
    approval can't be replayed to install a different one (§5).

    The gateway recomputes these from the attestation it JUST verified (the material
    dict from passkey_service.verify_registration), never from client-echoed values,
    so the signed bytes are the server's own view of what is being installed."""
    def lp(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b

    return b"".join((
        lp(RECOVER_DOMAIN_TAG.encode()),
        lp(server_nonce),
        lp(account_id.encode()),
        lp(new_credential_id.encode()),
        lp(new_public_key.encode()),
    ))


def _finalize_token() -> tuple[str, str]:
    """Mint a high-entropy finalize token and its sha256 hex. Return (raw, hash) —
    the raw is handed to the caller ONCE at finish; only the hash is persisted."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# --- policy read/enroll ---------------------------------------------------- #

async def get_policy(
    session: AsyncSession, user_id: str,
) -> tuple[RecoveryPolicy | None, list[RecoveryApprover]]:
    """The user's active policy + its approver rows (approvers empty if no policy).
    Ordered by created_at then id for a deterministic view."""
    policy = (await session.execute(
        select(RecoveryPolicy).where(RecoveryPolicy.user_id == user_id))
    ).scalar_one_or_none()
    if policy is None:
        return None, []
    approvers = list((await session.execute(
        select(RecoveryApprover).where(RecoveryApprover.user_id == user_id)
        .order_by(RecoveryApprover.created_at, RecoveryApprover.id)
    )).scalars())
    return policy, approvers


def _validate_enrollment(approver_pubkeys: list[str], k: int) -> list[str]:
    """Validate + normalize the enroll payload. Returns the DISTINCT pubkey list.
    Raises InvalidEnrollment on any violation (fail closed):
      * each pubkey a well-formed ed25519 Multikey (decode_multikey raises otherwise);
      * distinct approvers (a duplicate in the payload is rejected, not silently
        deduped — the client asked for something incoherent);
      * 1 <= k <= n and n within MAX_APPROVERS."""
    if not approver_pubkeys:
        raise InvalidEnrollment("at least one approver is required")
    if len(approver_pubkeys) > MAX_APPROVERS:
        raise InvalidEnrollment(
            f"too many approvers (max {MAX_APPROVERS})")
    seen: set[str] = set()
    for pk in approver_pubkeys:
        try:
            signing.decode_multikey(pk)
        except signing.OriginError as e:
            raise InvalidEnrollment(f"approver pubkey is not a valid Multikey: {e}")
        if pk in seen:
            raise InvalidEnrollment("approver pubkeys must be distinct")
        seen.add(pk)
    n = len(approver_pubkeys)
    if not (1 <= k <= n):
        raise InvalidEnrollment(
            f"threshold k={k} out of range (must be 1..{n})")
    return approver_pubkeys


async def enroll_policy(
    session: AsyncSession, *, user_id: str, approver_pubkeys: list[str], k: int,
) -> dict:
    """Enroll or ROTATE a recovery policy (Design 05 §6).

    FIRST enrollment (no existing policy): stored immediately and committed.

    REPLACING an existing policy (rotation): routed through the SAME notify + veto
    machinery a recovery uses (a momentary session theft must not silently swap the
    owner's guardians — §4 session-thief row). This is a NAMED intended coupling.

    IMPLEMENTATION NOTE / for-cage-match: the full rotation-veto (stage the new
    policy, notify, let the owner cancel, finalize the swap after the window) is a
    larger state machine than the recovery veto and is scoped as a follow-up. To
    honor the "do NOT silently swap" invariant WITHOUT that machinery yet, this
    REFUSES a silent rotation: an existing policy cannot be replaced through this
    endpoint (raises RecoveryAlreadyPending-shaped RecoveryError). The app must
    explicitly go through the (future) rotation flow. This fails CLOSED — the unsafe
    silent-swap window is never entered — at the cost of not yet SUPPORTING rotation
    online. See §11 open questions. A replacement is therefore currently a
    delete-then-re-enroll the owner performs deliberately (there is no delete
    endpoint yet either — also a named follow-up), NOT an implicit overwrite."""
    approver_pubkeys = _validate_enrollment(approver_pubkeys, k)
    existing = (await session.execute(
        select(RecoveryPolicy).where(RecoveryPolicy.user_id == user_id))
    ).scalar_one_or_none()
    if existing is not None:
        # Do NOT silently swap the guardians (§4). Rotation-through-veto is a named
        # follow-up; until it lands, refuse rather than overwrite in place. Fail
        # closed — the silent-swap window is never entered.
        raise RecoveryError(
            "a recovery policy already exists; rotation must go through the veto "
            "flow (not yet implemented) — cannot silently replace guardians")
    policy = RecoveryPolicy(
        id=new_ulid(), user_id=user_id, threshold_k=k, created_at=_utcnow())
    session.add(policy)
    now = _utcnow()
    for pk in approver_pubkeys:
        session.add(RecoveryApprover(
            id=new_ulid(), user_id=user_id, approver_pubkey=pk,
            label=None, created_at=now))
    await session.commit()
    return {"threshold_k": k, "approver_count": len(approver_pubkeys)}


# --- start (issue the single-use server nonce) ----------------------------- #

async def start_recovery(session: AsyncSession, user: User) -> dict:
    """Issue a single-use server nonce for a recovery, reusing the PasskeyChallenge
    store (operation='recover'). Returns {server_nonce (base64url state), threshold_k}.
    Does NOT reveal approver identities (no social-graph oracle) — only the threshold.

    NAMED DISCLOSURE (cage-match Tesla, PR#69): returning threshold_k reveals to any
    caller who knows a handle whether that account has recovery ENROLLED and its k.
    Accepted: the recovering owner needs k, approver IDENTITIES are never revealed, and
    handle-existence already leaks via the login path. Not a C1/C2 concern. See
    Design 05 §4/§11.

    Requires an existing policy (else NoRecoveryPolicy). The nonce is a
    PasskeyChallenge row consumed atomically at finish — the same guarded-UPDATE
    single-use mechanism as a register/authenticate challenge."""
    policy = (await session.execute(
        select(RecoveryPolicy).where(RecoveryPolicy.user_id == user.id))
    ).scalar_one_or_none()
    if policy is None:
        raise NoRecoveryPolicy("account has no recovery policy")
    raw = secrets.token_bytes(32)
    # _store_challenge commits; mirrors passkey_service.start_registration.
    state = await passkey_service._store_challenge(
        session, raw=raw, operation=PasskeyOperation.RECOVER)
    return {"server_nonce": state, "threshold_k": policy.threshold_k}


# --- finish (verify quorum, open the pending veto row) --------------------- #

def _verify_quorum(
    *, approvers: list[RecoveryApprover], approvals: list[dict], msg: bytes,
    threshold_k: int,
) -> None:
    """Verify >= k DISTINCT approver signatures over `msg` (the recomputed
    approval_bytes). Raises QuorumRejected on ANY shortfall — SAME exception whether
    a presented pubkey is not a registered approver or its signature is bad
    (constant-shape: no commitment-existence oracle).

    DISTINCT enforcement: an approver's pubkey counts AT MOST ONCE toward the quorum,
    so one guardian presenting k signatures (or the same signature k times) does not
    satisfy a k-threshold. Verification is against the STORED approver pubkey's raw
    ed25519 key — signing.decode_multikey gives the 32 raw bytes.

    Each approval is {approver_pubkey, sig} with sig unpadded-base64url of 64 bytes;
    a malformed approval simply fails to verify (no separate error class)."""
    registered = {a.approver_pubkey for a in approvers}
    satisfied: set[str] = set()
    for approval in approvals:
        if not isinstance(approval, dict):
            continue
        pk = approval.get("approver_pubkey")
        sig_b64 = approval.get("sig")
        if not isinstance(pk, str) or not isinstance(sig_b64, str):
            continue
        if pk in satisfied:
            continue  # already counted — distinct-only
        if pk not in registered:
            continue  # not a guardian of this account — but DON'T raise yet (oracle)
        # Decode the stored approver pubkey to raw ed25519, and the presented sig to
        # 64 raw bytes. Any decode failure = this approval doesn't count.
        try:
            raw_pub = signing.decode_multikey(pk)
            sig = signing._b64url_raw(
                sig_b64, expect_len=signing.SIG_RAW_LEN, field="approval.sig")
        except signing.OriginError:
            continue
        try:
            Ed25519PublicKey.from_public_bytes(raw_pub).verify(sig, msg)
        except InvalidSignature:
            continue
        except Exception:
            # from_public_bytes can raise on a malformed 32-byte key; treat as a
            # non-counting approval, never a 500.
            continue
        satisfied.add(pk)
        if len(satisfied) >= threshold_k:
            return
    # Fewer than k distinct valid approvals — constant-shape rejection.
    raise QuorumRejected("recovery quorum not satisfied")


async def finish_recovery(
    session: AsyncSession, *, user: User, server_nonce: str,
    new_credential_attestation: dict, approvals: list[dict],
) -> dict:
    """Verify the new passkey attestation + a quorum of guardian approvals bound to
    it, then open a pending_recovery row with a write-once veto_deadline. Returns
    {status:'pending', recovery_id, veto_deadline, finalize_token} — NO session yet.

    Order (all fail-closed):
      (a) consume the recover nonce atomically (single-use — a replayed finish finds
          it burned);
      (b) verify the new passkey attestation (py_webauthn) → verified material;
      (c) recompute approval_bytes from THAT material (never client-echoed values)
          and verify >= k distinct approver sigs (constant-shape rejection);
      (d) reclaim-on-expiry / reject-if-live: if a live pending row exists, reject
          (write-once deadline, §7); if an EXPIRED one exists, delete it first so the
          UNIQUE(user_id) slot is reusable;
      (e) insert the pending row (deadline = now + window) and return the token.

    Does NOT commit — the route commits after reading the response view, so the nonce
    burn + pending insert land atomically with the outcome (the #24 contract)."""
    policy, approvers = await get_policy(session, user.id)
    if policy is None:
        raise NoRecoveryPolicy("account has no recovery policy")

    # (a) single-use nonce burn (atomic guarded UPDATE; does not commit).
    raw = await passkey_service.consume_challenge(
        session, server_nonce, PasskeyOperation.RECOVER)
    if raw is None:
        raise QuorumRejected("invalid or expired recovery nonce")

    # (b) verify the new passkey attestation — the credential being installed.
    #     Raises InvalidRegistrationResponse (a webauthn error) on failure; the route
    #     maps it. We deliberately let it propagate rather than reshape it, since a
    #     malformed attestation is a distinct client error from a quorum shortfall.
    material = passkey_service.verify_registration(
        raw_challenge=raw, credential=new_credential_attestation)

    # (c) recompute the signed bytes from the VERIFIED material + the burned nonce,
    #     then verify the quorum. The binding: approvals authorize THIS credential.
    msg = approval_bytes(
        server_nonce=raw, account_id=user.id,
        new_credential_id=material["credential_id"],
        new_public_key=material["public_key"])
    if not isinstance(approvals, list):
        raise QuorumRejected("recovery quorum not satisfied")
    _verify_quorum(
        approvers=approvers, approvals=approvals, msg=msg,
        threshold_k=policy.threshold_k)

    # (d) reclaim-on-expiry: a LIVE pending row rejects (write-once deadline); an
    #     EXPIRED one is deleted so the slot is reusable. A single guarded DELETE
    #     whose WHERE requires expiry does the reclaim atomically — never
    #     observe-then-write.
    now = _utcnow()
    await session.execute(
        delete(PendingRecovery).where(
            PendingRecovery.user_id == user.id,
            PendingRecovery.veto_deadline <= now))
    existing_live = (await session.execute(
        select(PendingRecovery.id).where(PendingRecovery.user_id == user.id))
    ).scalar_one_or_none()
    if existing_live is not None:
        # A still-live pending row remains after the expired-only delete — reject
        # (don't advance the deadline). Roll back the nonce burn so the honest owner
        # can retry once the live window clears.
        await session.rollback()
        raise RecoveryAlreadyPending("a recovery is already pending for this account")

    # (e) open the pending row. veto_deadline is write-once (set here, never advanced).
    raw_token, token_hash = _finalize_token()
    deadline = now + dt.timedelta(seconds=settings.recovery_veto_window_seconds)
    pending = PendingRecovery(
        id=new_ulid(), user_id=user.id,
        staged_credential_id=material["credential_id"],
        staged_public_key=material["public_key"],
        staged_sign_count=material["sign_count"],
        veto_deadline=deadline,
        finalize_token_hash=token_hash,
        created_at=now)
    # Insert inside a SAVEPOINT so a UNIQUE(user_id) collision from a concurrent
    # finish that raced past the live-check above surfaces HERE as a clean
    # RecoveryAlreadyPending (409), never a raw IntegrityError/500 at the route's
    # commit (cage-match Carnot+Tesla, PR#69). Mirrors signing_keys_service's
    # begin_nested guard; matters more if WAL (#1450) ever relaxes the single-writer
    # serialization the select-then-insert otherwise leans on.
    try:
        async with session.begin_nested():
            session.add(pending)
    except IntegrityError:
        await session.rollback()
        raise RecoveryAlreadyPending(
            "a recovery is already pending for this account")
    # NOTE: notification of existing devices is a named requirement (§7); the floor
    # is GET /v1/auth/recovery/status (the caller/app polls). Push (#1406) not built.
    return {
        "status": "pending",
        "recovery_id": pending.id,
        "veto_deadline": deadline.isoformat(),
        "finalize_token": raw_token,
    }


# --- cancel (owner vetoes from a live device) ------------------------------ #

async def cancel_recovery(session: AsyncSession, user_id: str) -> bool:
    """Owner veto: delete the pending recovery row for `user_id` (a guarded delete).
    Returns True if a row was deleted. Scoped to the AUTHENTICATED user's own row —
    the route requires CurrentUser (a live device). Does NOT commit — the route owns
    the txn. A cancel that lands before finalize deletes the row, so finalize's
    guarded DELETE matches 0 rows and fails closed (§7)."""
    result = await session.execute(
        delete(PendingRecovery).where(PendingRecovery.user_id == user_id))
    return result.rowcount > 0


# --- status (the veto-notification floor) ---------------------------------- #

async def recovery_status(session: AsyncSession, user_id: str) -> dict:
    """Whether a recovery is pending for the user, and (if so) its deadline. The
    floor notification surface (§7) an existing device polls to see it must veto."""
    pending = (await session.execute(
        select(PendingRecovery).where(PendingRecovery.user_id == user_id))
    ).scalar_one_or_none()
    if pending is None:
        return {"pending": False}
    return {
        "pending": True,
        "recovery_id": pending.id,
        "veto_deadline": pending.veto_deadline.isoformat(),
    }


# --- finalize (the guarded DELETE + re-key + session) ---------------------- #

async def finalize_recovery(
    session: AsyncSession, *, recovery_id: str, finalize_token: str,
) -> dict | None:
    """Finalize a recovery whose veto window has passed (Design 05 §7).

    THE guarded statement: a single DELETE with veto_deadline <= now folded into the
    WHERE, arbitrated by rowcount. On rowcount == 1 this caller won the race — it then
    re-asserts the account still EXISTS (not self-deleted), enrolls the staged
    credential through the SINGLE passkey door, revokes the old passkeys AND old
    signing keys (clean re-key on the new device), issues a session, and commits —
    ONE commit.

    SESSION-PLANE RESIDUAL (named — cage-match Tesla, PR#69): this revokes the old
    CREDENTIALS but NOT existing JWT sessions — sessions are stateless HS256 tokens
    with no revocation mechanism, so a lost/old device keeps riding its refresh token
    until it expires (refresh TTL = 30d). Acceptable ONLY because recovery is
    deploy-dark + un-invokable today. Per-user token_generation revocation
    (claude-tasks #1914) MUST land + be live-verified BEFORE recovery is enabled for
    real users — that is the lift condition. See Design 05 §10/§11.

    Idempotent: a later poll (or a lost first response) finds the row already gone,
    so the DELETE matches 0 rows and this returns None. The route maps None to a clean
    terminal state — a double finalize-poll yields one session + one bound credential,
    never an IntegrityError.

    Returns the outcome dict {access_token, refresh_token, user} on the winning call,
    or None if there was no matching finalizable row (cancelled, absent, too-early,
    wrong token, or already finalized). Commits on the winning path.

    NOTE on the token: the guarded DELETE matches on (id, finalize_token_hash,
    veto_deadline <= now) so a wrong token never deletes the row — fail closed. The id
    alone (a public ULID) can't finalize; the high-entropy token is required."""
    now = _utcnow()
    token_hash = _hash_token(finalize_token)
    # The single guarded statement. Deadline INSIDE the WHERE (server clock), token
    # hash INSIDE the WHERE (a wrong token matches 0 rows). RETURNING gives the staged
    # material only to the winner. SQLite (dev+prod) supports DELETE ... RETURNING.
    result = await session.execute(
        delete(PendingRecovery)
        .where(
            PendingRecovery.id == recovery_id,
            PendingRecovery.finalize_token_hash == token_hash,
            PendingRecovery.veto_deadline <= now)
        .returning(
            PendingRecovery.user_id,
            PendingRecovery.staged_credential_id,
            PendingRecovery.staged_public_key,
            PendingRecovery.staged_sign_count))
    row = result.first()
    if row is None:
        # rowcount != 1: cancelled, absent, too-early, wrong token, or already
        # finalized. Fail closed — the caller polls again or gives up. NOT a rollback
        # target (nothing was written); leave the txn to the route.
        return None
    user_id, cred_id, public_key, sign_count = row

    # Re-assert the account still EXISTS (not self-deleted) IN this transaction (§4
    # self-deleting row). A recovery must not resurrect a deleted account. NOTE
    # (cage-match Carnot+Tesla, PR#69): there is no user-level ban/suspension field in
    # the schema today, so existence IS the complete standing check. If such a flag is
    # ever added, it MUST also be checked here — else recovery re-binds a passkey and
    # mints a session for a suspended account (ban-evasion). Existence-only is honest
    # for the current schema, not a stronger "good standing" than the data supports.
    user = await users_service.get_by_id(session, user_id)
    if user is None:
        # The account vanished (self-deleted) between finish and finalize. The pending
        # row is already deleted by the guarded statement above (correct — a recovery
        # for a gone account is dead). Commit the cleanup and fail closed.
        await session.commit()
        return None

    # Enroll the staged credential through the SINGLE passkey door (no bespoke insert
    # — credential_id uniqueness + clone detection stay in one place). link_passkey_
    # credential COMMITS; so we must do the revokes + issue the view BEFORE it, then
    # let its commit be the single durable point. To keep it one commit we inline the
    # same row-build here and revoke first, committing once at the end.
    material = {
        "credential_id": cred_id, "public_key": public_key,
        "sign_count": sign_count, "transports": None, "aaguid": None,
    }
    # Revoke old passkeys AND old signing keys — a clean re-key onto the new device
    # (Design 05 §6). Go through the SAME purge doors account deletion uses (neither
    # commits — the caller owns the txn), so the revoke set can't drift from those.
    # Delete BEFORE inserting the new credential so the new one survives the sweep.
    await passkey_service.purge_user_credentials(session, user_id)
    await signing_keys_service.purge_user_keys(session, user_id)
    # Insert the new credential inside a SAVEPOINT. credential_id is globally UNIQUE
    # and the user's own creds were just purged, so a collision could only come from
    # ANOTHER account holding this authenticator handle (astronomically unlikely).
    # Guard it anyway (cage-match Carnot+Tesla, PR#69): a collision fails CLOSED — the
    # full rollback restores the pending row (the guarded DELETE + purges are undone),
    # so finalize is retryable and returns a clean terminal 409, never a raw 500 on
    # the takeover path.
    try:
        async with session.begin_nested():
            session.add(users_service._passkey_credential_row(user_id, material))
    except IntegrityError:
        await session.rollback()
        return None
    # Read the session view BEFORE commit (expire_on_commit / MissingGreenlet trap).
    outcome = {
        "access_token": security.issue_access(user.id),
        "refresh_token": security.issue_refresh(user.id),
        "user": {
            "user_id": user.id, "username": user.username,
            "display_name": user.display_name, "aiko_username": user.aiko_username,
        },
    }
    await session.commit()
    return outcome


# --- account-deletion teardown --------------------------------------------- #

async def purge_user_recovery(session: AsyncSession, user_id: str) -> None:
    """Delete all recovery rows for a user — called from account deletion
    (children-before-parent, no ON DELETE CASCADE in this codebase). Deletes
    pending_recovery + recovery_approvers + recovery_policies for the user. Does NOT
    commit: the caller owns the deletion transaction (mirrors
    signing_keys_service.purge_user_keys / passkey_service.purge_user_credentials)."""
    await session.execute(
        delete(PendingRecovery).where(PendingRecovery.user_id == user_id))
    await session.execute(
        delete(RecoveryApprover).where(RecoveryApprover.user_id == user_id))
    await session.execute(
        delete(RecoveryPolicy).where(RecoveryPolicy.user_id == user_id))
