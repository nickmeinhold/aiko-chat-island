"""WebAuthn passkey domain service (#1471) — the parts we OWN, exercised not mocked.

The crypto verify (attestation/assertion) is delegated to py_webauthn and proven
end-to-end at the endpoint level with an inline software authenticator
(test_passkey_endpoints). Here we drive the logic that is OURS: the single-use +
operation-pinned challenge guard, the #24 atomic-with-outcome deferred commit, and
credential persistence — against the same throwaway-SQLite `session` harness as the
rest of the suite.
"""
from __future__ import annotations

import cbor2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from sqlalchemy import func, select

from aiko_gateway.config import settings
from aiko_gateway.domain import passkey_service
from aiko_gateway.domain.models import PasskeyChallenge, PasskeyOperation
from webauthn.helpers import base64url_to_bytes


async def test_start_registration_returns_state_and_webauthn_options(session):
    out = await passkey_service.start_registration(session)
    assert isinstance(out["state"], str)
    opts = out["options"]
    # The raw WebAuthn-JSON the platform authenticator parses.
    assert opts["rp"]["id"] == settings.passkey_rp_id
    assert "challenge" in opts and "pubKeyCredParams" in opts and "user" in opts
    # state is the handle for the SAME challenge embedded in the options.
    assert base64url_to_bytes(out["state"]) == base64url_to_bytes(opts["challenge"])


async def test_start_registration_user_handle_within_android_limit(session):
    # The WebAuthn user handle must be <= 32 bytes: py_webauthn defaults it to the
    # spec MAXIMUM (64 bytes) when user_id is omitted, and Android's Credential
    # Manager rejects a 64-byte handle at the boundary with TYPE_DATA_ERROR (#1957).
    out = await passkey_service.start_registration(session)
    handle = base64url_to_bytes(out["options"]["user"]["id"])
    assert len(handle) <= 32, f"user.id is {len(handle)} bytes; Android rejects > 32"


async def test_start_registration_offers_default_alg_set_incl_eddsa(session):
    # #52 revert: PR#98 dropped EdDSA (-8) on the theory Android hard-rejects it at
    # the create boundary, but that diagnosis was REFUTED — the real Android CREATE
    # blocker was the assetlinks handle_all_urls relation ([50152] RP-ID, PR#101),
    # not the alg set. With the narrowing's justification gone, we restore
    # py_webauthn's default {ES256(-7), EdDSA(-8), RS256(-257)} so EdDSA-capable
    # authenticators aren't needlessly excluded.
    out = await passkey_service.start_registration(session)
    algs = {p["alg"] for p in out["options"]["pubKeyCredParams"]}
    assert algs == {-7, -8, -257}, f"expected the default ES256+EdDSA+RS256, got {algs}"


