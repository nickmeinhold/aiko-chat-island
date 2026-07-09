"""Signing-key binding (#1816 PR B) — the pubkey->account roster.

The boundary under test: the gateway is a CARRIER, not a verifier, so a signing
key is RECORDED as an observation ("this authed account used this key"), never
adjudicated as ownership. Three layers:

  * the single-door mutator ``record_signing_key`` (idempotent upsert, PER-USER
    uniqueness — a cross-user duplicate is a recorded signal, not a write failure);
  * the IMPLICIT binding folded into ``create_outbound`` (a signed send observes
    its sender's key, atomically with the message);
  * the EXPLICIT ``/v1/keys`` routes (register/list/revoke), bound to the authed
    user, boundary-validated, scoped so no route crosses accounts.

Built from JUST the keys router (never ``main``) to keep the suite's "never import
aiko_services" isolation invariant — same pattern as test_devices.
"""
from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from aiko_gateway.domain import (
    accounts_service, messages_service, security, signing,
    signing_keys_service as svc, users_service,
)
from aiko_gateway.domain.models import Channel, Message, SigningKey, User
from aiko_gateway.rest import keys as key_routes
from aiko_gateway.rest.deps import get_session

# A canonical, externally-verified ed25519 Multikey (same vector the carriage
# suite pins the decoder against) — a real, shape-valid pubkey for the routes that
# validate shape.
VALID_MK = "z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"

