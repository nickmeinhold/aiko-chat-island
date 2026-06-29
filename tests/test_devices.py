"""Device-token registration (#16, increment 1) — the push-notification roster.

The boundary under test: a device push token routes to exactly ONE current user.
Registration is an upsert keyed on the globally-unique token (reassign on
conflict), the token is always bound to the AUTHENTICATED user (never a client
body user_id), and the platform closed-set is enforced at BOTH the API boundary
(422) and the DB CHECK (defense beyond the API, mirroring #11).

Built from JUST the devices router (never `main`) to keep the suite's "never
import aiko_services" isolation invariant — same pattern as test_membership_acl.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from aiko_gateway.domain import (
    accounts_service, devices_service, security, users_service,
)
from aiko_gateway.domain.models import DeviceToken
from aiko_gateway.rest import devices as device_routes
from aiko_gateway.rest.deps import get_session


async def _user(session, username: str):
    return await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(device_routes.router)
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


# ---------------------------------------------------------------- registration

async def test_register_creates_token_bound_to_authed_user(client, session):
    alice = await _user(session, "alice")
    resp = await client.post(
        "/v1/devices", json={"platform": "apns", "token": "tok-a"},
        headers=_headers(alice))
    assert resp.status_code == 201
    rows = await devices_service.tokens_for_user(session, alice.id)
    assert [(r.token, r.platform) for r in rows] == [("tok-a", "apns")]


async def test_register_is_idempotent_for_same_user_and_token(client, session):
    alice = await _user(session, "alice")
    for _ in range(2):
        resp = await client.post(
            "/v1/devices", json={"platform": "fcm", "token": "tok-dup"},
            headers=_headers(alice))
        assert resp.status_code == 201
    rows = await devices_service.tokens_for_user(session, alice.id)
    assert len(rows) == 1, "re-registering the same token must not duplicate"


async def test_register_existing_token_reassigns_to_new_owner(client, session):
    """A device that changes hands (logout A -> login B on the same phone)
    re-registers the SAME token. It must move to B, not create a second row, and
    A must no longer own it — otherwise a push for A would land on B's session."""
    alice = await _user(session, "alice")
    bob = await _user(session, "bob")
    await client.post("/v1/devices", json={"platform": "apns", "token": "shared"},
                      headers=_headers(alice))
    await client.post("/v1/devices", json={"platform": "apns", "token": "shared"},
                      headers=_headers(bob))
    assert await devices_service.tokens_for_user(session, alice.id) == []
    bob_rows = await devices_service.tokens_for_user(session, bob.id)
    assert [r.token for r in bob_rows] == ["shared"]


# ---------------------------------------------------------------- unregister

async def test_unregister_removes_token(client, session):
    alice = await _user(session, "alice")
    await client.post("/v1/devices", json={"platform": "apns", "token": "bye"},
                      headers=_headers(alice))
    resp = await client.request(
        "DELETE", "/v1/devices", json={"token": "bye"}, headers=_headers(alice))
    assert resp.status_code == 204
    assert await devices_service.tokens_for_user(session, alice.id) == []


async def test_unregister_unknown_token_is_still_204(client, session):
    """Idempotent + no existence oracle: unregistering a token that was never
    registered is a no-op success, not a 404 (which would confirm registration)."""
    alice = await _user(session, "alice")
    resp = await client.request(
        "DELETE", "/v1/devices", json={"token": "never"}, headers=_headers(alice))
    assert resp.status_code == 204


# ---------------------------------------------------------------- boundaries

async def test_register_requires_auth(client, session):
    resp = await client.post(
        "/v1/devices", json={"platform": "apns", "token": "x"})
    assert resp.status_code in (401, 403)  # HTTPBearer auto_error -> 403 on missing


async def test_register_rejects_unknown_platform_with_422(client, session):
    """The Platform enum on the request model rejects an out-of-set value at the
    boundary (422), before any row is touched — never a silent store the DB CHECK
    would later 500 on."""
    alice = await _user(session, "alice")
    resp = await client.post(
        "/v1/devices", json={"platform": "windows", "token": "t"},
        headers=_headers(alice))
    assert resp.status_code == 422
    assert await devices_service.tokens_for_user(session, alice.id) == []


async def test_db_check_rejects_bad_platform_beyond_the_api(session):
    """Defense beyond the API boundary (#11 pattern): even a direct write that
    bypasses the Pydantic enum cannot store an out-of-set platform — the DB CHECK
    (ck_device_tokens_platform) rejects it. A distinct user/token so the failure
    is the CHECK, not a unique/PK collision (test-green-for-the-right-reason)."""
    alice = await _user(session, "alice")
    session.add(DeviceToken(user_id=alice.id, platform="symbian", token="weird"))
    with pytest.raises(IntegrityError) as ei:
        await session.commit()
    assert "ck_device_tokens_platform" in str(ei.value)


# ---------------------------------------------------------------- account deletion

async def test_account_deletion_purges_device_tokens(session):
    """Device tokens are an FK child of users — account deletion must tear them
    down (verify-the-neighbor: the cascade in accounts_service learned about this
    new table). Otherwise the final User delete would FK-violate, or leave an
    orphan token routing pushes to a dead account."""
    alice = await _user(session, "alice")
    await devices_service.register_device(
        session, user_id=alice.id, platform="apns", token="doomed")
    await accounts_service.delete_user_account(session, alice.id)
    assert await devices_service.tokens_for_user(session, alice.id) == []
    assert await users_service.get_by_id(session, alice.id) is None