def _int_bytes(i: int) -> bytes:
    return i.to_bytes((i.bit_length() + 7) // 8, "big")


def _es256_key() -> dict:
    nums = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    return {1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"),
            -3: nums.y.to_bytes(32, "big")}


def _eddsa_key() -> dict:
    raw = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {1: 1, 3: -8, -1: 6, -2: raw}


def _rs256_key() -> dict:
    nums = rsa.generate_private_key(
        public_exponent=65537, key_size=2048).public_key().public_numbers()
    return {1: 3, 3: -257, -1: _int_bytes(nums.n), -2: _int_bytes(nums.e)}


@pytest.mark.parametrize("make_key", [
    pytest.param(_es256_key, id="es256-p256"),
    pytest.param(_eddsa_key, id="eddsa-ed25519"),
    pytest.param(_rs256_key, id="rs256-rsa2048"),
])
def test_assert_can_verify_accepts_a_real_key_of_every_offered_alg(make_key):
    """The other half of the #421 predicate: a GOOD key of each alg we offer
    (the test above) passes. The endpoint suite only ever registers ES256, so a
    cryptography or predicate change that refused RS256 (Windows Hello) or EdDSA
    would otherwise show up as 401s in production, not as a red test (#428)."""
    passkey_service._assert_can_verify(cbor2.dumps(make_key()))


async def test_start_authentication_is_usernameless(session):
    out = await passkey_service.start_authentication(session)
    opts = out["options"]
    assert opts["rpId"] == settings.passkey_rp_id
    # Discoverable: no allowCredentials restriction (empty or absent).
    assert not opts.get("allowCredentials")


async def test_consume_challenge_once_then_rejected(session):
    out = await passkey_service.start_registration(session)
    raw = await passkey_service.consume_challenge(
        session, out["state"], PasskeyOperation.REGISTER)
    assert raw == base64url_to_bytes(out["state"])          # returns the challenge bytes
    # Single-use: a replayed finish fails closed.
    assert await passkey_service.consume_challenge(
        session, out["state"], PasskeyOperation.REGISTER) is None


async def test_consume_wrong_operation_rejected(session):
    """Operation pinning (in the atomic WHERE clause): a REGISTER challenge cannot
    complete an AUTHENTICATE ceremony, and the failed match must NOT burn it."""
    out = await passkey_service.start_registration(session)
    assert await passkey_service.consume_challenge(
        session, out["state"], PasskeyOperation.AUTHENTICATE) is None
    # The mismatched attempt did not consume the row — the correct ceremony still works.
    assert await passkey_service.consume_challenge(
        session, out["state"], PasskeyOperation.REGISTER) == base64url_to_bytes(out["state"])


async def test_consume_unknown_state_rejected(session):
    assert await passkey_service.consume_challenge(
        session, "never-issued", PasskeyOperation.REGISTER) is None


async def test_consume_expired_rejected(session, monkeypatch):
    monkeypatch.setattr(settings, "passkey_challenge_ttl_seconds", -1)  # born expired
    out = await passkey_service.start_registration(session)
    assert await passkey_service.consume_challenge(
        session, out["state"], PasskeyOperation.REGISTER) is None


async def test_consume_defers_commit_so_rollback_unburns(session):
    """#24 atomic-with-outcome: consume CLAIMS but does not commit, so a downstream
    finish failure (rollback) revives the challenge for an honest retry.

    RED-proof: add `await session.commit()` to consume_challenge and the post-
    rollback consume returns None (the burn was made durable before the rollback)."""
    out = await passkey_service.start_registration(session)            # commits the row
    assert await passkey_service.consume_challenge(
        session, out["state"], PasskeyOperation.REGISTER) is not None  # claim (uncommitted)
    await session.rollback()
    assert await passkey_service.consume_challenge(
        session, out["state"], PasskeyOperation.REGISTER) is not None  # consumable again


async def test_start_prunes_expired_challenges(session, monkeypatch):
    monkeypatch.setattr(settings, "passkey_challenge_ttl_seconds", -1)
    await passkey_service.start_registration(session)                  # born expired
    monkeypatch.setattr(settings, "passkey_challenge_ttl_seconds", 300)
    await passkey_service.start_registration(session)                  # prunes the expired one
    count = (await session.execute(
        select(func.count()).select_from(PasskeyChallenge))).scalar()
    assert count == 1


async def test_create_passkey_user_then_get_credential(session):
    from aiko_gateway.domain import users_service

    material = {
        "credential_id": "Y3JlZC1pZC1iNjR1cmw",
        "public_key": "cHVibGljLWtleS1jb3Nl",
        "sign_count": 0,
        "transports": '["internal"]',
        "aaguid": "00000000-0000-0000-0000-000000000000",
    }
    user = await users_service.create_passkey_user(
        session, handle="pk_user", display_name="PK", email=None, material=material)
    row = await passkey_service.get_credential(session, "Y3JlZC1pZC1iNjR1cmw")
    assert row is not None
    assert row.user_id == user.id
    assert row.public_key == "cHVibGljLWtleS1jb3Nl"
    assert row.sign_count == 0
    # Unknown credential id → None (fail-closed lookup).
    assert await passkey_service.get_credential(session, "nope") is None
