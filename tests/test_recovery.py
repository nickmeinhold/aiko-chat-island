"""Social recovery — guardian approval quorum (Design 05).

The RED-prove list (Design 05 §12) as real tests, each a distinct account-takeover
property the gateway must hold:

  (a) cancel-lands-mid-finalize → owner wins (the guarded-DELETE race)
  (b) an EXPIRED attacker pending row does NOT lock out a fresh legit recovery
  (c) repeated finish never advances a live veto_deadline (write-once)
  (d) double finalize-poll → one session, one credential, no IntegrityError
  (e) a captured approval can't authorize a DIFFERENT credential (binding, fixes C1)
  (f) < k or non-distinct approvers → rejected
  (g) recovery of a banned/deleted account → fail closed
  (h) a leaked policy row alone recovers nothing (no approver privkeys)

Guardian approvers are real ed25519 keypairs generated in-test (cryptography lib) —
the SAME thing a guardian app would hold. The new passkey is minted by the same inline
P-256 SoftAuthenticator the passkey suite uses (REAL attestation through py_webauthn,
no crypto mocking). Built from JUST the recovery + auth routers (never `main`) to keep
the suite's 'never import aiko_services' isolation invariant.
"""
from __future__ import annotations

import base64
import datetime as dt

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from aiko_gateway.config import settings
from aiko_gateway.domain import (
    accounts_service, passkey_service, recovery_service, security,
    users_service,
)
from aiko_gateway.domain.models import (
    PasskeyCredential, PasskeyOperation, PendingRecovery, RecoveryApprover,
    RecoveryPolicy, SigningKey, User,
)
from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest import recovery as recovery_routes
from aiko_gateway.rest.deps import get_session

# Reuse the real inline authenticator from the passkey endpoint suite.
from test_passkey_endpoints import SoftAuthenticator


# --- multibase / ed25519 helpers (mirror test_signing_keys) ---------------- #

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(b: bytes) -> str:
    num = int.from_bytes(b, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _B58[rem] + out
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + out


def _multikey(raw_pub: bytes) -> str:
    """z + base58btc(0xed01 ‖ 32 raw bytes) — the same format decode_multikey reads."""
    return "z" + _b58encode(b"\xed\x01" + raw_pub)


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


class Guardian:
    """A guardian's ed25519 approver keypair. The public half is registered; the
    private half signs approval_bytes out-of-band (as a guardian app would)."""

    def __init__(self) -> None:
        self._key = Ed25519PrivateKey.generate()
        self.pubkey = _multikey(self._key.public_key().public_bytes_raw())

    def approve(self, msg: bytes) -> dict:
        return {"approver_pubkey": self.pubkey, "sig": _b64url(self._key.sign(msg))}


# --- app / client ---------------------------------------------------------- #

def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)      # passkey register/start etc.
    app.include_router(recovery_routes.router)
    return app


