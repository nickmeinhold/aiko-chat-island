"""Passkey endpoints (#1471) — the FULL ceremony proven end to end.

soft-webauthn (the off-the-shelf software authenticator) is dep-incompatible with
our cryptography pin (it needs <45 via fido2; py_webauthn needs >=46), so we drive
a minimal inline P-256 authenticator that produces REAL attestation/assertion
responses through the genuine py_webauthn verification path — no mocking of the
crypto boundary. This is what proves register -> authenticate works (register now
creates the account directly — Design 04 Step 1, no /social/claim), the sign_count
contract holds, and the outcome shapes match the social/broker door.
"""
from __future__ import annotations

import hashlib
import json
import struct

import cbor2
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from sqlalchemy import func, select

from aiko_gateway.config import settings
from aiko_gateway.domain import passkey_service
from aiko_gateway.domain.models import User
from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest import well_known as well_known_routes
from aiko_gateway.rest.deps import get_session

RP_ID = "chat.imagineering.cc"
ORIGIN = "https://chat.imagineering.cc"


@pytest_asyncio.fixture
async def client(session):
    """Auth + well-known routers over the throwaway session. NO flags set on
    purpose: passkey endpoints are ungated (deploy-dark), so they must work with
    passkey_enabled at its default False — this fixture proves that."""

    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(well_known_routes.router)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class SoftAuthenticator:
    """A throwaway WebAuthn platform-authenticator: an EC P-256 keypair that builds
    a fmt:'none' attestation and signs assertions exactly as a real device would."""

    def __init__(self, credential_id: bytes = b"test-credential-0001",
                 user_verified: bool = True):
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = credential_id
        self.sign_count = 0  # platform passkeys report 0 and never increment
        self.user_verified = user_verified  # whether the UV flag is set in authData

    def _flags(self, *, include_cred: bool) -> int:
        flags = 0x01  # UP (user present) always
        if self.user_verified:
            flags |= 0x04  # UV (user verified)
        if include_cred:
            flags |= 0x40  # AT (attested credential data)
        return flags

    def _cose_public_key(self) -> bytes:
        nums = self._key.public_key().public_numbers()
        x = nums.x.to_bytes(32, "big")
        y = nums.y.to_bytes(32, "big")
        # COSE_Key for EC2 / ES256 / P-256: {1:2, 3:-7, -1:1, -2:x, -3:y}
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: x, -3: y})

    def _auth_data(self, *, flags: int, include_cred: bool) -> bytes:
        rp_id_hash = hashlib.sha256(RP_ID.encode()).digest()
        data = rp_id_hash + bytes([flags]) + struct.pack(">I", self.sign_count)
        if include_cred:
            aaguid = b"\x00" * 16
            cred_len = struct.pack(">H", len(self.credential_id))
            data += aaguid + cred_len + self.credential_id + self._cose_public_key()
        return data

    def _client_data(self, *, ceremony: str, challenge_b64: str) -> bytes:
        return json.dumps({
            "type": ceremony, "challenge": challenge_b64,
            "origin": ORIGIN, "crossOrigin": False,
        }).encode()

    def register(self, challenge_b64: str) -> dict:
        auth_data = self._auth_data(
            flags=self._flags(include_cred=True), include_cred=True)
        client_data = self._client_data(
            ceremony="webauthn.create", challenge_b64=challenge_b64)
        att_obj = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(att_obj),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
        }

    def authenticate(self, challenge_b64: str) -> dict:
        auth_data = self._auth_data(
            flags=self._flags(include_cred=False), include_cred=False)
        client_data = self._client_data(
            ceremony="webauthn.get", challenge_b64=challenge_b64)
        signed = auth_data + hashlib.sha256(client_data).digest()
        signature = self._key.sign(signed, ec.ECDSA(hashes.SHA256()))  # DER, as WebAuthn wants
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(b"user-handle"),
            },
            "clientExtensionResults": {},
        }


async def _register_passkey(client, auth=None):
    """Drive register/start -> device -> register/finish. Returns the finish response
    (now an AUTHENTICATED outcome — the account is created directly at register/finish,
    Design 04 Step 1; no /social/claim) and the authenticator."""
    auth = auth or SoftAuthenticator()
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    finish = await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"], "credential": auth.register(start["options"]["challenge"])})
    return finish, auth


# --- Design 04 Step 1: register creates its own account (fixes #1728) ------- #

