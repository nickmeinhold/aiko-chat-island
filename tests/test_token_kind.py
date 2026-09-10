"""`token_kind` — which DELIVERY SEMANTICS a device token was minted for.

THE AXIS, because getting it wrong is the whole risk. `platform` says which
transport FAMILY (Apple vs Google); `token_kind` says which delivery semantics
(a banner vs a ring). An `apns_voip` PLATFORM value would be the wrong axis —
Android could never hold it.

The two tokens come from DIFFERENT REGISTRIES (UIKit
`registerForRemoteNotifications` vs PushKit `PKPushRegistry`), are different
strings, and rotate independently. So they are two ROWS, and `UNIQUE(token)`
needs no change.

THE CLOSED SET IS ENFORCED AT THREE LAYERS, each with its own arm, because the
0022 lesson is that `NULL IN ('alert','voip')` is UNKNOWN and a CHECK constraint
PASSES it — the CHECK alone would leave the set with a silent third member.

THE ONE ASYMMETRY WORTH PINNING. "Absent means alert" and "omission preserves"
look contradictory and are not: they answer different questions, on INSERT and
on REASSIGN respectively, and both arms are tested below.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aiko_gateway.domain import devices_service, security, users_service
from sqlalchemy import select
from aiko_gateway.domain.models import DeviceToken, TokenKind
from aiko_gateway.rest import devices as device_routes
from aiko_gateway.rest.deps import get_session

VOIP_TOKEN = "v" * 64
ALERT_TOKEN = "a" * 64


async def _user(session, username: str):
    return await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")


@pytest_asyncio.fixture
async def client(session):
    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(device_routes.router)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _headers(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


# ------------------------------------------------------------------ the axis

def test_the_closed_set_is_exactly_alert_and_voip():
    """The VALUES ARE APPLE'S OWN `apns-push-type` SPELLINGS, so renaming a
    member silently changes the wire. Two independent records already say
    `token_kind`/`alert`/`voip` — design 12 Decision 2 and the app tab's design
    16 §5 — so this pins a cross-repo vocabulary, not a local preference."""
    assert [m.value for m in TokenKind] == ["alert", "voip"]


# -------------------------------------------------------- layer 1: the API edge

async def test_out_of_set_token_kind_is_422_at_the_boundary(client, session):
    """Enum-typed on the request model, so a bad value is a 422 and not a 500
    from the DB CHECK — the same two-layer discipline as `platform`."""
    alice = await _user(session, "alice")
    resp = await client.post(
        "/v1/devices", headers=_headers(alice),
        json={"platform": "apns", "token": ALERT_TOKEN, "token_kind": "shout"})
    assert resp.status_code == 422
    # AND NO ROW (Tesla, cage-match PR#170 r3). The sibling
    # test_register_rejects_unknown_platform_with_422 in tests/test_devices.py
    # already asserts this; the kind test forgot. 422-after-insert is the complete
    # silence: a row exists, no honest error reaches the client, and the later VoIP
    # path finds a token whose kind nothing validated.
    assert await devices_service.tokens_for_user(session, alice.id) == [], (
        "a rejected registration must leave no row — a 422 returned AFTER the "
        "insert is indistinguishable from a clean rejection at the boundary")


# ---------------------------------------------------------- layer 2: the CHECK

async def test_db_check_rejects_a_bad_token_kind_beyond_the_api(session):
    """The API is not the only writer — a migration, a script or an in-process
    caller reaches the table directly. The constraint is named so the failure
    says which invariant it was."""
    alice = await _user(session, "alice")
    session.add(DeviceToken(user_id=alice.id, platform="apns",
                            token=ALERT_TOKEN, token_kind="shout"))
    with pytest.raises(IntegrityError) as ei:
        await session.commit()
    assert "ck_device_tokens_token_kind" in str(ei.value)
    await session.rollback()


async def test_a_null_token_kind_is_rejected(session):
    """ITS OWN ARM, and not redundant with the CHECK: `NULL IN ('alert','voip')`
    evaluates to UNKNOWN, which a CHECK constraint PASSES. Without NOT NULL the
    closed set has a silent third member, which `plan_deliveries` would then skip
    as `unroutable_row` — a handset that stops ringing (the 0022 lesson).

    RAW SQL, NOT THE ORM. Passing `token_kind=None` to the model does NOT produce
    a NULL: SQLAlchemy omits a None-valued column from the INSERT and the
    server_default fills it in, so an ORM-based version of this test cannot
    create the state it exists to reject. Its positive control is below.
    """
    alice = await _user(session, "nullalice")
    with pytest.raises(Exception) as ei:
        await session.execute(
            text("INSERT INTO device_tokens "
                 "(id, user_id, platform, token, apns_environment, token_kind, "
                 " created_at, updated_at) VALUES "
                 "('01NULLKINDAAAAAAAAAAAAAAAA', :u, 'apns', 'nullkind', "
                 "'production', NULL, :t, :t)"),
            {"u": alice.id, "t": "2026-09-09T00:00:00+00:00"})
        await session.commit()
    assert "NOT NULL" in str(ei.value).upper()
    await session.rollback()


async def test_the_same_raw_insert_succeeds_with_a_real_kind(session):
    """THE POSITIVE CONTROL for the NULL arm. Without it, that test would pass
    against an INSERT that is malformed for some entirely different reason —
    a column list typo would read exactly like a NOT NULL enforcement."""
    bob = await _user(session, "nullbob")
    await session.execute(
        text("INSERT INTO device_tokens "
             "(id, user_id, platform, token, apns_environment, token_kind, "
             " created_at, updated_at) VALUES "
             "('01REALKINDAAAAAAAAAAAAAAAA', :u, 'apns', 'realkind', "
             "'production', 'voip', :t, :t)"),
        {"u": bob.id, "t": "2026-09-09T00:00:00+00:00"})
    await session.commit()
    row = await session.get(DeviceToken, "01REALKINDAAAAAAAAAAAAAAAA")
    assert row.token_kind == "voip"


# ------------------------------------------------- layer 3: the resolution rules

async def test_registration_without_token_kind_stores_alert(client, session):
    """ABSENT MEANS ALERT, on INSERT. This is what makes the migration
    backfill-free: `server_default='alert'` and this resolution are the same
    constant, so every existing row and every older client stays correct."""
    alice = await _user(session, "alice")
    resp = await client.post("/v1/devices", headers=_headers(alice),
                             json={"platform": "apns", "token": ALERT_TOKEN})
    assert resp.status_code == 201
    row = await session.get(DeviceToken, resp.json()["id"])
    assert row.token_kind == TokenKind.ALERT.value


async def test_a_declared_voip_registration_is_stored(client, session):
    """The positive control for the test above — without it, "absent stores
    alert" would pass for a service that can only ever store alert."""
    bob = await _user(session, "bob")
    resp = await client.post(
        "/v1/devices", headers=_headers(bob),
        json={"platform": "apns", "token": VOIP_TOKEN, "token_kind": "voip"})
    assert resp.status_code == 201
    row = await session.get(DeviceToken, resp.json()["id"])
    assert row.token_kind == TokenKind.VOIP.value