@pytest_asyncio.fixture
async def client(session):
    async def _override_session():
        yield session

    app = _build_app()
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _headers(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


async def _user(session, username="alice"):
    return await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")


async def _enroll(session, user, guardians, k):
    """Enroll a policy directly via the service (commits)."""
    await recovery_service.enroll_policy(
        session, user_id=user.id, approver_pubkeys=[g.pubkey for g in guardians], k=k)


def _new_attestation(auth: SoftAuthenticator, server_nonce_state: str) -> dict:
    """Build a REAL attestation over the recovery server nonce (the challenge is the
    base64url `state` handle the recover/start issued)."""
    return auth.register(server_nonce_state)


async def _pending_count(session, user_id) -> int:
    return (await session.execute(
        select(func.count()).select_from(PendingRecovery)
        .where(PendingRecovery.user_id == user_id))).scalar_one()


async def _cred_count(session, user_id) -> int:
    return (await session.execute(
        select(func.count()).select_from(PasskeyCredential)
        .where(PasskeyCredential.user_id == user_id))).scalar_one()


# ============================================================ service-level flow

async def _drive_finish(
    session, user, guardians, k, auth=None, wrong_credential=False,
):
    """start → build attestation → k approvals bound to it → finish. Returns the
    finish result dict (pending). `wrong_credential` (for binding test e) signs the
    approvals over a DIFFERENT credential than the one submitted."""
    auth = auth or SoftAuthenticator()
    start = await recovery_service.start_recovery(session, user)
    nonce_state = start["server_nonce"]
    attestation = _new_attestation(auth, nonce_state)
    raw_nonce = base64url_to_bytes(nonce_state)

    # The material the gateway WILL verify — compute the same credential id/pubkey it
    # will derive, to bind the approvals. verify_registration is deterministic on the
    # attestation, so we run it here to learn the exact bytes to sign.
    material = passkey_service.verify_registration(
        raw_challenge=raw_nonce, credential=attestation)

    if wrong_credential:
        # Bind approvals to a DIFFERENT credential (a captured approval for another
        # recovery). Same shape, wrong credential id/pubkey.
        other_auth = SoftAuthenticator(credential_id=b"other-credential-9999")
        other_start = await recovery_service.start_recovery(session, user)
        other_att = _new_attestation(other_auth, other_start["server_nonce"])
        other_mat = passkey_service.verify_registration(
            raw_challenge=base64url_to_bytes(other_start["server_nonce"]),
            credential=other_att)
        msg = recovery_service.approval_bytes(
            server_nonce=raw_nonce, account_id=user.id,
            new_credential_id=other_mat["credential_id"],
            new_public_key=other_mat["public_key"])
    else:
        msg = recovery_service.approval_bytes(
            server_nonce=raw_nonce, account_id=user.id,
            new_credential_id=material["credential_id"],
            new_public_key=material["public_key"])

    approvals = [g.approve(msg) for g in guardians[:k]]
    result = await recovery_service.finish_recovery(
        session, user=user, server_nonce=nonce_state,
        new_credential_attestation=attestation, approvals=approvals)
    await session.commit()
    return result, auth


async def test_happy_path_finish_then_finalize(session):
    """Baseline: enroll 2-of-3, finish opens a pending row, finalize after the window
    re-keys and issues a session + binds exactly the staged credential."""
    user = await _user(session)
    gs = [Guardian(), Guardian(), Guardian()]
    await _enroll(session, user, gs, k=2)
    result, auth = await _drive_finish(session, user, gs, k=2)
    assert result["status"] == "pending"
    assert await _pending_count(session, user.id) == 1

    # Force the deadline into the past so finalize's guarded DELETE matches.
    await _expire_pending(session, user.id)
    outcome = await recovery_service.finalize_recovery(
        session, recovery_id=result["recovery_id"],
        finalize_token=result["finalize_token"])
    assert outcome is not None
    assert set(outcome) == {"access_token", "refresh_token", "user"}
    # Exactly one credential now — the staged one (old ones revoked, this bound).
    assert await _cred_count(session, user.id) == 1
    cred = await passkey_service.get_credential(
        session, bytes_to_base64url(auth.credential_id))
    assert cred is not None and cred.user_id == user.id
    assert await _pending_count(session, user.id) == 0


async def _expire_pending(session, user_id):
    """Push a pending row's veto_deadline into the past (simulate the window elapsing)
    without touching wall-clock — the deadline is a stored column."""
    from sqlalchemy import update
    await session.execute(
        update(PendingRecovery).where(PendingRecovery.user_id == user_id)
        .values(veto_deadline=dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)))
    await session.commit()


# ---- (a) cancel-lands-mid-finalize → owner wins ---------------------------- #

async def test_a_cancel_before_finalize_owner_wins(session):
    """The guarded-DELETE race: a cancel that lands (deletes the pending row) BEFORE
    finalize makes finalize match 0 rows → None → no session, no re-key. The owner's
    veto provably wins; there is no interleaving where both win."""
    user = await _user(session)
    gs = [Guardian(), Guardian()]
    await _enroll(session, user, gs, k=2)
    result, auth = await _drive_finish(session, user, gs, k=2)
    await _expire_pending(session, user.id)  # window passed — finalize is eligible

    # Owner cancels first (from a live device).
    assert await recovery_service.cancel_recovery(session, user.id) is True
    await session.commit()

    # Now finalize: the row is gone → guarded DELETE matches nothing → None.
    outcome = await recovery_service.finalize_recovery(
        session, recovery_id=result["recovery_id"],
        finalize_token=result["finalize_token"])
    assert outcome is None, "finalize must NOT win after a cancel deleted the row"
    # No credential was bound (the attacker got nothing).
    assert await _cred_count(session, user.id) == 0