async def test_register_finish_creates_account_and_returns_session(client, session):
    """Design 04 Step 1 / #1728 fix: passkey register/finish creates its OWN account
    directly (auto-handle, no /social/claim) and returns session tokens. The
    credential persists immediately — persistence is no longer gated on a handle
    claim that could collide with a pre-existing account and orphan the device
    credential forever."""
    auth = SoftAuthenticator()
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    finish = await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"], "credential": auth.register(start["options"]["challenge"])})
    assert finish.status_code == 200, finish.text
    body = finish.json()
    # AUTHENTICATED outcome (byte-identical to the social/broker door), NOT provisioning.
    assert set(body) == {"access_token", "refresh_token", "user"}
    assert set(body["user"]) == {"user_id", "username", "display_name", "aiko_username"}
    # An auto-generated handle — nobody picked one, and it did not collide.
    assert body["user"]["username"].startswith("aiko-")
    # The credential is persisted right now (the #1728 orphan is impossible).
    cred = await passkey_service.get_credential(
        session, bytes_to_base64url(auth.credential_id))
    assert cred is not None and cred.user_id == body["user"]["user_id"]


async def test_register_finish_persists_even_when_a_handle_is_taken(client, session):
    """#1728 root cause, pinned: a pre-existing account owning a name no longer blocks
    passkey account creation — register auto-assigns a unique handle instead of
    claiming a chosen one, so there is no collision to reject and no orphan."""
    from aiko_gateway.domain import users_service
    # A social-style account already exists (mirrors Nick's live case).
    await users_service.create_passkey_user(
        session, handle="nick", display_name="Nick", email=None,
        material={"credential_id": "cHJlZXhpc3Rpbmc", "public_key": "cGs",
                  "sign_count": 0, "transports": None, "aaguid": None})
    auth = SoftAuthenticator(credential_id=b"fresh-passkey-cred")
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    finish = await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"], "credential": auth.register(start["options"]["challenge"])})
    assert finish.status_code == 200, finish.text  # NOT a 409 — no handle claim to collide
    cred = await passkey_service.get_credential(
        session, bytes_to_base64url(auth.credential_id))
    assert cred is not None