async def test_reregistration_without_token_kind_preserves_voip(client, session):
    """OMISSION PRESERVES, on REASSIGN — and the argument is STRONGER here than
    for `apns_environment`. "Same token string, kind changed" is not merely close
    to unreachable, it is impossible by construction: PushKit and UIKit mint from
    different registries and one string cannot be both. Whereas "a client stopped
    sending the field" (an app rollback to a pre-`token_kind` build) is an
    ordinary regression, and absent-means-alert here would silently downgrade a
    live VoIP row — which then sends an ALERT push to a VoIP token: 400
    DeviceTokenNotForTopic, REJECTED, no reap, no exception, ONE WARNING LINE,
    and no ring. This change's own failure mode, self-inflicted."""
    carol = await _user(session, "carol")
    first = await client.post(
        "/v1/devices", headers=_headers(carol),
        json={"platform": "apns", "token": VOIP_TOKEN, "token_kind": "voip"})
    assert first.status_code == 201

    again = await client.post("/v1/devices", headers=_headers(carol),
                              json={"platform": "apns", "token": VOIP_TOKEN})
    assert again.status_code == 201
    assert again.json()["token_kind"] == TokenKind.VOIP.value
    row = await session.get(DeviceToken, again.json()["id"])
    assert row.token_kind == TokenKind.VOIP.value, (
        "a re-registration without the field downgraded a live VoIP row to alert")


async def test_a_declared_kind_wins_on_reregistration(client, session):
    """DECLARATION WINS — the control for omission-preserves. A guard that never
    updated the stored kind would satisfy the test above perfectly."""
    dave = await _user(session, "dave")
    await client.post("/v1/devices", headers=_headers(dave),
                      json={"platform": "apns", "token": ALERT_TOKEN})
    resp = await client.post(
        "/v1/devices", headers=_headers(dave),
        json={"platform": "apns", "token": ALERT_TOKEN, "token_kind": "voip"})
    row = await session.get(DeviceToken, resp.json()["id"])
    assert row.token_kind == TokenKind.VOIP.value


