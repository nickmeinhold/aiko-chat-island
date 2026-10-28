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
import re
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
                 user_verified: bool = True, *, extra_flags: int = 0,
                 cose_key: object = None):
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = credential_id
        self.sign_count = 0  # platform passkeys report 0 and never increment
        self.user_verified = user_verified  # whether the UV flag is set in authData
        self.extra_flags = extra_flags  # hostile bits OR'd into authData flags
        self.cose_key = cose_key  # a hostile COSE key (a dict, or raw bytes) if set

    def _flags(self, *, include_cred: bool) -> int:
        flags = 0x01 | self.extra_flags  # UP (user present) always
        if self.user_verified:
            flags |= 0x04  # UV (user verified)
        if include_cred:
            flags |= 0x40  # AT (attested credential data)
        return flags

    def _cose_public_key(self) -> bytes:
        if isinstance(self.cose_key, bytes):
            return self.cose_key
        if self.cose_key is not None:
            return cbor2.dumps(self.cose_key)
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

    def register(self, challenge_b64: str, *, fmt: str = "none",
                 att_stmt: dict | None = None) -> dict:
        auth_data = self._auth_data(
            flags=self._flags(include_cred=True), include_cred=True)
        client_data = self._client_data(
            ceremony="webauthn.create", challenge_b64=challenge_b64)
        att_obj = cbor2.dumps(
            {"fmt": fmt, "attStmt": att_stmt or {}, "authData": auth_data})
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
    assert set(body["user"]) == {"user_id", "username", "display_name", "aiko_username",
                                 "handle_auto"}
    # An auto-generated handle — nobody picked one, and it did not collide — and the
    # outcome says so, which is what sends the app to "choose a handle".
    assert body["user"]["username"].startswith("aiko-")
    assert body["user"]["handle_auto"] is True
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
    # Patch the GENERATOR, not `secrets.token_hex`: the stdlib module is shared, and
    # register/start draws its random passkey label from it first, so an earlier
    # version of this test handed "collide0" to the LABEL and never collided at all
    # (Noether, cage-match #435). The assertion below now fails if the retry is removed.
    handles = iter(["aiko-collide0", "aiko-fresh001"])  # attempt 1 collides; 2 is free
    monkeypatch.setattr(users_service, "_new_auto_handle", lambda: next(handles))

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
    for open_ in (True, False):
        monkeypatch.setattr(settings, "open_registration", open_)
        providers = (await client.get("/v1/auth/providers")).json()["providers"]
        entry = next(p for p in providers if p["slug"] == "passkey")
        assert entry == {"slug": "passkey", "display_name": "Passkey", "kind": "passkey",
                         "creates_accounts": open_}


@pytest.mark.parametrize("environment", ["test", "production"])
@pytest.mark.parametrize("open_", [True, False])
async def test_creates_accounts_matches_what_register_finish_does(
    client, monkeypatch, open_, environment,
):
    """The advertisement and the enforcement come from ONE predicate
    (`registration_open`), so they cannot disagree: when /providers says the
    island creates accounts, register/finish creates one; when it says no,
    register/finish is the 403. This is what lets the app hide "Create account"
    instead of leaving an orphaned passkey on the phone. Run in production too,
    because production is where the two doors now answer differently and where
    the app actually reads this."""
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "passkey_enabled", True)
    monkeypatch.setattr(settings, "open_registration", open_)
    providers = (await client.get("/v1/auth/providers")).json()["providers"]
    says = next(p for p in providers if p["slug"] == "passkey")["creates_accounts"]
    finish, _ = await _register_passkey(client)
    assert says is open_
    assert finish.status_code == (200 if open_ else 403), finish.text


async def test_in_production_the_flag_opens_passkey_and_not_password(
        session, monkeypatch):
    """ONE SETTING, TWO DOORS, OPPOSITE ANSWERS (Nick, 2026-10-28). Production with
    OPEN_REGISTRATION=true lets a passkey create an account, and does not serve
    password /register at all: it takes any username and any password. The
    surface is built by `include_auth_routers`, the function main.py mounts auth
    through, so this is what production serves. Both requests run under identical
    settings, so the password 404 cannot be "production refuses everything": the
    passkey 200 beside it is the control."""
    async def _override_session():
        yield session

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "passkey_enabled", True)
    monkeypatch.setattr(settings, "open_registration", True)
    app = FastAPI()
    auth_routes.include_auth_routers(app)
    app.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        finish, _ = await _register_passkey(c)
        assert finish.status_code == 200, finish.text
        password = await c.post("/v1/auth/register", json={
            "username": "admin", "display_name": "", "password": ""})
    assert password.status_code == 404, password.text