async def test_register_finish_then_authenticate_round_trip(client):
    """The whole point of Step 1: the just-registered device signs in immediately,
    with no claim step in between."""
    auth = SoftAuthenticator()
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    reg = (await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"], "credential": auth.register(start["options"]["challenge"])})).json()
    astart = (await client.post("/v1/auth/passkey/authenticate/start")).json()
    afinish = await client.post("/v1/auth/passkey/authenticate/finish", json={
        "state": astart["state"], "credential": auth.authenticate(astart["options"]["challenge"])})
    assert afinish.status_code == 200, afinish.text
    assert afinish.json()["user"]["user_id"] == reg["user"]["user_id"]


async def test_register_finish_duplicate_credential_conflicts(client):
    """Re-registering an already-known credential is a 409 (credential_id UNIQUE),
    not a silent second account — the device should authenticate, not re-register."""
    auth = SoftAuthenticator(credential_id=b"dup-reg-cred-0001")
    s1 = (await client.post("/v1/auth/passkey/register/start")).json()
    r1 = await client.post("/v1/auth/passkey/register/finish", json={
        "state": s1["state"], "credential": auth.register(s1["options"]["challenge"])})
    assert r1.status_code == 200
    s2 = (await client.post("/v1/auth/passkey/register/start")).json()
    r2 = await client.post("/v1/auth/passkey/register/finish", json={
        "state": s2["state"], "credential": auth.register(s2["options"]["challenge"])})
    assert r2.status_code == 409, r2.text


async def test_register_finish_retries_on_handle_collision(client, session, monkeypatch):
    """The auto-handle SAVEPOINT-retry path: when a generated handle collides, register
    mints a fresh one inside a nested savepoint and still succeeds — and because the
    savepoint is opened AFTER the challenge consume, the single-use challenge burn
    survives the inner rollback. Forces exactly ONE collision, then a fresh handle."""
    from aiko_gateway.domain import users_service
    # Pre-seed an account owning the FIRST handle the RNG will yield (aiko-<hex>).
    await users_service.create_passkey_user(
        session, handle="aiko-collide0", display_name="", email=None,
        material={"credential_id": "c2VlZC1jcmVk", "public_key": "cGs",
                  "sign_count": 0, "transports": None, "aaguid": None})
    hexes = iter(["collide0", "fresh001"])  # attempt-1 collides; attempt-2 is free
    monkeypatch.setattr(users_service.secrets, "token_hex", lambda n: next(hexes))

    auth = SoftAuthenticator(credential_id=b"collision-retry-cred")
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    finish = await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"], "credential": auth.register(start["options"]["challenge"])})
    assert finish.status_code == 200, finish.text
    assert finish.json()["user"]["username"] == "aiko-fresh001"  # the retried handle
    # Credential persisted under the fresh account.
    cred = await passkey_service.get_credential(
        session, bytes_to_base64url(auth.credential_id))
    assert cred is not None
    # The challenge is single-use: replaying the SAME state now fails (burn survived
    # the inner savepoint rollback).
    replay = await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"], "credential": auth.register(start["options"]["challenge"])})
    assert replay.status_code == 400


async def test_register_finish_handle_exhaustion_returns_503_no_orphan(
        client, session, monkeypatch):
    """If EVERY auto-handle attempt collides (stuck RNG / saturated table), register
    fails with a deliberate 503 — never a bare 500 — and leaves no orphan user or
    credential (cage-match PR#68, Carnot+Tesla)."""
    from aiko_gateway.domain import users_service
    # Seed the single handle every attempt will generate.
    await users_service.create_passkey_user(
        session, handle="aiko-deadbeefcafe", display_name="", email=None,
        material={"credential_id": "c2VlZC1leGg", "public_key": "cGs",
                  "sign_count": 0, "transports": None, "aaguid": None})
    users_before = await _count_users(session)
    # Every attempt yields the SAME colliding hex → all 5 savepoints fail on username.
    monkeypatch.setattr(users_service.secrets, "token_hex", lambda n: "deadbeefcafe")
    auth = SoftAuthenticator(credential_id=b"exhaustion-cred")
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    finish = await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"], "credential": auth.register(start["options"]["challenge"])})
    assert finish.status_code == 503, finish.text
    # No orphan — the credential was never persisted, no extra user rows.
    assert await _count_users(session) == users_before
    assert await passkey_service.get_credential(
        session, bytes_to_base64url(auth.credential_id)) is None


async def test_social_claim_rejects_passkey_provider_token(client):
    """A pre-cutover passkey provisioning token (provider='passkey') must NOT be
    processed as a social identity at /social/claim — rejected 401 (cage-match PR#68,
    Tesla), not minted into a bogus SocialIdentity(provider='passkey')."""
    from aiko_gateway.domain import security
    tok = security.issue_provisioning("passkey", "some-credential-id")
    r = await client.post("/v1/auth/social/claim", json={
        "provisioning_token": tok, "handle": "x", "display_name": ""})
    assert r.status_code == 401, r.text


# --- fail-closed paths ----------------------------------------------------- #

async def test_register_finish_bad_state_rejected(client):
    auth = SoftAuthenticator()
    finish = await client.post("/v1/auth/passkey/register/finish", json={
        "state": "never-issued", "credential": auth.register("AAAA")})
    assert finish.status_code == 400


async def test_register_challenge_is_single_use(client):
    auth = SoftAuthenticator()
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    cred = auth.register(start["options"]["challenge"])
    r1 = await client.post("/v1/auth/passkey/register/finish",
                           json={"state": start["state"], "credential": cred})
    assert r1.status_code == 200
    # Replaying the SAME state (a captured finish) fails closed — challenge burned.
    r2 = await client.post("/v1/auth/passkey/register/finish",
                           json={"state": start["state"], "credential": cred})
    assert r2.status_code == 400


async def test_authenticate_unknown_credential_rejected(client):
    """An assertion from a never-registered authenticator → 401 (no stored key)."""
    auth = SoftAuthenticator(credential_id=b"ghost-credential")
    start = (await client.post("/v1/auth/passkey/authenticate/start")).json()
    finish = await client.post("/v1/auth/passkey/authenticate/finish", json={
        "state": start["state"], "credential": auth.authenticate(start["options"]["challenge"])})
    assert finish.status_code == 401


async def test_authenticate_wrong_key_rejected(client):
    """A DIFFERENT keypair claiming a registered credential_id → signature fails."""
    reg, _ = await _register_passkey(client)
    assert reg.status_code == 200
    impostor = SoftAuthenticator(credential_id=b"test-credential-0001")  # same id, new key
    start = (await client.post("/v1/auth/passkey/authenticate/start")).json()
    finish = await client.post("/v1/auth/passkey/authenticate/finish", json={
        "state": start["state"], "credential": impostor.authenticate(start["options"]["challenge"])})
    assert finish.status_code == 401


# --- cage-match #38 hardening (Carnot findings) ---------------------------- #

async def test_register_rejects_unverified_user(client):
    """UV is REQUIRED by default — a passwordless PRIMARY factor (Carnot HIGH). An
    authenticator that proves only user PRESENCE (no UV bit) is refused at register."""
    auth = SoftAuthenticator(user_verified=False)
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    finish = await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"], "credential": auth.register(start["options"]["challenge"])})
    assert finish.status_code == 401


# --- advertisement + domain association ------------------------------------ #

async def test_providers_advertises_passkey_only_when_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "passkey_enabled", False)
    monkeypatch.setattr(settings, "social_signin_enabled", False)
    slugs = [p["slug"] for p in (await client.get("/v1/auth/providers")).json()["providers"]]
    assert "passkey" not in slugs
    monkeypatch.setattr(settings, "passkey_enabled", True)
    providers = (await client.get("/v1/auth/providers")).json()["providers"]
    entry = next(p for p in providers if p["slug"] == "passkey")
    assert entry == {"slug": "passkey", "display_name": "Passkey", "kind": "passkey"}


