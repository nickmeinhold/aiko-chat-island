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
from sqlalchemy import select
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


async def test_unregister_cannot_remove_another_users_token(client, session):
    """The DELETE is scoped to the authenticated user (cage-match PR#28): an authed
    caller who knows another user's token cannot unregister it (a push-DoS vector).
    Bob tries to delete Alice's token — 204 (no existence oracle), but Alice's row
    survives."""
    alice = await _user(session, "alice")
    bob = await _user(session, "bob")
    await client.post("/v1/devices", json={"platform": "apns", "token": "alice-tok"},
                      headers=_headers(alice))
    resp = await client.request(
        "DELETE", "/v1/devices", json={"token": "alice-tok"}, headers=_headers(bob))
    assert resp.status_code == 204  # no leak that the token exists / belongs to alice
    survivors = await devices_service.tokens_for_user(session, alice.id)
    assert [r.token for r in survivors] == ["alice-tok"], "cross-user delete must not strip Alice's token"


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


# --- Recovered by the split audit (cage-match PR#170 round 2) ---------------
# Router-level token_kind coverage, written on the combined branch and dropped
# when this piece was split out.

async def test_a_voip_registration_round_trips_through_the_router(client, session):
    """The router's own arm of the token-kind wire (the service-level and
    DB-level arms live in `test_token_kind.py`)."""
    bob = await _user(session, "kindbob")
    resp = await client.post(
        "/v1/devices", headers=_headers(bob),
        json={"platform": "apns", "token": "j" * 64, "token_kind": "voip"})
    assert resp.status_code == 201
    assert resp.json()["token_kind"] == "voip"
    # AND THE ROW, not just the echo (Carnot, cage-match PR#170 r4). This asserted
    # the 201 body alone, so a service that echoes `req.token_kind` faithfully and
    # STORES 'alert' passed — while durable misclassification of a VoIP token is
    # this PR's central failure mode. The storage assertion exists in
    # test_token_kind.py, but this test names the ROUTER round trip and has to
    # discriminate the router-to-service leg itself.
    row = (await session.execute(
        select(DeviceToken).where(DeviceToken.token == "j" * 64))).scalar_one()
    assert row.token_kind == "voip", (
        "the router echoed voip but the row stored "
        f"{row.token_kind!r} — the echo is not evidence of storage")

async def test_the_registration_response_names_the_token_kind(client, session):
    """The 201 body grew a third field. It echoes the RESOLVED kind for the same
    stated reason `apns_environment` is echoed: a client that sent nothing learns
    what the island picked, which is the only way it can notice a mismatch with
    the build it actually is — and, measured, the only way an app shipping
    `token_kind` against an un-deployed island learns the field was discarded."""
    alice = await _user(session, "kindalice")
    resp = await client.post("/v1/devices", headers=_headers(alice),
                             json={"platform": "apns", "token": "k" * 64})
    assert resp.status_code == 201
    assert resp.json()["token_kind"] == "alert"


# ---------------------------------------------------------------------------
# THE DOCUMENT, NOT JUST THE BODY (Tesla, cage-match PR#170 r5).
#
# `RegisterDeviceResp` exists BECAUSE an untyped `-> dict` made the OpenAPI half of
# the desync detector undetectable: the 201 was `{"additionalProperties": true}` and
# `RegisterDeviceReq` was the only schema in the document carrying `token_kind`. The
# app tab stated it will verify against `openapi.json` before wiring its first VoIP
# registration.
#
# Nothing in this suite read that document. Revert the return annotation to `dict`,
# keep the same keys in the body, and every runtime test above stays green — so the
# round-1 fix for "this contract is not assertable" was itself not asserted. That is
# the same class this PR has now found ten times, at the highest level it can occur:
# the guard is unguarded.
# ---------------------------------------------------------------------------


def test_the_openapi_document_types_the_registration_echo() -> None:
    """Locks the CONTRACT the app tab verifies against, not the runtime body."""
    from aiko_gateway.main import app

    schemas = app.openapi()["components"]["schemas"]

    assert "RegisterDeviceResp" in schemas, (
        "the 201 response is untyped — openapi.json cannot tell a client that the "
        "island resolves and RETURNS a token_kind, only that it accepts one. That "
        "is exactly the half the app tab said it would check.")

    props = schemas["RegisterDeviceResp"]["properties"]
    assert "token_kind" in props, (
        f"RegisterDeviceResp does not carry token_kind: {sorted(props)}")

    # EVERY closed set typed, not just the new one — a contract that says
    # `platform: Platform` inbound and `platform: string` outbound has to be read
    # twice, and this repo's rule is that a closed set is never a String.
    for field, schema_name in (("platform", "Platform"),
                               ("apns_environment", "ApnsEnvironment"),
                               ("token_kind", "TokenKind")):
        ref = props[field].get("$ref") or "".join(
            a.get("$ref", "") for a in props[field].get("anyOf", []))
        assert schema_name in ref, (
            f"RegisterDeviceResp.{field} is not typed as {schema_name} in the "
            f"document — it resolved to {props[field]!r}")