def test_every_account_door_must_name_itself():
    """`door` has no default, and an unknown door raises instead of borrowing an
    answer. A new account-creating route that forgot to say which door it is
    would otherwise inherit an answer by omission or by typo."""
    import inspect

    from aiko_gateway.rest.auth import _require_may_provision_account

    door = inspect.signature(_require_may_provision_account).parameters["door"]
    assert door.default is inspect.Parameter.empty
    with pytest.raises(ValueError):
        _require_may_provision_account(door="pasword")


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
    # Once configured, the target appears with the fingerprint. BOTH relations are
    # required: get_login_creds alone passes Google's assetlinks:check but GMS's
    # on-device ValidateRpIdOperation additionally needs handle_all_urls, whose
    # absence was the Android passkey-create [50152] block (2026-07-28).
    monkeypatch.setattr(settings, "passkey_android_cert_sha256", ["AB:CD:EF"])
    entry = (await client.get("/.well-known/assetlinks.json")).json()[0]
    assert entry["relation"] == [
        "delegate_permission/common.get_login_creds",
        "delegate_permission/common.handle_all_urls",
    ]
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


# --- PR-B: a hostile finish body is a 4xx, never a 500 ----------------------- #
# py_webauthn 3.0.0 raises InvalidJSONStructure (a sibling of the two types the
# routes caught) for a body that isn't a credential at all, and leaves the COSE
# key decode, attestation verifiers and backup-flag parse unwrapped. Each case
# below escaped as an uncaught 500 before the service sealed its surface.

_P256_X, _P256_Y = b"\x11" * 32, b"\x22" * 32


def _on_curve_key(alg: int) -> dict:
    """A COSE EC2 key with a REAL P-256 point, carrying whatever alg is given."""
    nums = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    return {1: 2, 3: alg, -1: 1, -2: nums.x.to_bytes(32, "big"),
            -3: nums.y.to_bytes(32, "big")}


def _strip(cred: dict, *keys: str) -> dict:
    return {k: v for k, v in cred.items() if k not in keys}


@pytest.mark.parametrize("mangle", [
    pytest.param(lambda c: _strip(c, "rawId"), id="no-rawId"),
    pytest.param(lambda c: _strip(c, "response"), id="no-response"),
    pytest.param(lambda c: {**c, "response": "x"}, id="response-not-object"),
    pytest.param(lambda c: {**c, "type": "nope"}, id="wrong-type"),
])
async def test_register_finish_malformed_credential_is_400(client, mangle):
    auth = SoftAuthenticator()
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    finish = await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"],
        "credential": mangle(auth.register(start["options"]["challenge"]))})
    assert finish.status_code == 400, finish.text
    assert finish.json()["detail"] == "malformed passkey credential"