async def test_a_guard_is_real_delete_the_guard_red_prove(session):
    """RED-prove the guard: if the deadline predicate were NOT in the WHERE, an
    unexpired pending row would finalize immediately. Prove the guard is load-bearing
    by showing finalize on a still-LIVE (unexpired) row matches nothing."""
    user = await _user(session)
    gs = [Guardian(), Guardian()]
    await _enroll(session, user, gs, k=2)
    result, _auth = await _drive_finish(session, user, gs, k=2)
    # Deadline is 72h out (default) — NOT yet passed. finalize must fail closed.
    outcome = await recovery_service.finalize_recovery(
        session, recovery_id=result["recovery_id"],
        finalize_token=result["finalize_token"])
    assert outcome is None, "finalize before the deadline must match 0 rows"
    assert await _pending_count(session, user.id) == 1  # row untouched


async def test_a_finalize_credential_collision_fails_closed(session):
    """RED-prove the finalize credential-insert guard (cage-match Carnot+Tesla, PR#69):
    if the staged credential_id already belongs to ANOTHER account (a global UNIQUE
    collision), finalize must FAIL CLOSED — roll back (restoring the pending row so it
    stays retryable) and return None, never raise a raw IntegrityError/500 on the
    takeover path. Remove the begin_nested try/except in finalize_recovery and this
    goes red with an unhandled IntegrityError."""
    user = await _user(session, username="alice")
    uid = user.id  # capture BEFORE finalize — its rollback expires the shared-session
                   # ORM objects (a test-harness artifact of one session across the
                   # whole test; in prod the route just 409s on None, no ORM access).
    gs = [Guardian(), Guardian()]
    await _enroll(session, user, gs, k=2)
    result, auth = await _drive_finish(session, user, gs, k=2)
    staged_cred_id = bytes_to_base64url(auth.credential_id)

    # A DIFFERENT account already holds this credential_id (it is globally UNIQUE).
    bob = await _user(session, username="bob")
    bob_id = bob.id
    session.add(PasskeyCredential(
        credential_id=staged_cred_id, user_id=bob_id,
        public_key="AAAA", sign_count=0))
    await session.commit()

    await _expire_pending(session, uid)  # window passed — finalize is eligible
    outcome = await recovery_service.finalize_recovery(
        session, recovery_id=result["recovery_id"],
        finalize_token=result["finalize_token"])
    assert outcome is None, "a credential collision must fail closed, not 500"
    # Fail-closed AND retryable: the rollback undid the guarded DELETE, so alice's
    # pending row is restored; bob's credential is untouched.
    assert await _pending_count(session, uid) == 1
    bobs = await passkey_service.get_credential(session, staged_cred_id)
    assert bobs is not None and bobs.user_id == bob_id


# ---- (b) expired attacker row does NOT lock out a fresh legit recovery ------ #

async def test_b_expired_pending_is_reclaimable(session):
    """A griefer opens a pending recovery then lets it expire. The UNIQUE(user_id)
    slot must NOT stay wedged — a fresh legit recovery reclaims the expired row."""
    user = await _user(session)
    gs = [Guardian(), Guardian()]
    await _enroll(session, user, gs, k=2)
    # First (griefer) recovery opens a pending row, which then expires.
    first, _ = await _drive_finish(session, user, gs, k=2)
    await _expire_pending(session, user.id)
    assert await _pending_count(session, user.id) == 1  # expired but present

    # A fresh legit recovery must succeed (reclaim-on-expiry), not hit UNIQUE lockout.
    second, _ = await _drive_finish(session, user, gs, k=2)
    assert second["status"] == "pending"
    assert second["recovery_id"] != first["recovery_id"]
    assert await _pending_count(session, user.id) == 1  # exactly one, the new one