async def test_well_known_apple_app_site_association(client):
    r = await client.get("/.well-known/apple-app-site-association")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"webcredentials": {"apps": [settings.passkey_ios_app_id]}}


async def test_well_known_assetlinks_empty_until_fingerprints_configured(
        client, monkeypatch):
    # Default (no Play-signing SHA yet, app #20): serve [] — NOT a fingerprint-less
    # target that can never verify (cage-match #38, Carnot).
    monkeypatch.setattr(settings, "passkey_android_cert_sha256", [])
    r = await client.get("/.well-known/assetlinks.json")
    assert r.status_code == 200 and r.json() == []
    # Once configured, the target appears with the fingerprint.
    monkeypatch.setattr(settings, "passkey_android_cert_sha256", ["AB:CD:EF"])
    entry = (await client.get("/.well-known/assetlinks.json")).json()[0]
    assert entry["relation"] == ["delegate_permission/common.get_login_creds"]
    assert entry["target"]["package_name"] == settings.passkey_android_package
    assert entry["target"]["sha256_cert_fingerprints"] == ["AB:CD:EF"]


# --- add-passkey-to-existing-account (#1727) ------------------------------- #
# The bug: there was only ONE passkey path (first-passkey-creates-account via
# register→claim). An existing user (e.g. social) adding a passkey was forced
# through create+claim; a handle conflict with their OWN account rejected the
# claim and orphaned the device credential — passkey_credentials stayed empty and
# every authenticate 401'd. The fix is an AUTHENTICATED add path that links the
# verified credential straight to the existing user_id (no claim, no new account).

async def _count_users(session) -> int:
    return (await session.execute(select(func.count()).select_from(User))).scalar_one()


async def test_add_passkey_links_to_existing_user_no_new_account(client, session):
    """An authenticated user adds a passkey; it links to their EXISTING account
    (no new account) and can then sign in AS them. This is the #1727 repro/fix."""
    reg, _first = await _register_passkey(client)
    assert reg.status_code == 200, reg.text
    token = reg.json()["access_token"]
    uid = reg.json()["user"]["user_id"]
    users_before = await _count_users(session)

    added = SoftAuthenticator(credential_id=b"added-credential-0002")
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    r = await client.post(
        "/v1/auth/passkey/add/finish",
        headers={"Authorization": f"Bearer {token}"},
        json={"state": start["state"],
              "credential": added.register(start["options"]["challenge"])})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == uid

    # No new account — the credential attached to the existing user.
    assert await _count_users(session) == users_before
    cred = await passkey_service.get_credential(
        session, bytes_to_base64url(added.credential_id))
    assert cred is not None and cred.user_id == uid

    # The freshly-added passkey now authenticates AS that same user.
    astart = (await client.post("/v1/auth/passkey/authenticate/start")).json()
    afinish = await client.post(
        "/v1/auth/passkey/authenticate/finish",
        json={"state": astart["state"],
              "credential": added.authenticate(astart["options"]["challenge"])})
    assert afinish.status_code == 200, afinish.text
    assert afinish.json()["user"]["user_id"] == uid


async def test_add_passkey_requires_authentication(client):
    """Unauthenticated add/finish is refused — the bearer names which account to
    link to, so there is no anonymous add."""
    added = SoftAuthenticator(credential_id=b"unauth-credential-0003")
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    r = await client.post(
        "/v1/auth/passkey/add/finish",
        json={"state": start["state"],
              "credential": added.register(start["options"]["challenge"])})
    assert r.status_code in (401, 403), r.text


async def test_add_passkey_duplicate_credential_conflicts(client, session):
    """Re-adding an already-registered credential is a 409 (credential_id UNIQUE is
    the replay guard), not a silent second row."""
    reg, first = await _register_passkey(client)
    assert reg.status_code == 200, reg.text
    token = reg.json()["access_token"]
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    r = await client.post(
        "/v1/auth/passkey/add/finish",
        headers={"Authorization": f"Bearer {token}"},
        json={"state": start["state"],
              "credential": first.register(start["options"]["challenge"])})
    assert r.status_code == 409, r.text
