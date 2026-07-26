"""Moderator act-on-report subsystem (Piece B, #7) — take-down / dismiss / ban.

Three layers, mirroring test_moderation.py:
  * service tests call moderation_service directly and assert data effects
    (take-down soft-deletes + resolves; dismiss resolves without deleting; ban/
    unban toggle banned_at; self-ban and missing-target guards; queue shape).
  * authz tests drive the HTTP contract: a non-moderator gets 403 on every new
    endpoint, a configured moderator succeeds.
  * the ENFORCEMENT suite (load-bearing) proves a banned user is rejected at
    EVERY auth ingress — REST (get_current_user), token refresh, password login,
    and the WS handshake — RED-proving each by contrast with an active user.

App-under-test is built from just the auth + moderation routers (never `main`),
keeping the suite's "never import aiko_services" isolation invariant.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI, WebSocketDisconnect
from httpx import ASGITransport, AsyncClient

from aiko_gateway.config import settings
from aiko_gateway.domain import moderation_service, security, users_service
from aiko_gateway.domain.models import Channel, Message, MessageReport
from aiko_gateway.realtime import ws as ws_module
from aiko_gateway.realtime.hub import Connection, Hub
from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest import moderation as moderation_routes
from aiko_gateway.rest.deps import get_session


def _ulid(n: int) -> str:
    return f"{n:026d}"


async def _user(session, username: str, *, banned: bool = False):
    user = await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")
    if banned:
        user.banned_at = dt.datetime.now(dt.timezone.utc)
        await session.commit()
    return user


async def _channel(session, *, cid: int = 0, name: str = "general") -> Channel:
    ch = Channel(id=_ulid(cid), name=name, kind="standard", aiko_channel=name,
                 is_private=False)
    session.add(ch)
    await session.commit()
    return ch


async def _msg(session, *, mid: int, channel: Channel, sender) -> Message:
    msg = Message(
        id=_ulid(mid), channel_id=channel.id, sender_user_id=sender.id,
        sender_kind="human", sender_label=sender.display_name, body="bad words",
        aiko_origin=False, created_at=dt.datetime.now(dt.timezone.utc))
    session.add(msg)
    await session.commit()
    return msg


async def _report(session, *, rid: int, message: Message, reporter) -> MessageReport:
    row = MessageReport(
        id=_ulid(rid), message_id=message.id, reporter_user_id=reporter.id,
        reason="harassment", created_at=dt.datetime.now(dt.timezone.utc))
    session.add(row)
    await session.commit()
    return row


# --- service: take-down / dismiss -------------------------------------------

async def test_take_down_soft_deletes_and_resolves(session):
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    rep = await _report(session, rid=100, message=msg, reporter=author)

    await moderation_service.take_down_message(
        session, report_id=rep.id, moderator_id=mod.id)

    await session.refresh(msg)
    await session.refresh(rep)
    assert msg.deleted_at is not None          # message soft-deleted
    assert rep.resolved_at is not None
    assert rep.resolution == "taken_down"
    assert rep.resolved_by_user_id == mod.id


async def test_dismiss_resolves_without_deleting(session):
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    rep = await _report(session, rid=100, message=msg, reporter=author)

    await moderation_service.dismiss_report(
        session, report_id=rep.id, moderator_id=mod.id)

    await session.refresh(msg)
    await session.refresh(rep)
    assert msg.deleted_at is None              # message untouched
    assert rep.resolution == "dismissed"
    assert rep.resolved_by_user_id == mod.id


async def test_take_down_unknown_report_raises(session):
    with pytest.raises(moderation_service.ReportNotFound):
        await moderation_service.take_down_message(
            session, report_id=_ulid(999), moderator_id=_ulid(1))


async def test_take_down_after_dismiss_conflicts_and_leaves_message(session):
    """The cage-match HIGH finding: acting on an already-DISMISSED report must NOT
    soft-delete the message while leaving resolution='dismissed'. It raises
    ReportAlreadyResolved and the message stays intact."""
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    rep = await _report(session, rid=100, message=msg, reporter=author)

    await moderation_service.dismiss_report(session, report_id=rep.id, moderator_id=mod.id)
    with pytest.raises(moderation_service.ReportAlreadyResolved):
        await moderation_service.take_down_message(
            session, report_id=rep.id, moderator_id=mod.id)

    await session.refresh(msg)
    await session.refresh(rep)
    assert msg.deleted_at is None            # message NOT deleted
    assert rep.resolution == "dismissed"     # resolution unchanged — state agrees with label


async def test_dismiss_after_take_down_conflicts(session):
    """Symmetric: dismissing an already-taken-down report is refused."""
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    rep = await _report(session, rid=100, message=msg, reporter=author)

    await moderation_service.take_down_message(session, report_id=rep.id, moderator_id=mod.id)
    with pytest.raises(moderation_service.ReportAlreadyResolved):
        await moderation_service.dismiss_report(
            session, report_id=rep.id, moderator_id=mod.id)


async def test_resolve_after_dismiss_route_409(client, session, monkeypatch):
    """The conflict surfaces as HTTP 409 at the route (not a silent state/label split)."""
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    monkeypatch.setattr(settings, "moderator_user_ids", [mod.id])
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    rep = await _report(session, rid=100, message=msg, reporter=author)
    h = _auth(mod)
    assert (await client.post(f"/v1/reports/{rep.id}/dismiss", headers=h)).status_code == 204
    assert (await client.post(f"/v1/reports/{rep.id}/resolve", headers=h)).status_code == 409


async def test_take_down_is_idempotent(session):
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    rep = await _report(session, rid=100, message=msg, reporter=author)

    await moderation_service.take_down_message(session, report_id=rep.id, moderator_id=mod.id)
    await session.refresh(msg)
    first_deleted = msg.deleted_at
    # Second call keeps the first deleted_at + resolution (no re-stamp).
    await moderation_service.take_down_message(session, report_id=rep.id, moderator_id=mod.id)
    await session.refresh(msg)
    assert msg.deleted_at == first_deleted


# --- service: ban / unban ----------------------------------------------------

async def test_ban_sets_and_unban_clears_banned_at(session):
    mod = await _user(session, "mod")
    target = await _user(session, "target")
    assert users_service.is_banned(target) is False

    await moderation_service.ban_user(session, target_id=target.id, moderator_id=mod.id)
    await session.refresh(target)
    assert users_service.is_banned(target) is True

    await moderation_service.unban_user(session, target_id=target.id)
    await session.refresh(target)
    assert users_service.is_banned(target) is False


async def test_ban_self_raises(session):
    mod = await _user(session, "mod")
    with pytest.raises(moderation_service.CannotBanSelf):
        await moderation_service.ban_user(session, target_id=mod.id, moderator_id=mod.id)


async def test_ban_missing_target_raises(session):
    with pytest.raises(moderation_service.UserNotFound):
        await moderation_service.ban_user(
            session, target_id=_ulid(999), moderator_id=_ulid(1))


async def test_cannot_ban_a_configured_moderator(session, monkeypatch):
    """A configured moderator can't be banned (cage-match Carnot/Tesla): guarantees
    the moderator set stays fully unbanned — no lockout, no mod-bans-peer."""
    mod_a = await _user(session, "moda")
    mod_b = await _user(session, "modb")
    monkeypatch.setattr(settings, "moderator_user_ids", [mod_a.id, mod_b.id])
    with pytest.raises(moderation_service.CannotBanModerator):
        await moderation_service.ban_user(
            session, target_id=mod_b.id, moderator_id=mod_a.id)
    await session.refresh(mod_b)
    assert mod_b.banned_at is None


# --- service: queue + is_moderator ------------------------------------------

async def test_pending_queue_excludes_resolved_and_carries_preview(session):
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    ch = await _channel(session)
    m1 = await _msg(session, mid=1, channel=ch, sender=author)
    m2 = await _msg(session, mid=2, channel=ch, sender=author)
    r_open = await _report(session, rid=100, message=m1, reporter=author)
    r_done = await _report(session, rid=101, message=m2, reporter=author)
    await moderation_service.dismiss_report(session, report_id=r_done.id, moderator_id=mod.id)

    queue = await moderation_service.list_pending_reports(session)
    ids = {r["report_id"] for r in queue}
    assert r_open.id in ids and r_done.id not in ids   # resolved excluded
    row = next(r for r in queue if r["report_id"] == r_open.id)
    assert row["message_body"] == "bad words"          # privileged preview
    assert row["reporter_display_name"] == "Author"


def test_is_moderator_reads_config(monkeypatch):
    monkeypatch.setattr(settings, "moderator_user_ids", ["u-mod"])
    assert moderation_service.is_moderator("u-mod") is True
    assert moderation_service.is_moderator("u-other") is False


# --- HTTP: authz + enforcement ----------------------------------------------

@pytest_asyncio.fixture
async def client(session):
    """App from the auth + moderation routers, sharing the test session."""
    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(auth_routes.me_router)
    app.include_router(moderation_routes.router)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


async def test_non_moderator_gets_403_on_every_endpoint(client, session, monkeypatch):
    monkeypatch.setattr(settings, "moderator_user_ids", [])   # nobody is a mod
    plain = await _user(session, "plain")
    h = _auth(plain)
    assert (await client.get("/v1/reports", headers=h)).status_code == 403
    assert (await client.post(f"/v1/reports/{_ulid(1)}/resolve", headers=h)).status_code == 403
    assert (await client.post(f"/v1/reports/{_ulid(1)}/dismiss", headers=h)).status_code == 403
    assert (await client.post(f"/v1/users/{_ulid(2)}/ban", headers=h)).status_code == 403
    assert (await client.delete(f"/v1/users/{_ulid(2)}/ban", headers=h)).status_code == 403


async def test_moderator_can_list_and_act(client, session, monkeypatch):
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    monkeypatch.setattr(settings, "moderator_user_ids", [mod.id])
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    rep = await _report(session, rid=100, message=msg, reporter=author)
    h = _auth(mod)

    listed = await client.get("/v1/reports", headers=h)
    assert listed.status_code == 200
    assert listed.json()["reports"][0]["report_id"] == rep.id

    resolved = await client.post(f"/v1/reports/{rep.id}/resolve", headers=h)
    assert resolved.status_code == 204
    await session.refresh(msg)
    assert msg.deleted_at is not None


async def test_reports_status_query_alias_and_unknown(client, session, monkeypatch):
    """The queue is addressed as ?status=pending (wire name 'status', not the
    Python param 'status_'); an unsupported status is a 422, not a silent all-list."""
    mod = await _user(session, "mod")
    monkeypatch.setattr(settings, "moderator_user_ids", [mod.id])
    h = _auth(mod)
    assert (await client.get("/v1/reports?status=pending", headers=h)).status_code == 200
    assert (await client.get("/v1/reports?status=resolved", headers=h)).status_code == 422


async def test_ban_route_self_ban_400_and_missing_404(client, session, monkeypatch):
    mod = await _user(session, "mod")
    monkeypatch.setattr(settings, "moderator_user_ids", [mod.id])
    h = _auth(mod)
    assert (await client.post(f"/v1/users/{mod.id}/ban", headers=h)).status_code == 400
    assert (await client.post(f"/v1/users/{_ulid(777)}/ban", headers=h)).status_code == 404


async def test_me_exposes_is_moderator(client, session, monkeypatch):
    mod = await _user(session, "mod")
    plain = await _user(session, "plain")
    monkeypatch.setattr(settings, "moderator_user_ids", [mod.id])
    assert (await client.get("/v1/me", headers=_auth(mod))).json()["is_moderator"] is True
    assert (await client.get("/v1/me", headers=_auth(plain))).json()["is_moderator"] is False


# --- ENFORCEMENT: a ban bites at every ingress (load-bearing) ---------------

async def test_banned_user_rejected_on_rest(client, session):
    """REST ingress (get_current_user): a banned user is 403 even holding a valid
    access token; an active user passes the same route."""
    active = await _user(session, "active")
    banned = await _user(session, "banned", banned=True)
    assert (await client.get("/v1/blocks", headers=_auth(active))).status_code == 200
    assert (await client.get("/v1/blocks", headers=_auth(banned))).status_code == 403


async def test_banned_user_refused_refresh(client, session):
    """Refresh ingress: a banned user cannot mint a fresh access token off a valid
    refresh token; an active user can."""
    active = await _user(session, "active")
    banned = await _user(session, "banned", banned=True)
    ok = await client.post("/v1/auth/refresh",
                           json={"refresh_token": security.issue_refresh(active.id)})
    assert ok.status_code == 200
    bad = await client.post("/v1/auth/refresh",
                            json={"refresh_token": security.issue_refresh(banned.id)})
    assert bad.status_code == 403


async def test_banned_user_refused_login(client, session):
    """Login ingress: a banned user with correct credentials is refused a session."""
    banned = await _user(session, "banned", banned=True)
    resp = await client.post("/v1/auth/login",
                             json={"username": "banned", "password": "pw"})
    assert resp.status_code == 403


async def _run_handshake(session, monkeypatch, user):
    """Drive ws_endpoint against a stub socket, returning (accepted, close_code)."""
    class _CM:
        async def __aenter__(self): return session
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(ws_module, "SessionLocal", lambda: _CM())

    class _StubWS:
        def __init__(self, token):
            self.query_params = {"token": token}
            self.app = SimpleNamespace(state=SimpleNamespace(gw=SimpleNamespace(hub=Hub())))
            self.closed_code = None
            self.accepted = False
        async def close(self, code=1000): self.closed_code = code
        async def accept(self): self.accepted = True
        async def receive_json(self): raise WebSocketDisconnect()  # exit loop cleanly

    stub = _StubWS(security.issue_access(user.id))
    await ws_module.ws_endpoint(stub)
    return stub.accepted, stub.closed_code


async def test_banned_user_closed_on_ws_handshake(session, monkeypatch):
    """WS ingress: a banned user's handshake is closed 1008 before accept; an
    active user's handshake is accepted."""
    active = await _user(session, "active")
    banned = await _user(session, "banned", banned=True)

    accepted, code = await _run_handshake(session, monkeypatch, banned)
    assert accepted is False and code == 1008

    accepted, code = await _run_handshake(session, monkeypatch, active)
    assert accepted is True and code is None


# --- hub: active-disconnect --------------------------------------------------

async def test_hub_disconnect_user_closes_and_unregisters():
    hub = Hub()

    class _WS:
        def __init__(self): self.closed = None
        async def close(self, code=1000): self.closed = code

    ws_a1, ws_a2, ws_b = _WS(), _WS(), _WS()
    hub.register(Connection(ws_a1, "user-a"))
    hub.register(Connection(ws_a2, "user-a"))   # two sockets, same user
    hub.register(Connection(ws_b, "user-b"))

    dropped = await hub.disconnect_user("user-a")
    assert dropped == 2
    assert ws_a1.closed == 1008 and ws_a2.closed == 1008
    assert ws_b.closed is None                  # other users untouched
    # A now has no live connections; disconnecting again drops zero.
    assert await hub.disconnect_user("user-a") == 0
