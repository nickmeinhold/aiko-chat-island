"""Passkey endpoints (#1471) — the FULL ceremony proven end to end.

soft-webauthn (the off-the-shelf software authenticator) is dep-incompatible with
our cryptography pin (it needs <45 via fido2; py_webauthn needs >=46), so we drive
a minimal inline P-256 authenticator that produces REAL attestation/assertion
responses through the genuine py_webauthn verification path — no mocking of the
crypto boundary. This is what proves register -> claim -> authenticate works, the
sign_count contract holds, and the outcome shapes match the social/broker door.
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

from aiko_gateway.config import settings
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

    def __init__(self, credential_id: bytes = b"test-credential-0001"):
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = credential_id
        self.sign_count = 0  # platform passkeys report 0 and never increment

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
        # UP | UV | AT (attested credential data present)
        auth_data = self._auth_data(flags=0x01 | 0x04 | 0x40, include_cred=True)
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
        auth_data = self._auth_data(flags=0x01 | 0x04, include_cred=False)  # UP | UV
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


async def _register_and_claim(client, handle="passkey_nick", auth=None):
    """Drive register/start -> device -> register/finish -> claim. Returns the
    claim response (authenticated) and the authenticator."""
    auth = auth or SoftAuthenticator()
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    finish = await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"], "credential": auth.register(start["options"]["challenge"])})
    assert finish.status_code == 200, finish.text
    body = finish.json()
    assert body["status"] == "provisioning" and body["provisioning_token"]
    claim = await client.post("/v1/auth/social/claim", json={
        "provisioning_token": body["provisioning_token"],
        "handle": handle, "display_name": "PK Nick"})
    return claim, auth


# --- full ceremonies ------------------------------------------------------- #

async def test_register_then_claim_creates_authenticated_user(client):
    claim, _ = await _register_and_claim(client)
    assert claim.status_code == 200, claim.text
    body = claim.json()
    # Byte-identical to the social/broker authenticated outcome shape.
    assert set(body) == {"access_token", "refresh_token", "user"}
    assert set(body["user"]) == {
        "user_id", "username", "display_name", "aiko_username"}
    assert body["user"]["username"] == "passkey_nick"


async def test_full_round_trip_register_claim_authenticate(client):
    """The whole point: a registered passkey can sign in afterwards."""
    claim, auth = await _register_and_claim(client)
    assert claim.status_code == 200
    start = (await client.post("/v1/auth/passkey/authenticate/start")).json()
    finish = await client.post("/v1/auth/passkey/authenticate/finish", json={
        "state": start["state"], "credential": auth.authenticate(start["options"]["challenge"])})
    assert finish.status_code == 200, finish.text
    body = finish.json()
    assert "access_token" in body and "refresh_token" in body
    assert body["user"]["username"] == "passkey_nick"


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
    claim, _ = await _register_and_claim(client)
    assert claim.status_code == 200
    impostor = SoftAuthenticator(credential_id=b"test-credential-0001")  # same id, new key
    start = (await client.post("/v1/auth/passkey/authenticate/start")).json()
    finish = await client.post("/v1/auth/passkey/authenticate/finish", json={
        "state": start["state"], "credential": impostor.authenticate(start["options"]["challenge"])})
    assert finish.status_code == 401


async def test_replayed_provisioning_token_conflicts(client):
    """Claiming the same passkey provisioning token twice → 409 (credential_id
    UNIQUE; the whole second create rolls back, no orphan user)."""
    auth = SoftAuthenticator()
    start = (await client.post("/v1/auth/passkey/register/start")).json()
    finish = (await client.post("/v1/auth/passkey/register/finish", json={
        "state": start["state"], "credential": auth.register(start["options"]["challenge"])})).json()
    tok = finish["provisioning_token"]
    r1 = await client.post("/v1/auth/social/claim", json={
        "provisioning_token": tok, "handle": "first", "display_name": ""})
    assert r1.status_code == 200
    r2 = await client.post("/v1/auth/social/claim", json={
        "provisioning_token": tok, "handle": "second", "display_name": ""})
    assert r2.status_code == 409


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


async def test_well_known_assetlinks(client):
    r = await client.get("/.well-known/assetlinks.json")
    assert r.status_code == 200
    entry = r.json()[0]
    assert entry["relation"] == ["delegate_permission/common.get_login_creds"]
    assert entry["target"]["package_name"] == settings.passkey_android_package
    # Fingerprints empty until Play signing is registered (app #20).
    assert entry["target"]["sha256_cert_fingerprints"] == settings.passkey_android_cert_sha256
