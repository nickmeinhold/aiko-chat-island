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

from sqlalchemy import select

from aiko_gateway.config import settings
from aiko_gateway.domain import moderation_service, security, users_service
from aiko_gateway.domain.models import Channel, Message, MessageReport, Retraction
from aiko_gateway.realtime import ws as ws_module
from aiko_gateway.realtime.hub import Connection, Hub
from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest import moderation as moderation_routes
from aiko_gateway.rest.deps import get_session
from aiko_gateway.rest.errors import register_error_handlers


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


# --- service: takedown retraction emission (#7 propagation) ------------------

async def _all_retractions(session) -> list[Retraction]:
    return list((await session.execute(select(Retraction))).scalars())


async def test_take_down_emits_forward_retraction(session):
    """The takedown transition appends a forward-ULID retraction in the same txn,
    and returns it. The retraction targets the message, is scoped to its channel,
    and its id sorts ABOVE the target's id so a client's forward cursor carries it."""
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    rep = await _report(session, rid=100, message=msg, reporter=author)

    retraction = await moderation_service.take_down_message(
        session, report_id=rep.id, moderator_id=mod.id)

    assert retraction is not None
    assert retraction.target_msg_id == msg.id
    assert retraction.channel_id == ch.id
    assert retraction.id > msg.id                     # forward cursor carries it
    # Persisted (durable system of record), exactly one, committed with the delete.
    rows = await _all_retractions(session)
    assert [r.id for r in rows] == [retraction.id]


async def test_idempotent_re_resolve_emits_no_second_retraction(session):
    """A re-resolve of an already-taken-down report returns None and does NOT mint a
    second retraction — clients already received the first."""
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    rep = await _report(session, rid=100, message=msg, reporter=author)

    first = await moderation_service.take_down_message(
        session, report_id=rep.id, moderator_id=mod.id)
    second = await moderation_service.take_down_message(
        session, report_id=rep.id, moderator_id=mod.id)

    assert first is not None and second is None
    assert [r.id for r in await _all_retractions(session)] == [first.id]  # still one


async def test_take_down_of_already_soft_deleted_message_still_emits_retraction(session):
    """A message soft-deleted by a PRIOR path (e.g. account-deletion husk) can still
    be taken down; the re-delete is skipped but the retraction MUST still emit — a
    client may still hold the pre-takedown row and needs the removal signal."""
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    msg.deleted_at = dt.datetime.now(dt.timezone.utc)   # already soft-deleted
    await session.commit()
    rep = await _report(session, rid=100, message=msg, reporter=author)

    retraction = await moderation_service.take_down_message(
        session, report_id=rep.id, moderator_id=mod.id)

    assert retraction is not None
    assert retraction.target_msg_id == msg.id


# --- HTTP: resolve route fans the retraction out live -----------------------

class _RecordingConn(Connection):
    """A Connection that records frames instead of touching a socket."""

    def __init__(self, user_id: str):
        self.ws = None  # type: ignore[assignment]
        self.user_id = user_id
        self.subscribed: set[str] = set()
        self.sent: list[dict] = []

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)


async def test_resolve_route_fans_out_retraction_frame(session, monkeypatch):
    """The resolve route delivers a live `retraction` frame to a subscriber of the
    channel — the live twin of the history catch-up. Best-effort layer over the
    durable Retraction row."""
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    monkeypatch.setattr(settings, "moderator_user_ids", [mod.id])
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    rep = await _report(session, rid=100, message=msg, reporter=author)

    hub = Hub()
    watcher = _RecordingConn(author.id)
    watcher.subscribed.add(ch.id)
    hub.register(watcher)

    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(moderation_routes.router)
    register_error_handlers(app)
    app.state.gw = SimpleNamespace(hub=hub)   # what the route reaches for the fanout
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(f"/v1/reports/{rep.id}/resolve", headers=_auth(mod))
    app.dependency_overrides.clear()

    assert resp.status_code == 204
    frames = [f for f in watcher.sent if f.get("type") == "retraction"]
    assert len(frames) == 1
    assert frames[0]["channel_id"] == ch.id
    assert frames[0]["target_msg_id"] == msg.id
    assert frames[0]["id"] > msg.id


async def test_second_report_on_same_message_emits_no_duplicate_retraction(session):
    """At most ONE retraction per taken-down message (cage-match Tesla + Carnot). Two
    DISTINCT reports on the same message, each resolved: the first takedown emits, the
    second (message already retracted) returns None — no duplicate — yet the second
    report is still properly resolved."""
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    r2 = await _user(session, "reporter2")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    rep1 = await _report(session, rid=100, message=msg, reporter=author)
    rep2 = await _report(session, rid=101, message=msg, reporter=r2)

    first = await moderation_service.take_down_message(
        session, report_id=rep1.id, moderator_id=mod.id)
    second = await moderation_service.take_down_message(
        session, report_id=rep2.id, moderator_id=mod.id)

    assert first is not None and second is None
    assert [r.id for r in await _all_retractions(session)] == [first.id]  # exactly one
    await session.refresh(rep2)
    assert rep2.resolution == "taken_down"  # dedup didn't block rep2's resolution