# ---- (c) repeated finish never advances a live veto_deadline --------------- #

async def test_c_repeated_finish_does_not_advance_live_deadline(session):
    """A second finish while a row is LIVE is rejected (RecoveryAlreadyPending) — the
    write-once deadline is never pushed out by re-running finish."""
    user = await _user(session)
    gs = [Guardian(), Guardian()]
    await _enroll(session, user, gs, k=2)
    uid = user.id  # capture: the service's rollback on the reject path expires `user`
    first, _ = await _drive_finish(session, user, gs, k=2)
    deadline_before = (await session.execute(
        select(PendingRecovery.veto_deadline)
        .where(PendingRecovery.user_id == uid))).scalar_one()

    # A second finish while the first is live must be rejected.
    with pytest.raises(recovery_service.RecoveryAlreadyPending):
        await _drive_finish(session, user, gs, k=2)

    deadline_after = (await session.execute(
        select(PendingRecovery.veto_deadline)
        .where(PendingRecovery.user_id == uid))).scalar_one()
    assert deadline_after == deadline_before, "live deadline must not advance"
    assert await _pending_count(session, uid) == 1


# ---- (d) double finalize-poll → one session, one bind, no IntegrityError ---- #

async def test_d_double_finalize_poll_is_idempotent(session):
    """A double finalize-poll (retry / lost response): the first wins (session +
    bind), the second finds 0 rows → None. One session, one credential, no
    IntegrityError from a second bind."""
    user = await _user(session)
    gs = [Guardian(), Guardian()]
    await _enroll(session, user, gs, k=2)
    result, auth = await _drive_finish(session, user, gs, k=2)
    await _expire_pending(session, user.id)

    first = await recovery_service.finalize_recovery(
        session, recovery_id=result["recovery_id"],
        finalize_token=result["finalize_token"])
    second = await recovery_service.finalize_recovery(
        session, recovery_id=result["recovery_id"],
        finalize_token=result["finalize_token"])
    assert first is not None
    assert second is None, "second poll must be a clean no-op, not a re-bind"
    assert await _cred_count(session, user.id) == 1, "exactly one credential bound"


# ---- (e) captured approval can't authorize a DIFFERENT credential ---------- #

async def test_e_binding_captured_approval_cannot_swap_credential(session):
    """The binding (fixes C1): approvals signed over credential A must NOT authorize
    installing credential B. Submit attestation A with approvals bound to B → the
    recomputed approval_bytes (over A) don't match the signatures (over B) → quorum
    rejected."""
    user = await _user(session)
    gs = [Guardian(), Guardian()]
    await _enroll(session, user, gs, k=2)
    with pytest.raises(recovery_service.QuorumRejected):
        await _drive_finish(session, user, gs, k=2, wrong_credential=True)
    assert await _pending_count(session, user.id) == 0, "no pending row on rejection"


# ---- (f) < k or non-distinct approvers → rejected -------------------------- #

async def test_f_below_threshold_rejected(session):
    """Fewer than k valid approvals → QuorumRejected."""
    user = await _user(session)
    gs = [Guardian(), Guardian(), Guardian()]
    await _enroll(session, user, gs, k=3)
    with pytest.raises(recovery_service.QuorumRejected):
        await _drive_finish(session, user, gs, k=2)  # only 2 signatures for k=3


async def test_f_non_distinct_approvers_rejected(session):
    """One guardian signing k times does NOT satisfy a k-threshold — distinct
    approvers only. k=2 with the same guardian's signature twice → rejected."""
    user = await _user(session)
    g = Guardian()
    other = Guardian()
    await _enroll(session, user, [g, other], k=2)

    # Build a finish where BOTH approvals come from the same guardian g.
    auth = SoftAuthenticator()
    start = await recovery_service.start_recovery(session, user)
    nonce_state = start["server_nonce"]
    attestation = _new_attestation(auth, nonce_state)
    raw_nonce = base64url_to_bytes(nonce_state)
    material = passkey_service.verify_registration(
        raw_challenge=raw_nonce, credential=attestation)
    msg = recovery_service.approval_bytes(
        server_nonce=raw_nonce, account_id=user.id,
        new_credential_id=material["credential_id"],
        new_public_key=material["public_key"])
    # Same guardian's approval presented twice (distinct dict objects, same key).
    approvals = [g.approve(msg), g.approve(msg)]
    with pytest.raises(recovery_service.QuorumRejected):
        await recovery_service.finish_recovery(
            session, user=user, server_nonce=nonce_state,
            new_credential_attestation=attestation, approvals=approvals)