@pytest.mark.parametrize("auth, fmt, att_stmt", [
    pytest.param(SoftAuthenticator(cose_key={1: 2, -1: 1, -2: _P256_X, -3: _P256_Y}),
                 "none", None, id="cose-no-alg"),  # KeyError
    pytest.param(SoftAuthenticator(cose_key={1: 9, 3: -7}),
                 "none", None, id="cose-unknown-kty"),  # UnsupportedPublicKeyType
    pytest.param(SoftAuthenticator(cose_key=b"\x01"),
                 "none", None, id="cose-not-a-map"),  # TypeError
    pytest.param(SoftAuthenticator(extra_flags=0x10),
                 "none", None, id="backed-up-not-eligible"),  # InvalidBackupFlags
    pytest.param(SoftAuthenticator(), "packed",
                 {"alg": -7, "sig": b"x", "x5c": [b"not-a-cert"]},
                 id="packed-garbage-x5c"),  # ValueError (asn1)
    pytest.param(SoftAuthenticator(), "tpm",
                 {"ver": "2.0", "alg": -7, "sig": b"x", "x5c": [b"x"],
                  "certInfo": b"x", "pubArea": b"x"}, id="tpm-garbage"),  # KeyError
    pytest.param(SoftAuthenticator(), "android-safetynet",
                 {"ver": "1", "response": b"x.y.z"},
                 id="safetynet-garbage"),  # binascii.Error
    pytest.param(SoftAuthenticator(), "fido-u2f",
                 {"sig": b"x", "x5c": [b"not-a-cert"]},
                 id="fido-u2f-garbage-x5c"),  # ValueError (asn1)
    # Keys py_webauthn decodes but never loads (#421): these used to REGISTER, and
    # then could never sign in.
    pytest.param(SoftAuthenticator(cose_key={1: 2, 3: -7, -1: 1,
                                             -2: b"\x00" * 32, -3: b"\x00" * 32}),
                 "none", None, id="ec2-off-curve"),  # ValueError at key load
    pytest.param(SoftAuthenticator(cose_key={1: 2, 3: -7, -1: 99,
                                             -2: _P256_X, -3: _P256_Y}),
                 "none", None, id="ec2-unknown-curve"),  # UnsupportedEC2Curve
    # A valid P-256 point labelled with an alg that isn't EC2's: loads fine, then
    # verify_signature's EC branch raises UnsupportedAlgorithm (Maxwell + Carnot, r2).
    pytest.param(SoftAuthenticator(cose_key=_on_curve_key(-257)),
                 "none", None, id="ec2-key-rs256-alg"),
    pytest.param(SoftAuthenticator(cose_key=_on_curve_key(-8)),
                 "none", None, id="ec2-key-eddsa-alg"),
])
async def test_register_finish_hostile_attestation_is_401(
    client, session, auth, fmt, att_stmt,
):
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    finish = await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"],
        "credential": auth.register(start["options"]["challenge"],
                                    fmt=fmt, att_stmt=att_stmt)})
    assert finish.status_code == 401, finish.text
    assert await passkey_service.get_credential(
        session, bytes_to_base64url(auth.credential_id)) is None


async def test_register_finish_server_misconfig_stays_loud(client, monkeypatch):
    """The catch-all covers py_webauthn's frames only. A fault in OUR inputs (here
    the origin list) must escape as a server error, never a 401 for the device."""
    monkeypatch.setattr(settings, "passkey_extra_origins", None)
    auth = SoftAuthenticator()
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    with pytest.raises(TypeError):
        await client.post("/v1/auth/passkey/register/finish", json={
            "state": start["state"],
            "credential": auth.register(start["options"]["challenge"])})


async def test_register_finish_malformed_then_honest_retry_same_state(client, session):
    """A refused body burns nothing: the SAME state then completes honestly."""
    auth = SoftAuthenticator()
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    good = auth.register(start["options"]["challenge"])
    bad = await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"], "credential": _strip(good, "response")})
    assert bad.status_code == 400, bad.text
    assert await _count_users(session) == 0
    ok = await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"], "credential": good})
    assert ok.status_code == 200, ok.text


async def test_add_passkey_malformed_credential_is_400(client, session):
    reg, _ = await _register_passkey(client)
    token = reg.json()["access_token"]
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    added = SoftAuthenticator(credential_id=b"added-malformed-0004")
    r = await client.post(
        "/v1/auth/passkey/add/finish",
        headers={"Authorization": f"Bearer {token}"},
        json={"state": start["state"], "credential": _strip(
            added.register(start["options"]["challenge"]), "response")})
    assert r.status_code == 400, r.text
    assert await passkey_service.get_credential(
        session, bytes_to_base64url(added.credential_id)) is None


async def test_add_passkey_hostile_attestation_is_401(client):
    reg, _ = await _register_passkey(client)
    token = reg.json()["access_token"]
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    added = SoftAuthenticator(credential_id=b"added-hostile-0005", cose_key={1: 9, 3: -7})
    r = await client.post(
        "/v1/auth/passkey/add/finish",
        headers={"Authorization": f"Bearer {token}"},
        json={"state": start["state"],
              "credential": added.register(start["options"]["challenge"])})
    assert r.status_code == 401, r.text


@pytest.mark.parametrize("field", ["response", "rawId"])
async def test_authenticate_finish_malformed_credential_is_400(client, field):
    """A registered authenticator's body with a field stripped: the parse, which
    runs before the credential lookup, refuses it as malformed."""
    reg, auth = await _register_passkey(client)
    assert reg.status_code == 200, reg.text
    start = (await client.post("/v1/auth/passkey/authenticate/start")).json()
    finish = await client.post("/v1/auth/passkey/authenticate/finish", json={
        "state": start["state"],
        "credential": _strip(auth.authenticate(start["options"]["challenge"]), field)})
    assert finish.status_code == 400, finish.text
    assert finish.json()["detail"] == "malformed passkey credential"