# -- helpers -----------------------------------------------------------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(b: bytes) -> str:
    num = int.from_bytes(b, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _B58[rem] + out
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + out


def _multikey(raw_pub: bytes) -> str:
    return "z" + _b58encode(b"\xed\x01" + raw_pub)


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _origin_for(priv: Ed25519PrivateKey, *, channel_id: str, client_msg_id: str,
                signed_at_ms: int, body: str, reply_to: str | None = None) -> dict:
    raw_pub = priv.public_key().public_bytes_raw()
    sig = priv.sign(signing.signing_bytes(
        raw_pubkey=raw_pub, channel_id=channel_id, client_msg_id=client_msg_id,
        signed_at_ms=signed_at_ms, body=body, reply_to=reply_to))
    return {
        "v": 1, "alg": "EdDSA", "key_version": 1,
        "sender_pubkey": _multikey(raw_pub),
        "client_msg_id": client_msg_id,
        "signed_at_ms": signed_at_ms,
        "sig": _b64url(sig),
    }


async def _user(session, username: str):
    return await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")


async def _key_count(session, user_id: str) -> int:
    return (await session.execute(
        select(func.count()).select_from(SigningKey)
        .where(SigningKey.user_id == user_id))).scalar_one()


def _naive(d):
    """Drop tzinfo for comparison — SQLite (the prod engine) stores DateTime
    timezone-blind, so a DB-roundtripped value reads back naive while an in-memory
    value is tz-aware. Normalize both sides before comparing instants."""
    return d.replace(tzinfo=None) if d.tzinfo is not None else d


# ============================================================ service (the door)

async def test_first_observation_inserts_with_equal_timestamps(session):
    alice = await _user(session, "alice")
    row = await svc.record_signing_key(
        session, user_id=alice.id, pubkey="z-alice-key", key_version=3)
    await session.commit()
    assert row.pubkey == "z-alice-key"
    assert row.key_version == 3
    assert row.first_seen_at == row.last_seen_at  # first sight: not yet re-seen
    assert await _key_count(session, alice.id) == 1


async def test_reobservation_bumps_last_seen_without_duplicating(session):
    """Idempotent on (user, pubkey): a repeat bumps last_seen_at, does not dupe,
    and does NOT overwrite the first-seen key_version."""
    alice = await _user(session, "alice")
    first = await svc.record_signing_key(
        session, user_id=alice.id, pubkey="z-alice-key", key_version=1)
    await session.commit()
    first_seen = first.first_seen_at

    again = await svc.record_signing_key(
        session, user_id=alice.id, pubkey="z-alice-key", key_version=9)
    await session.commit()
    assert await _key_count(session, alice.id) == 1, "re-record must not duplicate"
    assert _naive(again.first_seen_at) == _naive(first_seen), "first_seen is immutable"
    assert _naive(again.last_seen_at) >= _naive(first_seen)
    assert again.key_version == 1, "a fixed pubkey keeps its first-seen version"


async def test_distinct_pubkeys_for_one_user_are_separate_rows(session):
    alice = await _user(session, "alice")
    await svc.record_signing_key(session, user_id=alice.id, pubkey="z-key-1")
    await svc.record_signing_key(session, user_id=alice.id, pubkey="z-key-2")
    await session.commit()
    assert await _key_count(session, alice.id) == 2


async def test_same_pubkey_across_users_is_allowed_two_rows(session):
    """THE per-user-uniqueness stance, proven: the same pubkey recorded against two
    accounts is NOT a conflict — both observations survive as a signal a future
    trust root can adjudicate. A global UNIQUE(pubkey) would (wrongly) reject the
    second here. Carrier records what it sees; it does not decide ownership."""
    alice = await _user(session, "alice")
    bob = await _user(session, "bob")
    shared = "z-contested-key"
    ra = await svc.record_signing_key(session, user_id=alice.id, pubkey=shared)
    rb = await svc.record_signing_key(session, user_id=bob.id, pubkey=shared)
    await session.commit()
    assert ra.id != rb.id
    assert await _key_count(session, alice.id) == 1
    assert await _key_count(session, bob.id) == 1


async def test_list_keys_is_scoped_and_ordered(session):
    alice = await _user(session, "alice")
    bob = await _user(session, "bob")
    await svc.record_signing_key(session, user_id=alice.id, pubkey="z-a1")
    await svc.record_signing_key(session, user_id=alice.id, pubkey="z-a2")
    await svc.record_signing_key(session, user_id=bob.id, pubkey="z-b1")
    await session.commit()
    alice_keys = await svc.list_keys(session, alice.id)
    assert [k.pubkey for k in alice_keys] == ["z-a1", "z-a2"]  # first_seen order
    assert [k.pubkey for k in await svc.list_keys(session, bob.id)] == ["z-b1"]


async def test_revoke_is_scoped_to_the_user(session):
    """Revoke is scoped to (user, pubkey): a caller who learns another account's
    (public) key cannot delete that account's binding."""
    alice = await _user(session, "alice")
    bob = await _user(session, "bob")
    await svc.record_signing_key(session, user_id=alice.id, pubkey="z-shared")
    await svc.record_signing_key(session, user_id=bob.id, pubkey="z-shared")
    await session.commit()
    # Bob revoking "z-shared" removes only HIS row.
    assert await svc.revoke_key(session, user_id=bob.id, pubkey="z-shared") is True
    assert await _key_count(session, bob.id) == 0
    assert await _key_count(session, alice.id) == 1, "cross-user revoke must not strip alice"


async def test_revoke_unknown_key_returns_false(session):
    alice = await _user(session, "alice")
    assert await svc.revoke_key(session, user_id=alice.id, pubkey="z-never") is False


async def test_purge_removes_all_keys_for_user(session):
    alice = await _user(session, "alice")
    bob = await _user(session, "bob")
    await svc.record_signing_key(session, user_id=alice.id, pubkey="z-a1")
    await svc.record_signing_key(session, user_id=alice.id, pubkey="z-a2")
    await svc.record_signing_key(session, user_id=bob.id, pubkey="z-b1")
    await session.commit()
    await svc.purge_user_keys(session, alice.id)
    await session.commit()
    assert await _key_count(session, alice.id) == 0
    assert await _key_count(session, bob.id) == 1, "purge is scoped to the user"


# ==================================================== implicit binding (send path)

async def _channel_and_user(session):
    channel = Channel(id="0" * 26, name="general", kind="standard", aiko_channel="general")
    user = User(id="u" * 26, username="ada", display_name="Ada", aiko_username="ada")
    session.add_all([channel, user])
    await session.commit()
    return channel, user


async def test_signed_send_records_binding_atomically(session):
    """A signed message observes its sender's key at send time — the message AND
    the binding both exist after the single create_outbound commit (atomic)."""
    channel, user = await _channel_and_user(session)
    priv = Ed25519PrivateKey.generate()
    cmid = "tmp-1"
    origin = signing.validate_origin(
        _origin_for(priv, channel_id=channel.id, client_msg_id=cmid,
                    signed_at_ms=1720000000000, body="hi"),
        frame_client_msg_id=cmid)
    row, created = await messages_service.create_outbound(
        session, user=user, channel=channel, body="hi", client_msg_id=cmid, origin=origin)
    assert created
    # message persisted
    assert await session.get(Message, row.id) is not None
    # binding persisted, bound to the authed user, carrying the origin's pubkey
    keys = await svc.list_keys(session, user.id)
    assert [k.pubkey for k in keys] == [origin["sender_pubkey"]]


async def test_resend_does_not_duplicate_binding(session):
    channel, user = await _channel_and_user(session)
    priv = Ed25519PrivateKey.generate()
    cmid = "tmp-1"
    origin = signing.validate_origin(
        _origin_for(priv, channel_id=channel.id, client_msg_id=cmid,
                    signed_at_ms=1720000000000, body="hi"),
        frame_client_msg_id=cmid)
    for _ in range(2):
        await messages_service.create_outbound(
            session, user=user, channel=channel, body="hi",
            client_msg_id=cmid, origin=origin)
    assert await _key_count(session, user.id) == 1


async def test_unsigned_send_records_no_binding(session):
    channel, user = await _channel_and_user(session)
    await messages_service.create_outbound(
        session, user=user, channel=channel, body="hi",
        client_msg_id="m1", origin=None)
    assert await _key_count(session, user.id) == 0


# ============================================================ REST (explicit door)

def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(key_routes.router)
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


async def test_post_registers_key_bound_to_authed_user(client, session):
    alice = await _user(session, "alice")
    resp = await client.post(
        "/v1/keys", json={"pubkey": VALID_MK, "key_version": 2},
        headers=_headers(alice))
    assert resp.status_code == 201
    keys = await svc.list_keys(session, alice.id)
    assert [(k.pubkey, k.key_version) for k in keys] == [(VALID_MK, 2)]


async def test_post_is_idempotent(client, session):
    alice = await _user(session, "alice")
    for _ in range(2):
        resp = await client.post(
            "/v1/keys", json={"pubkey": VALID_MK}, headers=_headers(alice))
        assert resp.status_code == 201
    assert await _key_count(session, alice.id) == 1


async def test_get_lists_only_the_callers_keys(client, session):
    alice = await _user(session, "alice")
    bob = await _user(session, "bob")
    await client.post("/v1/keys", json={"pubkey": VALID_MK}, headers=_headers(alice))
    bob_mk = _multikey(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    await client.post("/v1/keys", json={"pubkey": bob_mk}, headers=_headers(bob))
    resp = await client.get("/v1/keys", headers=_headers(alice))
    assert resp.status_code == 200
    assert [k["pubkey"] for k in resp.json()] == [VALID_MK]


async def test_same_pubkey_registered_by_two_users_via_api(client, session):
    """The per-user stance at the API layer: both accounts registering the same
    (public) key succeed — no 409, both rows recorded."""
    alice = await _user(session, "alice")
    bob = await _user(session, "bob")
    r1 = await client.post("/v1/keys", json={"pubkey": VALID_MK}, headers=_headers(alice))
    r2 = await client.post("/v1/keys", json={"pubkey": VALID_MK}, headers=_headers(bob))
    assert r1.status_code == 201 and r2.status_code == 201
    assert await _key_count(session, alice.id) == 1
    assert await _key_count(session, bob.id) == 1


async def test_delete_revokes_the_callers_key(client, session):
    alice = await _user(session, "alice")
    await client.post("/v1/keys", json={"pubkey": VALID_MK}, headers=_headers(alice))
    resp = await client.delete(f"/v1/keys/{VALID_MK}", headers=_headers(alice))
    assert resp.status_code == 204
    assert await _key_count(session, alice.id) == 0


async def test_delete_cannot_remove_another_users_key(client, session):
    """Scoped revoke: bob deleting alice's (public) key is a 204 (no existence
    oracle) but alice's row survives."""
    alice = await _user(session, "alice")
    bob = await _user(session, "bob")
    await client.post("/v1/keys", json={"pubkey": VALID_MK}, headers=_headers(alice))
    resp = await client.delete(f"/v1/keys/{VALID_MK}", headers=_headers(bob))
    assert resp.status_code == 204
    assert await _key_count(session, alice.id) == 1, "cross-user revoke must not strip alice"


async def test_delete_unknown_key_is_still_204(client, session):
    alice = await _user(session, "alice")
    resp = await client.delete("/v1/keys/z-never", headers=_headers(alice))
    assert resp.status_code == 204


async def test_post_requires_auth(client, session):
    resp = await client.post("/v1/keys", json={"pubkey": VALID_MK})
    assert resp.status_code in (401, 403)  # HTTPBearer auto_error -> 403 on missing


async def test_get_requires_auth(client, session):
    resp = await client.get("/v1/keys")
    assert resp.status_code in (401, 403)


async def test_post_rejects_malformed_pubkey_with_400(client, session):
    """A pubkey that is not a well-formed ed25519 Multikey is rejected at the
    boundary (400) via signing.decode_multikey, before any row is written — the
    carrier never stores a value it would later echo as if canonical."""
    alice = await _user(session, "alice")
    resp = await client.post(
        "/v1/keys", json={"pubkey": "Qm-not-a-multikey"}, headers=_headers(alice))
    assert resp.status_code == 400
    assert await _key_count(session, alice.id) == 0


async def test_post_rejects_bad_multicodec_pubkey_with_400(client, session):
    """Charset-valid but not an ed25519 Multikey (wrong multicodec / length)."""
    alice = await _user(session, "alice")
    resp = await client.post(
        "/v1/keys", json={"pubkey": "z" + "1" * 40}, headers=_headers(alice))
    assert resp.status_code == 400
    assert await _key_count(session, alice.id) == 0


async def test_post_rejects_key_version_below_one_with_422(client, session):
    """key_version >= 1 is enforced at the request model (422), before the handler."""
    alice = await _user(session, "alice")
    resp = await client.post(
        "/v1/keys", json={"pubkey": VALID_MK, "key_version": 0},
        headers=_headers(alice))
    assert resp.status_code == 422
    assert await _key_count(session, alice.id) == 0


# ============================================================ account deletion

async def test_account_deletion_purges_signing_keys(session):
    """Signing keys are an FK child of users — account deletion must tear them down
    (verify-the-neighbor: the cascade in accounts_service learned about this new
    table). The dedicated cascade-guard tests prove completeness generically; this
    is the topic-local proof."""
    alice = await _user(session, "alice")
    await svc.record_signing_key(session, user_id=alice.id, pubkey=VALID_MK)
    await session.commit()
    await accounts_service.delete_user_account(session, alice.id)
    assert await _key_count(session, alice.id) == 0
    assert await users_service.get_by_id(session, alice.id) is None