async def test_f_non_registered_approver_rejected_constant_shape(session):
    """A signature from a valid ed25519 key that is NOT a registered guardian counts
    for nothing — and raises the SAME QuorumRejected as a bad signature (constant
    shape: no commitment-existence oracle)."""
    user = await _user(session)
    real = [Guardian(), Guardian()]
    await _enroll(session, user, real, k=2)
    stranger = Guardian()  # a well-formed key, but never registered
    # One real + one stranger for k=2 → only one distinct REGISTERED approver → reject.
    with pytest.raises(recovery_service.QuorumRejected):
        await _drive_finish(session, user, [real[0], stranger], k=2)


# ---- (g) recovery of a banned/deleted account → fail closed ---------------- #

async def test_g_finalize_on_deleted_account_fails_closed(session):
    """Finalize must re-assert the account is live in the same txn. If the account is
    deleted between finish and finalize, finalize fails closed (None) — recovery
    cannot resurrect a gone account."""
    user = await _user(session)
    gs = [Guardian(), Guardian()]
    await _enroll(session, user, gs, k=2)
    result, _auth = await _drive_finish(session, user, gs, k=2)
    await _expire_pending(session, user.id)

    # Delete the account (tears down recovery rows too — so the pending row is gone).
    await accounts_service.delete_user_account(session, user.id)
    assert await users_service.get_by_id(session, user.id) is None

    # Finalize now finds no pending row (purged by deletion) → None, fail closed.
    outcome = await recovery_service.finalize_recovery(
        session, recovery_id=result["recovery_id"],
        finalize_token=result["finalize_token"])
    assert outcome is None


async def test_g_finalize_when_user_vanishes_but_pending_survives(session):
    """A narrower fail-closed: the pending row survives but the user row is gone (a
    hypothetical partial state). finalize's re-assert-live guard must still fail
    closed, never mint a session for a non-existent account."""
    user = await _user(session)
    gs = [Guardian(), Guardian()]
    await _enroll(session, user, gs, k=2)
    result, _auth = await _drive_finish(session, user, gs, k=2)
    await _expire_pending(session, user.id)

    # Delete ONLY the user row, leaving the pending row (simulate an inconsistent
    # state the re-assert guard must catch).
    from sqlalchemy import delete
    await session.execute(delete(User).where(User.id == user.id))
    await session.commit()

    outcome = await recovery_service.finalize_recovery(
        session, recovery_id=result["recovery_id"],
        finalize_token=result["finalize_token"])
    assert outcome is None, "finalize must not mint a session for a vanished user"


# ---- (h) a leaked policy row alone recovers nothing ------------------------ #

async def test_h_leaked_policy_alone_recovers_nothing(session):
    """The self-custody property: an attacker who reads the ENTIRE recovery DB (policy
    + approver PUBLIC keys) still cannot recover — they have no approver PRIVATE keys,
    so they cannot produce the signatures a quorum needs. Simulate the leak by trying
    to recover with NO guardian private keys (empty/garbage approvals)."""
    user = await _user(session)
    gs = [Guardian(), Guardian()]
    await _enroll(session, user, gs, k=2)

    # The attacker knows the policy + the approver pubkeys (they're in the DB), but has
    # no private keys. Best they can do: submit garbage signatures over the bound bytes.
    auth = SoftAuthenticator()
    start = await recovery_service.start_recovery(session, user)
    attestation = _new_attestation(auth, start["server_nonce"])
    # Garbage 64-byte "signatures" attributed to the real approver pubkeys.
    approvals = [
        {"approver_pubkey": g.pubkey, "sig": _b64url(b"\x00" * 64)} for g in gs]
    with pytest.raises(recovery_service.QuorumRejected):
        await recovery_service.finish_recovery(
            session, user=user, server_nonce=start["server_nonce"],
            new_credential_attestation=attestation, approvals=approvals)
    assert await _pending_count(session, user.id) == 0