async def test_re_resolve_heals_a_takedown_missing_its_retraction(session):
    """A message taken down BEFORE retractions existed (pre-0016, or any takedown whose
    retraction was never minted) has deleted_at set + a taken_down report but no
    retraction row — the exact watermark gap this PR closes, left open for old data.
    Re-resolving must SELF-HEAL (emit the missing retraction), not dead-end on the
    idempotent return (cage-match Carnot)."""
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    rep = await _report(session, rid=100, message=msg, reporter=author)
    # Simulate a pre-retraction takedown: soft-deleted + resolved taken_down, NO retraction.
    msg.deleted_at = dt.datetime.now(dt.timezone.utc)
    rep.resolved_at = dt.datetime.now(dt.timezone.utc)
    rep.resolution = "taken_down"
    rep.resolved_by_user_id = mod.id
    await session.commit()
    assert await _all_retractions(session) == []            # the stranded state

    healed = await moderation_service.take_down_message(
        session, report_id=rep.id, moderator_id=mod.id)
    assert healed is not None                                # emitted the missing retraction
    assert healed.target_msg_id == msg.id
    assert [r.id for r in await _all_retractions(session)] == [healed.id]
    # A second re-resolve is now a genuine no-op (retraction already exists).
    assert await moderation_service.take_down_message(
        session, report_id=rep.id, moderator_id=mod.id) is None


async def test_take_down_missing_message_fails_closed(session):
    """A report whose target message row is absent fails closed with MessageNotFound
    rather than stamping a taken_down label for a non-transition (cage-match Carnot
    LOW). FK-off in tests lets us seed the orphan directly."""
    mod = await _user(session, "mod")
    reporter = await _user(session, "reporter")
    orphan = MessageReport(
        id=_ulid(200), message_id=_ulid(777), reporter_user_id=reporter.id,
        reason="harassment", created_at=dt.datetime.now(dt.timezone.utc))
    session.add(orphan)
    await session.commit()

    with pytest.raises(moderation_service.MessageNotFound):
        await moderation_service.take_down_message(
            session, report_id=orphan.id, moderator_id=mod.id)

    assert await _all_retractions(session) == []   # nothing emitted
    await session.refresh(orphan)
    assert orphan.resolved_at is None              # NOT stamped resolved


async def test_resolve_route_maps_ordering_violation_to_500(client, session, monkeypatch):
    """The (unreachable) ordering guard surfaces as an observable 500 at the route,
    not an opaque stack trace (cage-match Tesla + Wu). Fail-closed: report unresolved."""
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    monkeypatch.setattr(settings, "moderator_user_ids", [mod.id])
    ch = await _channel(session)
    msg = await _msg(session, mid=5, channel=ch, sender=author)
    rep = await _report(session, rid=100, message=msg, reporter=author)
    monkeypatch.setattr(moderation_service, "new_ulid", lambda: _ulid(1))  # < target(5)

    resp = await client.post(f"/v1/reports/{rep.id}/resolve", headers=_auth(mod))
    assert resp.status_code == 500


async def test_ordering_violation_fails_clean_with_no_partial_mutation(session, monkeypatch):
    """cage-match Wu: the retraction ordering guard must fail CLEAN — if it fires it
    must leave NO partial mutation, so a LATER commit on the same session cannot
    persist a `taken_down` resolution with no retraction (the exact un-catch-uppable
    split the PR exists to prevent). RED-proves the validate-before-mutate ordering:
    force new_ulid() below the target, then commit and assert nothing stuck."""
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=5, channel=ch, sender=author)
    rep = await _report(session, rid=100, message=msg, reporter=author)
    monkeypatch.setattr(moderation_service, "new_ulid", lambda: _ulid(1))  # id < target(5)

    with pytest.raises(moderation_service.RetractionOrderingError):
        await moderation_service.take_down_message(
            session, report_id=rep.id, moderator_id=mod.id)

    # Force the "later commit" the dirty-session harm depends on. Clean code has
    # nothing pending, so this persists nothing.
    await session.commit()
    await session.refresh(msg)
    await session.refresh(rep)
    assert msg.deleted_at is None                    # NOT soft-deleted
    assert rep.resolved_at is None                   # NOT stamped taken_down
    assert await _all_retractions(session) == []