async def test_authenticate_finish_backed_up_not_eligible_is_401(client):
    """A correctly SIGNED assertion whose flags claim backed-up-but-not-eligible:
    py_webauthn checks this after the signature, unwrapped."""
    reg, auth = await _register_passkey(client)
    assert reg.status_code == 200, reg.text
    auth.extra_flags = 0x10  # BS without BE
    start = (await client.post("/v1/auth/passkey/authenticate/start")).json()
    finish = await client.post("/v1/auth/passkey/authenticate/finish", json={
        "state": start["state"], "credential": auth.authenticate(start["options"]["challenge"])})
    assert finish.status_code == 401, finish.text


async def test_authenticate_finish_bad_user_handle_is_400(client):
    """py_webauthn decodes userHandle outside its own wrapper: "a" raised a bare
    binascii.Error before any signature check (Carnot + Tesla, #422 r1)."""
    reg, auth = await _register_passkey(client)
    assert reg.status_code == 200, reg.text
    start = (await client.post("/v1/auth/passkey/authenticate/start")).json()
    cred = auth.authenticate(start["options"]["challenge"])
    cred["response"]["userHandle"] = "a"
    finish = await client.post("/v1/auth/passkey/authenticate/finish", json={
        "state": start["state"], "credential": cred})
    assert finish.status_code == 400, finish.text
    assert finish.json()["detail"] == "malformed passkey credential"


async def test_authenticate_finish_malformed_unknown_id_is_400(client):
    """Same malformed body, never-registered id: still 400, so the status does not
    reveal whether a credential id exists (it was 401 for unknown, 400 for known)."""
    ghost = SoftAuthenticator(credential_id=b"ghost-credential-0006")
    start = (await client.post("/v1/auth/passkey/authenticate/start")).json()
    finish = await client.post("/v1/auth/passkey/authenticate/finish", json={
        "state": start["state"],
        "credential": _strip(ghost.authenticate(start["options"]["challenge"]), "response")})
    assert finish.status_code == 400, finish.text


async def test_register_finish_refusal_logs_its_cause(client, caplog):
    """The service folds a dozen causes into one type; the route must log which one,
    or a hostile body and a real authenticator bug read the same to an operator."""
    auth = SoftAuthenticator(cose_key={1: 2, -1: 1, -2: _P256_X, -3: _P256_Y})  # KeyError
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    with caplog.at_level("WARNING", logger="aiko_gateway.rest.auth"):
        finish = await client.post("/v1/auth/passkey/register/finish", json={
            "state": start["state"], "credential": auth.register(start["options"]["challenge"])})
    assert finish.status_code == 401, finish.text
    assert "KeyError" in caplog.text
    assert "ALG" in caplog.text  # the inner detail (which COSE field), not just the class


async def test_authenticate_finish_corrupt_stored_key_stays_loud(client, session):
    """The stored key is OURS: one that won't decode is a server fault and must
    escape, never fold into a 401 that blames the device."""
    reg, auth = await _register_passkey(client)
    assert reg.status_code == 200, reg.text
    cred = await passkey_service.get_credential(
        session, bytes_to_base64url(auth.credential_id))
    cred.public_key = bytes_to_base64url(cbor2.dumps({1: 2}))  # no alg, no point
    await session.commit()
    start = (await client.post("/v1/auth/passkey/authenticate/start")).json()
    with pytest.raises(KeyError):  # what decode raises on a missing alg (measured)
        await client.post("/v1/auth/passkey/authenticate/finish", json={
            "state": start["state"],
            "credential": auth.authenticate(start["options"]["challenge"])})


# --- passkey labels (handle-first sign-up, app tab 2026-10-28) ------------- #

_RANDOM_LABEL = re.compile(r"aiko-[0-9a-f]{8}")


async def _start_label(client, **kwargs) -> str:
    resp = await client.post("/v1/auth/passkey/register/start", **kwargs)
    assert resp.status_code == 200, resp.text
    user = resp.json()["options"]["user"]
    assert user["displayName"] == user["name"]
    return user["name"]


async def test_register_start_labels_the_passkey_with_the_given_name(client):
    """The name becomes BOTH user.name and user.displayName, trimmed, so the phone's
    password manager shows the handle being chosen instead of `aiko-<hex>`."""
    assert await _start_label(client, json={"name": "  paul  "}) == "paul"