# ------------------------------------------------------------- the wire echo

async def test_the_201_echoes_the_resolved_token_kind(client, session):
    """THE DESYNC DETECTOR. Measured before this shipped: `RegisterDeviceReq`
    SILENTLY DROPPED an unknown `token_kind` (pydantic's default
    `extra="ignore"`), so an app shipping the field against an un-deployed island
    got a 201, the field vanished, the row stored 'alert', and every VoIP push
    went to a token that is not a VoIP token. The echo is the only way the client
    learns it sent `voip` and got back `alert`."""
    erin = await _user(session, "erin")

    # THE LOAD-BEARING ARM, and it was missing (Tesla, cage-match PR#170). This
    # test asserted only the DEFAULT path — sent nothing, got 'alert' — which is
    # the one case the desync detector is NOT for. The 3am signature is "sent
    # voip, got alert" or "sent voip, got nothing", and neither was exercised
    # anywhere: the declared-voip test reads the ROW, not the 201 body. The
    # docstring above described a test that did not exist.
    voip = await client.post("/v1/devices", headers=_headers(erin),
                             json={"platform": "apns", "token": VOIP_TOKEN,
                                   "token_kind": "voip"})
    assert voip.status_code == 201
    body = voip.json()
    assert "token_kind" in body, (
        "the 201 MUST carry token_kind — its absence is precisely the signature "
        "of an island that predates this field, and the client fail-closes on it")
    assert body["token_kind"] == "voip", (
        f"sent voip, echoed {body['token_kind']!r} — this is the desync the echo "
        "exists to surface, and the app tab fail-closes its VoIP registration on it")

    # The default arm, kept: absent means alert is the wire contract.
    alert = await client.post("/v1/devices", headers=_headers(erin),
                              json={"platform": "apns", "token": ALERT_TOKEN})
    assert alert.json()["token_kind"] == "alert"


async def test_both_kinds_for_one_handset_coexist_as_two_rows(client, session):
    """PARTIAL IS THE DEFAULT (design 12 Decision 2). An alert token and a VoIP
    token for one phone are different strings, so `UNIQUE(token)` needs no change
    and both registrations stand independently."""
    frank = await _user(session, "frank")
    await client.post(
        "/v1/devices", headers=_headers(frank),
        json={"platform": "apns", "token": ALERT_TOKEN, "token_kind": "alert"})
    await client.post(
        "/v1/devices", headers=_headers(frank),
        json={"platform": "apns", "token": VOIP_TOKEN, "token_kind": "voip"})
    rows = await devices_service.tokens_for_user(session, frank.id)
    assert sorted(r.token_kind for r in rows) == ["alert", "voip"]


async def test_an_fcm_row_stores_whatever_kind_it_declared(client, session):
    """INERT FOR FCM, in the same shape as `apns_environment`'s inert-for-FCM
    paragraph. NO conditional CHECK and NO cross-field 422: that machinery was
    explicitly refused once already, it prevents nothing (the FCM branch never
    reads the kind), and a rejected registration is the most complete silence
    available — no row, no wake-time log, no reachability entry."""
    grace = await _user(session, "grace")
    resp = await client.post(
        "/v1/devices", headers=_headers(grace),
        json={"platform": "fcm", "token": "f" * 100, "token_kind": "voip"})
    assert resp.status_code == 201

    # ASSERT THE NAMED TARGET, not merely that the request was accepted (Carnot,
    # cage-match PR#170 r3). This asserted only the 201, so it stayed green whether
    # the route stored 'voip' or silently coerced to 'alert' — its outcome depended
    # on neither half of "may carry any kind" nor "nothing reads it". The first half
    # is now checked here; the second half is a claim about a send path that does
    # NOT EXIST on this branch, so it is stated as scope rather than tested, and the
    # test name no longer promises coverage this piece cannot provide.
    assert resp.json()["token_kind"] == "voip", (
        "an fcm row must store the kind it declared — the FCM transport does not "
        "read it, but coercing it here would be a silent rewrite of client-supplied "
        "data and would make the echo lie")
    row = (await session.execute(
        select(DeviceToken).where(DeviceToken.token == "f" * 100))).scalar_one()
    assert row.token_kind == "voip", "the stored row must match the echo"