async def test_resolve_route_fans_retraction_to_blocked_subscriber_too(
        session, monkeypatch):
    """#7 ADD/REMOVE asymmetry: the live retraction fanout is NOT block-filtered — a
    subscriber blocked with the taken-down message's author STILL receives the frame
    (a delete carries no content, only removes). Contrast the message fanout, which IS
    block-filtered. Delivering the delete to a blocked viewer removes stale content
    they may hold and leaks nothing (opaque id)."""
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    blocker = await _user(session, "blocker")
    monkeypatch.setattr(settings, "moderator_user_ids", [mod.id])
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    rep = await _report(session, rid=100, message=msg, reporter=author)
    await moderation_service.block_user(session, blocker.id, author.id)

    hub = Hub()
    blocked_conn = _RecordingConn(blocker.id); blocked_conn.subscribed.add(ch.id)
    hub.register(blocked_conn)

    async def _override_session():
        yield session
    app = FastAPI()
    app.include_router(moderation_routes.router)
    register_error_handlers(app)
    app.state.gw = SimpleNamespace(hub=hub)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(f"/v1/reports/{rep.id}/resolve", headers=_auth(mod))
    app.dependency_overrides.clear()

    assert resp.status_code == 204
    # Blocked subscriber DOES receive the retraction (delete is not block-filtered).
    assert len([f for f in blocked_conn.sent if f.get("type") == "retraction"]) == 1


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
    register_error_handlers(app)  # structured ban-403 body (mirrors main)
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


async def test_banned_user_refused_social_login(session):
    """Social single door (_resolve_identity — covers native /social AND broker
    /callback): a banned social user is refused a session; an active one gets tokens."""
    from fastapi import HTTPException

    from aiko_gateway.domain.oauth import VerifiedIdentity
    from aiko_gateway.rest.auth import _resolve_identity

    await users_service.create_social_user(
        session, provider="google", provider_sub="sub-active",
        handle="socialactive", display_name="Active")
    banned = await users_service.create_social_user(
        session, provider="google", provider_sub="sub-banned",
        handle="socialbanned", display_name="Banned")
    banned.banned_at = dt.datetime.now(dt.timezone.utc)
    await session.commit()

    ok = await _resolve_identity(session, VerifiedIdentity(
        provider="google", sub="sub-active", email=None, suggested_name=None))
    assert "access_token" in ok
    with pytest.raises(HTTPException) as ei:
        await _resolve_identity(session, VerifiedIdentity(
            provider="google", sub="sub-banned", email=None, suggested_name=None))
    assert ei.value.status_code == 403


async def test_reports_limit_validated_and_capped(client, session, monkeypatch):
    """The queue exposes a validated `limit` (1..500) so a backlog past the default
    can't hide the oldest reports; out-of-range is a 422 (cage-match Carnot)."""
    mod = await _user(session, "mod")
    author = await _user(session, "author")
    monkeypatch.setattr(settings, "moderator_user_ids", [mod.id])
    ch = await _channel(session)
    for i in range(3):
        m = await _msg(session, mid=i + 1, channel=ch, sender=author)
        await _report(session, rid=100 + i, message=m, reporter=author)
    h = _auth(mod)
    assert len((await client.get("/v1/reports?limit=2", headers=h)).json()["reports"]) == 2
    assert (await client.get("/v1/reports?limit=0", headers=h)).status_code == 422
    assert (await client.get("/v1/reports?limit=999", headers=h)).status_code == 422


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


# --- ban 403 carries a machine-stable error code (app §2 ask, #30) ----------

_SUSPENDED_BODY = {"error": "account_suspended", "detail": "account suspended"}


async def test_ban_403_carries_machine_stable_error_code(client, session):
    """The ban 403 body carries a top-level `error` code at BOTH REST ingresses,
    so a client branches on the code not the human-readable prose (which we're
    free to retune). `detail` is preserved verbatim — additive, non-breaking for
    the existing prose match. RED-proven against the two distinct ingress paths:
    authed route (get_current_user / deps.py) and login-mint (_deny_if_banned /
    auth.py, reached here via refresh)."""
    banned = await _user(session, "banned", banned=True)

    # ingress 1: authed route (get_current_user)
    r = await client.get("/v1/blocks", headers=_auth(banned))
    assert r.status_code == 403
    assert r.json() == _SUSPENDED_BODY

    # ingress 2: login/mint door (_deny_if_banned), reached via refresh
    r2 = await client.post(
        "/v1/auth/refresh",
        json={"refresh_token": security.issue_refresh(banned.id)})
    assert r2.status_code == 403
    assert r2.json() == _SUSPENDED_BODY