@pytest.mark.parametrize("kwargs", [
    {},                                                   # no body (today's app)
    {"json": {}},
    {"json": {"name": ""}},
    {"json": {"name": "   "}},
    {"json": {"name": 123}},
    {"json": ["paul"]},
    {"json": {"name": "pa\u202eul"}},                     # bidi override
    {"json": {"name": "nick\u200f"}},                     # RLM: looks exactly like nick
    {"json": {"name": "\u200bpaul"}},                     # zero-width space
    {"json": {"name": "pa\u0007ul"}},                     # control character
    {"json": {"name": "\u200d\u200d"}},                   # nothing visible
    {"content": b'{"name": "\\ud800"}', "headers": {"content-type": "application/json"}},
    {"content": b"[" * 60000, "headers": {"content-type": "application/json"}},
    {"content": b"{not json", "headers": {"content-type": "application/json"}},
    {"content": b"\xff\xfe", "headers": {"content-type": "application/json"}},
], ids=["absent", "empty-object", "empty", "blank", "non-string", "non-object",
        "bidi-override", "rlm", "zwsp", "control", "only-joiners", "lone-surrogate",
        "deeply-nested", "malformed", "not-utf8"])
async def test_register_start_falls_back_to_a_random_label_never_an_error(client, kwargs):
    """register/start is ALSO add-a-device's start, so a cosmetic field must never
    give it a new way to fail: every bad body is a 200 with today's random label. A
    name a handle couldn't be is refused, not rewritten (stripping would make a third
    string). The lone surrogate and the deeply nested body were both anonymous 500s
    (cage-match #435: Carnot, Maxwell)."""
    assert _RANDOM_LABEL.fullmatch(await _start_label(client, **kwargs))


@pytest.mark.parametrize("raw, label", [
    ("\U0001F469\u200d\U0001F4BBdev", "\U0001F469\u200d\U0001F4BBdev"),  # ZWJ kept
    ("x" * 64, "x" * 64),                                 # exactly 64 bytes: whole
    ("x" * 65, "x" * 64),                                 # cut to 64 bytes
    ("\u6f22" * 22, "\u6f22" * 21),                       # 66 bytes: whole chars only
    # 1 + 9 x (4 + 3) = 64 bytes ends exactly on a ZWJ, which must not be left dangling
    ("a" + "\U0001F469\u200d" * 20, "a" + "\U0001F469\u200d" * 8 + "\U0001F469"),
], ids=["zwj-emoji", "64-bytes", "65-bytes", "cjk-66-bytes", "no-dangling-zwj"])
async def test_register_start_cuts_the_label_to_64_bytes(client, raw, label):
    """CTAP bounds user.name at 64 BYTES, so the label is cut here, on a code-point
    boundary and never leaving a dangling joiner, rather than by the device, which may
    cut mid-character or refuse (cage-match #435: Tesla, Noether)."""
    got = await _start_label(client, json={"name": raw})
    assert got == label
    assert len(got.encode("utf-8")) <= 64


async def test_register_start_logs_why_there_is_no_label(client, caplog):
    """A fallback must be able to report itself: the log says absent, malformed or
    rejected (never the label), so a client bug isn't the same line as no label."""
    import logging
    with caplog.at_level(logging.INFO, logger="aiko_gateway.rest.auth"):
        await _start_label(client)
        await _start_label(client, json={"name": "nick\u200f"})
        await _start_label(client, content=b"{", headers={"content-type": "application/json"})
        await _start_label(client, json={"name": "paul"})
    lines = [r.getMessage() for r in caplog.records if "passkey.register.start" in r.getMessage()]
    assert [line.rsplit("label=", 1)[1] for line in lines] == [
        "absent", "rejected", "malformed", "given"]
    assert not any("paul" in line for line in lines)


async def test_a_label_grants_nothing(client, monkeypatch):
    """The label names the passkey on the caller's phone and NOTHING else: a start
    labelled "nick" still creates an account with an auto handle, so a label is never
    a way to claim someone's handle. The app claims a handle afterwards, with
    PATCH /v1/me, where taken handles and the cooldown are enforced."""
    monkeypatch.setattr(settings, "open_registration", True)
    auth = SoftAuthenticator()
    start = (await client.post("/v1/auth/passkey/register/start",
                               json={"name": "nick"})).json()
    assert start["options"]["user"]["name"] == "nick"
    finish = await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"], "credential": auth.register(start["options"]["challenge"])})
    assert finish.status_code == 200, finish.text
    user = finish.json()["user"]
    assert user["username"] != "nick"
    assert re.fullmatch(r"aiko-[0-9a-f]{12}", user["username"])
    assert user["handle_auto"] is True