# ============================================================ enroll / policy

async def test_enroll_validates_k_range(session):
    user = await _user(session)
    gs = [Guardian(), Guardian()]
    with pytest.raises(recovery_service.InvalidEnrollment):
        await recovery_service.enroll_policy(
            session, user_id=user.id,
            approver_pubkeys=[g.pubkey for g in gs], k=3)  # k > n


async def test_enroll_rejects_duplicate_approvers(session):
    user = await _user(session)
    g = Guardian()
    with pytest.raises(recovery_service.InvalidEnrollment):
        await recovery_service.enroll_policy(
            session, user_id=user.id,
            approver_pubkeys=[g.pubkey, g.pubkey], k=1)  # not distinct


async def test_enroll_refuses_silent_rotation(session):
    """A second enroll while a policy exists is REFUSED (no silent guardian swap) —
    the §4 session-theft defense. Rotation-through-veto is a named follow-up."""
    user = await _user(session)
    await _enroll(session, user, [Guardian()], k=1)
    with pytest.raises(recovery_service.RecoveryError):
        await recovery_service.enroll_policy(
            session, user_id=user.id, approver_pubkeys=[Guardian().pubkey], k=1)


async def test_start_without_policy_fails(session):
    user = await _user(session)
    with pytest.raises(recovery_service.NoRecoveryPolicy):
        await recovery_service.start_recovery(session, user)


# ============================================================ REST surface

async def test_endpoints_enroll_status_cancel(client, session):
    """The authed endpoints round-trip: enroll → policy → status shows no pending →
    (drive a finish) → status shows pending → cancel clears it."""
    user = await _user(session)
    gs = [Guardian(), Guardian()]
    # Enroll via the endpoint.
    resp = await client.post(
        "/v1/auth/recovery/enroll",
        json={"approver_pubkeys": [g.pubkey for g in gs], "k": 2},
        headers=_headers(user))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"threshold_k": 2, "approver_count": 2}

    # Policy reflects it.
    pol = (await client.get(
        "/v1/auth/recovery/policy", headers=_headers(user))).json()
    assert pol["enrolled"] is True and pol["threshold_k"] == 2

    # No pending yet.
    st = (await client.get(
        "/v1/auth/recovery/status", headers=_headers(user))).json()
    assert st == {"pending": False}

    # Open a pending recovery (service-level to reuse the crypto helpers).
    result, _ = await _drive_finish(session, user, gs, k=2)
    st = (await client.get(
        "/v1/auth/recovery/status", headers=_headers(user))).json()
    assert st["pending"] is True and st["recovery_id"] == result["recovery_id"]

    # Cancel via the endpoint.
    c = await client.post(
        "/v1/auth/passkey/recover/cancel", headers=_headers(user))
    assert c.status_code == 200 and c.json() == {"cancelled": True}
    st = (await client.get(
        "/v1/auth/recovery/status", headers=_headers(user))).json()
    assert st == {"pending": False}


async def test_endpoint_recover_start_unknown_handle_404(client, session):
    resp = await client.post(
        "/v1/auth/passkey/recover/start", json={"account_handle": "ghost"})
    assert resp.status_code == 404


async def test_endpoint_enroll_requires_auth(client, session):
    resp = await client.post(
        "/v1/auth/recovery/enroll",
        json={"approver_pubkeys": [Guardian().pubkey], "k": 1})
    assert resp.status_code in (401, 403)  # HTTPBearer auto_error


async def test_endpoint_finalize_before_deadline_409(client, session):
    """The finalize endpoint returns 409 (not finalizable) before the window passes."""
    user = await _user(session)
    gs = [Guardian(), Guardian()]
    await _enroll(session, user, gs, k=2)
    result, _ = await _drive_finish(session, user, gs, k=2)
    resp = await client.post(
        "/v1/auth/passkey/recover/finalize",
        json={"recovery_id": result["recovery_id"],
              "finalize_token": result["finalize_token"]})
    assert resp.status_code == 409
