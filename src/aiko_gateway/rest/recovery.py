"""Social-recovery endpoints — guardian approval quorum (Design 05).

The re-bind-a-passkey-after-losing-your-device trust surface. All enforcement lives
in recovery_service (the single door); this layer is boundary validation + the
commit boundary + log-safe tracing, mirroring rest/auth.py's passkey ceremonies.

  enroll / policy / cancel / status  → CurrentUser (a live authenticated device)
  recover/start|finish|finalize      → UNauthenticated (the whole point is you lost
                                        the device), rate-limited under "passkey"

The recover/* endpoints are gated on the SAME rate_limit("passkey") bucket as the
passkey ceremonies, so an attacker can't get extra budget by rotating between the
recover and register/authenticate endpoints. Deploy-dark: no /providers
advertisement (the app drives these directly once it ships the guardian UX).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from webauthn.helpers.exceptions import InvalidRegistrationResponse

from ..domain import recovery_service
from ..domain.rate_limit import rate_limit
from .auth import _short
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1/auth", tags=["recovery"])

log = logging.getLogger(__name__)

# Field caps (untrusted input on a wire boundary). A Multikey pubkey is ~48 chars; a
# base64url raw-64 sig is ~86 chars. 128 matches signing._MAX_PUBKEY/_SIG_STR.
_MAX_KEY_STR = 128
_MAX_SIG_STR = 128
# Cap the approver-pubkey list and the approvals list to the service's MAX_APPROVERS
# so an oversized payload is rejected at the boundary, never reaching the DB / verify.
_MAX_APPROVERS = recovery_service.MAX_APPROVERS
# Server nonce is a base64url(32 bytes) state handle → ~43 chars; the challenge
# `state` column is String(64). Cap at 64.
_MAX_NONCE = 64
# A ULID recovery_id is 26 chars; a token_urlsafe(32) finalize token ~43 chars.
_MAX_RECOVERY_ID = 64
_MAX_TOKEN = 128


# --- enroll / policy (authed) ---------------------------------------------- #

class EnrollReq(BaseModel):
    approver_pubkeys: list[str] = Field(min_length=1, max_length=_MAX_APPROVERS)
    k: int = Field(ge=1, le=_MAX_APPROVERS)


@router.post("/recovery/enroll")
async def recovery_enroll(
    req: EnrollReq, user: CurrentUser, session: DbSession,
) -> dict:
    """Enroll a recovery policy (k-of-n guardians) for the authenticated user. FIRST
    enrollment is immediate; a replacement must go through the (not-yet-online) veto
    rotation flow — a silent guardian swap is refused (409), never applied."""
    # Per-string caps beyond the list-length cap (each pubkey bounded before it
    # reaches decode_multikey's bigint loop; the service validates shape).
    for pk in req.approver_pubkeys:
        if not isinstance(pk, str) or len(pk) > _MAX_KEY_STR:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "approver pubkey too long")
    try:
        result = await recovery_service.enroll_policy(
            session, user_id=user.id,
            approver_pubkeys=req.approver_pubkeys, k=req.k)
    except recovery_service.InvalidEnrollment as e:
        await session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    except recovery_service.RecoveryError as e:
        # Existing policy → refuse-silent-swap (409). The service commits nothing on
        # this path, but roll back defensively before reusing the session.
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    log.info("recovery.enroll: OK user=%s n=%s k=%s",
             user.id, result["approver_count"], result["threshold_k"])
    return result


@router.get("/recovery/policy")
async def recovery_policy(user: CurrentUser, session: DbSession) -> dict:
    """The authenticated user's current recovery policy (threshold + approver
    labels/pubkeys), or {enrolled:false}. Own-account only (CurrentUser)."""
    policy, approvers = await recovery_service.get_policy(session, user.id)
    if policy is None:
        return {"enrolled": False}
    return {
        "enrolled": True,
        "threshold_k": policy.threshold_k,
        "approvers": [
            {"approver_pubkey": a.approver_pubkey, "label": a.label}
            for a in approvers
        ],
    }


# --- recover/start|finish|finalize (unauthenticated, rate-limited) --------- #

class RecoverStartReq(BaseModel):
    # The account being recovered. The authed enroll path binds a policy to a user;
    # start needs to name that user. Uses the SAME access-token-less identification
    # the login path already reveals (handle existence) — no social-graph oracle.
    account_handle: str = Field(min_length=1, max_length=64)


class Approval(BaseModel):
    approver_pubkey: str = Field(min_length=1, max_length=_MAX_KEY_STR)
    sig: str = Field(min_length=1, max_length=_MAX_SIG_STR)


class RecoverFinishReq(BaseModel):
    account_handle: str = Field(min_length=1, max_length=64)
    server_nonce: str = Field(min_length=1, max_length=_MAX_NONCE)
    new_credential_attestation: dict
    approvals: list[Approval] = Field(min_length=1, max_length=_MAX_APPROVERS)


class RecoverFinalizeReq(BaseModel):
    recovery_id: str = Field(min_length=1, max_length=_MAX_RECOVERY_ID)
    finalize_token: str = Field(min_length=1, max_length=_MAX_TOKEN)


async def _user_by_handle(session, handle: str):
    """Resolve a user by their username handle (the login-visible identifier). Local
    import of users_service kept out of module scope to preserve the test-isolation
    invariant (no eager aiko/bus imports)."""
    from sqlalchemy import select

    from ..domain.models import User
    return (await session.execute(
        select(User).where(User.username == handle))).scalar_one_or_none()


@router.post("/passkey/recover/start", dependencies=[rate_limit("passkey")])
async def recover_start(req: RecoverStartReq, session: DbSession) -> dict:
    """Issue a single-use server nonce for a recovery of `account_handle`. Returns
    {server_nonce, threshold_k}. Reveals only the threshold — never approver
    identities. A missing account or no-policy account gets the SAME 404 (no
    enumeration difference beyond what login already exposes)."""
    user = await _user_by_handle(session, req.account_handle)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no recovery available")
    try:
        result = await recovery_service.start_recovery(session, user)
    except recovery_service.NoRecoveryPolicy:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no recovery available")
    log.info("recovery.start: nonce issued user=%s nonce=%s",
             user.id, _short(result.get("server_nonce")))
    return result


@router.post("/passkey/recover/finish", dependencies=[rate_limit("passkey")])
async def recover_finish(req: RecoverFinishReq, session: DbSession) -> dict:
    """Verify the new passkey attestation + a guardian quorum bound to it, then open
    a pending recovery (veto window). Returns {status:'pending', recovery_id,
    veto_deadline, finalize_token} — NO session yet."""
    user = await _user_by_handle(session, req.account_handle)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no recovery available")
    approvals = [a.model_dump() for a in req.approvals]
    try:
        result = await recovery_service.finish_recovery(
            session, user=user, server_nonce=req.server_nonce,
            new_credential_attestation=req.new_credential_attestation,
            approvals=approvals)
    except recovery_service.NoRecoveryPolicy:
        await session.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no recovery available")
    except InvalidRegistrationResponse:
        # A malformed / failed new-passkey attestation. The nonce burn is deferred
        # (uncommitted) so rolling back lets an honest retry reuse the ceremony.
        await session.rollback()
        log.warning("recovery.finish: REJECT attestation verification failed user=%s",
                    user.id)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "passkey registration verification failed")
    except recovery_service.RecoveryAlreadyPending:
        # The service already rolled back the nonce burn (a live pending row must not
        # advance its deadline). 409 — the owner must wait out or cancel the live one.
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a recovery is already pending")
    except recovery_service.QuorumRejected:
        # Constant-shape rejection (bad nonce / <k / non-distinct / bad sig / non-
        # approver key all land here). Roll back the deferred burn. Same opaque 401 —
        # never leak which check failed (commitment-existence oracle defense).
        await session.rollback()
        log.warning("recovery.finish: REJECT quorum not satisfied user=%s", user.id)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "recovery could not be authorized")
    # Durable AFTER reading the response view: the nonce burn + pending insert commit
    # atomically with the outcome (the #24 contract).
    await session.commit()
    log.info("recovery.finish: pending opened user=%s recovery_id=%s deadline=%s",
             user.id, result["recovery_id"], result["veto_deadline"])
    return result


@router.post("/passkey/recover/finalize", dependencies=[rate_limit("passkey")])
async def recover_finalize(req: RecoverFinalizeReq, session: DbSession) -> dict:
    """Poll to finalize a recovery whose veto window has passed. The guarded DELETE
    (deadline + token hash in the WHERE) either wins (issues a session, re-keys) or
    matches nothing (cancelled / too-early / wrong-token / already-finalized) → a
    clean terminal 409. Idempotent: a double poll yields one session, one credential,
    no IntegrityError."""
    outcome = await recovery_service.finalize_recovery(
        session, recovery_id=req.recovery_id, finalize_token=req.finalize_token)
    if outcome is None:
        # No finalizable row: cancelled, absent, too-early, wrong token, or already
        # finalized. A uniform 409 — the client stops polling / surfaces "recovery
        # unavailable". (finalize_recovery commits any account-vanished cleanup.)
        raise HTTPException(
            status.HTTP_409_CONFLICT, "recovery not finalizable")
    log.info("recovery.finalize: OK user=%s (re-keyed, session issued)",
             outcome["user"]["user_id"])
    return outcome


# --- cancel / status (authed) ---------------------------------------------- #

@router.post("/passkey/recover/cancel")
async def recover_cancel(user: CurrentUser, session: DbSession) -> dict:
    """Owner veto from a live authenticated device: delete the pending recovery for
    the authenticated user. A cancel that lands before finalize makes finalize's
    guarded DELETE match 0 rows (owner provably wins the race, §7). Returns
    {cancelled: bool}."""
    cancelled = await recovery_service.cancel_recovery(session, user.id)
    await session.commit()
    log.info("recovery.cancel: user=%s cancelled=%s", user.id, cancelled)
    return {"cancelled": cancelled}


@router.get("/recovery/status")
async def recovery_status_endpoint(user: CurrentUser, session: DbSession) -> dict:
    """Whether a recovery is pending for the authenticated user (the veto-
    notification floor, §7). A live device polls this to learn it must veto."""
    return await recovery_service.recovery_status(session, user.id)
