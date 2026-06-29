"""WebAuthn passkey domain service (#1471) — the parts we OWN, exercised not mocked.

The crypto verify (attestation/assertion) is delegated to py_webauthn and proven
end-to-end at the endpoint level with an inline software authenticator
(test_passkey_endpoints). Here we drive the logic that is OURS: the single-use +
operation-pinned challenge guard, the #24 atomic-with-outcome deferred commit, and
credential persistence — against the same throwaway-SQLite `session` harness as the
rest of the suite.
"""
from __future__ import annotations

from sqlalchemy import func, select

from aiko_gateway.config import settings
from aiko_gateway.domain import passkey_service
from aiko_gateway.domain.models import (
    PasskeyChallenge, PasskeyCredential, PasskeyOperation, User,
)
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


async def test_persist_and_get_credential(session):
    user = User(username="pk_user", display_name="PK", aiko_username="pk_user")
    session.add(user)
    await session.commit()
    material = {
        "credential_id": "Y3JlZC1pZC1iNjR1cmw",
        "public_key": "cHVibGljLWtleS1jb3Nl",
        "sign_count": 0,
        "transports": '["internal"]',
        "aaguid": "00000000-0000-0000-0000-000000000000",
    }
    await passkey_service.persist_credential(session, user_id=user.id, material=material)
    await session.commit()
    row = await passkey_service.get_credential(session, "Y3JlZC1pZC1iNjR1cmw")
    assert row is not None
    assert row.user_id == user.id
    assert row.public_key == "cHVibGljLWtleS1jb3Nl"
    assert row.sign_count == 0
    # Unknown credential id → None (fail-closed lookup).
    assert await passkey_service.get_credential(session, "nope") is None
