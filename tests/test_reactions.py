"""Acceptance tests for emoji reactions (#2634, v2 social layer).

Two layers, mirroring the codebase's split:
  * SERVICE — idempotency, viewer-dependent aggregation, validation, the
    account-deletion purge, folded through the ONE door (reactions_service).
  * ROUTE — auth, the existence-hiding visibility gate (soft-delete + channel ACL
    + block), the discrete `reaction` WS fanout, and the viewer-dependent
    reactions[] on the history read.

Built from JUST the reaction + message routers (never `aiko_gateway.main`, which
imports the aiko bus — the suite's "never import aiko_services" isolation
invariant). The DB `get_session` dependency is overridden to the in-memory test
session so the endpoint and the auth dependency share one DB; a real `Hub` is
wired onto `app.state.gw` so the fanout is exercised, not mocked.

Contract under test (mirrors HANDOFF-from-app-tab-v2-social-wire, task #2634):
  - reaction is STATE not event — no forward-ULID row; the aggregate is recomputed
    on every history read (a live `reaction` frame is a best-effort delta over it)
  - `count` + `reacted_by_me` per emoji is the whole read shape (no reactors list)
  - add/remove idempotent; emoji opaque + length-bounded
  - you may only react to a message you can SEE (same predicate as history)
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiko_gateway.domain import (
    accounts_service, messages_service, moderation_service, reactions_service,
    security, users_service)
from aiko_gateway.domain.models import Channel, Membership, Message
from aiko_gateway.realtime.hub import Connection, Hub
from aiko_gateway.rest import messages as message_routes
from aiko_gateway.rest import reactions as reaction_routes
from aiko_gateway.rest.deps import get_session
from aiko_gateway.rest.errors import register_error_handlers

THUMB = "\U0001f44d"   # 👍
HEART = "❤️"  # ❤️


def _ulid(n: int) -> str:
    return f"{n:026d}"


async def _user(session, username: str):
    return await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")


async def _channel(session, *, cid: int = 0, name: str = "general",
                   private: bool = False) -> Channel:
    ch = Channel(id=_ulid(cid), name=name, kind="standard", aiko_channel=name,
                 is_private=private)
    session.add(ch)
    await session.commit()
    return ch


async def _msg(session, *, mid: int, channel: Channel, sender,
               deleted: bool = False) -> Message:
    msg = Message(
        id=_ulid(mid), channel_id=channel.id, sender_user_id=sender.id,
        sender_kind="human", sender_label=sender.display_name, body="hi",
        aiko_origin=False, created_at=dt.datetime.now(dt.timezone.utc),
        deleted_at=dt.datetime.now(dt.timezone.utc) if deleted else None)
    session.add(msg)
    await session.commit()
    return msg


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


# ============================ SERVICE LAYER ================================

async def test_add_is_idempotent(session):
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    c1 = await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    c2 = await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    assert c1 == 1
    assert c2 == 1  # re-add is a no-op, not a second row


async def test_distinct_emoji_are_distinct_rows(session):
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    heart = await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=HEART)
    assert heart == 1  # a different emoji from the same user is its own reaction


async def test_two_users_same_emoji_counts_two(session):
    author = await _user(session, "author")
    a = await _user(session, "aaa")
    b = await _user(session, "bbb")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    await reactions_service.add_reaction(
        session, user_id=a.id, message_id=msg.id, emoji=THUMB)
    count = await reactions_service.add_reaction(
        session, user_id=b.id, message_id=msg.id, emoji=THUMB)
    assert count == 2


async def test_remove_is_idempotent(session):
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    c1 = await reactions_service.remove_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    c2 = await reactions_service.remove_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    assert c1 == 0
    assert c2 == 0  # removing an absent reaction is not an error


async def test_aggregate_is_viewer_dependent_and_ordered(session):
    author = await _user(session, "author")
    me = await _user(session, "me")
    other = await _user(session, "other")
    third = await _user(session, "third")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    # THUMB: me + other + third (count 3, mine). HEART: other only (count 1, not mine).
    for u in (me, other, third):
        await reactions_service.add_reaction(
            session, user_id=u.id, message_id=msg.id, emoji=THUMB)
    await reactions_service.add_reaction(
        session, user_id=other.id, message_id=msg.id, emoji=HEART)

    agg = await reactions_service.aggregate_for_messages(session, [msg.id], me.id)
    entries = agg[msg.id]
    # Ordered most-reacted first (-count), so THUMB (3) precedes HEART (1).
    assert [e["emoji"] for e in entries] == [THUMB, HEART]
    assert entries[0] == {"emoji": THUMB, "count": 3, "reacted_by_me": True}
    assert entries[1] == {"emoji": HEART, "count": 1, "reacted_by_me": False}

    # Same rows, DIFFERENT viewer — reacted_by_me flips where appropriate.
    agg_other = await reactions_service.aggregate_for_messages(
        session, [msg.id], other.id)
    by_emoji = {e["emoji"]: e for e in agg_other[msg.id]}
    assert by_emoji[THUMB]["reacted_by_me"] is True
    assert by_emoji[HEART]["reacted_by_me"] is True


async def test_aggregate_absent_for_unreacted_message(session):
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    agg = await reactions_service.aggregate_for_messages(session, [msg.id], author.id)
    assert agg == {}  # no entry → serializer falls back to []


async def test_aggregate_empty_ids_short_circuits(session):
    assert await reactions_service.aggregate_for_messages(session, [], "whoever") == {}


@pytest.mark.parametrize("bad", ["", "   ", "x" * 65])
async def test_validate_emoji_rejects(bad):
    with pytest.raises(reactions_service.InvalidEmoji):
        reactions_service.validate_emoji(bad)


async def test_validate_emoji_accepts_zwj_sequence():
    family = "\U0001f468‍\U0001f469‍\U0001f467"  # 👨‍👩‍👧
    assert reactions_service.validate_emoji(family) == family


async def test_message_view_defaults_reactions_empty(session):
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    view = messages_service.message_view(msg)
    assert view["reactions"] == []


async def test_purge_user_reactions(session):
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)

    await reactions_service.purge_user_reactions(session, reactor.id)
    await session.commit()
    agg = await reactions_service.aggregate_for_messages(session, [msg.id], author.id)
    assert agg == {}


# ============================ ROUTE LAYER =================================

class _RecordingConn(Connection):
    """A Connection that records frames instead of touching a socket."""

    def __init__(self, user_id: str):
        self.ws = None  # type: ignore[assignment]
        self.user_id = user_id
        self.subscribed: set[str] = set()
        self.sent: list[dict] = []

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)


@pytest_asyncio.fixture
async def app_ctx(session):
    """Minimal app: reaction + message routers, a real Hub on app.state.gw, the DB
    override. Returns (client, hub)."""
    hub = Hub()

    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(reaction_routes.router)
    app.include_router(message_routes.router)
    register_error_handlers(app)
    app.state.gw = SimpleNamespace(hub=hub)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, hub
    app.dependency_overrides.clear()


async def test_post_requires_auth(app_ctx):
    client, _ = app_ctx
    resp = await client.post(f"/v1/messages/{_ulid(1)}/reactions",
                             json={"emoji": THUMB})
    assert resp.status_code == 401


async def test_post_adds_and_fans_out(app_ctx, session):
    client, hub = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    watcher = _RecordingConn(author.id)
    watcher.subscribed.add(ch.id)
    hub.register(watcher)

    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 200
    assert resp.json() == {"msg_id": msg.id, "emoji": THUMB, "count": 1,
                           "reacted_by_me": True}
    frames = [f for f in watcher.sent if f.get("type") == "reaction"]
    assert len(frames) == 1
    assert frames[0] == {"type": "reaction", "channel_id": ch.id, "msg_id": msg.id,
                         "emoji": THUMB, "action": "add", "user_id": reactor.id,
                         "count": 1}


async def test_post_is_idempotent_over_http(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    await client.post(f"/v1/messages/{msg.id}/reactions",
                      json={"emoji": THUMB}, headers=_auth(reactor))
    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 200
    assert resp.json()["count"] == 1  # still one


async def test_post_invalid_emoji_422(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": "   "}, headers=_auth(reactor))
    assert resp.status_code == 422


async def test_post_to_missing_message_404(app_ctx, session):
    client, _ = app_ctx
    reactor = await _user(session, "reactor")
    resp = await client.post(f"/v1/messages/{_ulid(999)}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 404


async def test_post_to_soft_deleted_message_404(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author, deleted=True)
    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 404  # can't react to a taken-down message


async def test_post_to_private_channel_non_member_404(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    outsider = await _user(session, "outsider")
    ch = await _channel(session, private=True)
    # author is a member (can post); outsider is not.
    session.add(Membership(channel_id=ch.id, user_id=author.id, role="member"))
    await session.commit()
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(outsider))
    assert resp.status_code == 404  # existence-hiding: same 404 as non-existent


async def test_post_to_blocked_authors_message_404(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    # reactor blocks author → author's message is hidden from reactor → un-reactable.
    await moderation_service.block_user(session, reactor.id, author.id)
    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 404


async def test_delete_removes_and_fans_out(app_ctx, session):
    client, hub = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)

    watcher = _RecordingConn(author.id)
    watcher.subscribed.add(ch.id)
    hub.register(watcher)

    resp = await client.delete(
        f"/v1/messages/{msg.id}/reactions/{THUMB}", headers=_auth(reactor))
    assert resp.status_code == 200
    assert resp.json() == {"msg_id": msg.id, "emoji": THUMB, "count": 0,
                           "reacted_by_me": False}
    frames = [f for f in watcher.sent if f.get("type") == "reaction"]
    assert frames[0]["action"] == "remove"
    assert frames[0]["count"] == 0


async def test_fanout_excludes_reactor_block_pairs(app_ctx, session):
    """A subscriber the reactor has blocked does NOT receive the reaction frame — the
    live twin of the history visibility filter (mirrors the send path)."""
    client, hub = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await moderation_service.block_user(session, reactor.id, author.id)

    watcher = _RecordingConn(author.id)  # author is blocked by the reactor
    watcher.subscribed.add(ch.id)
    hub.register(watcher)

    # reactor reacts to their OWN-visible message (a third party's would 404); use a
    # self-authored message so the block gate on the resolve doesn't 404 the reactor.
    own = await _msg(session, mid=2, channel=ch, sender=reactor)
    resp = await client.post(f"/v1/messages/{own.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 200
    # author (blocked by reactor) must NOT see the reactor's reaction frame.
    assert [f for f in watcher.sent if f.get("type") == "reaction"] == []


async def test_history_reflects_reactions_viewer_dependently(app_ctx, session):
    """The history read carries the viewer-dependent reactions[] on each message —
    reacted_by_me is TRUE for the reactor, FALSE for another reader."""
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await client.post(f"/v1/messages/{msg.id}/reactions",
                      json={"emoji": THUMB}, headers=_auth(reactor))

    # The reactor sees reacted_by_me = True.
    resp = await client.get(
        f"/v1/channels/{ch.id}/messages", headers=_auth(reactor))
    item = next(m for m in resp.json()["messages"] if m["msg_id"] == msg.id)
    assert item["reactions"] == [{"emoji": THUMB, "count": 1, "reacted_by_me": True}]

    # Another reader sees the same count but reacted_by_me = False.
    resp2 = await client.get(
        f"/v1/channels/{ch.id}/messages", headers=_auth(author))
    item2 = next(m for m in resp2.json()["messages"] if m["msg_id"] == msg.id)
    assert item2["reactions"] == [{"emoji": THUMB, "count": 1, "reacted_by_me": False}]


async def test_account_deletion_purges_reactions(session):
    """A deleted user's reactions vanish (the cascade purge), and a message they
    reacted to loses that count — proven end-to-end through delete_user_account."""
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)

    await accounts_service.delete_user_account(session, reactor.id)

    agg = await reactions_service.aggregate_for_messages(session, [msg.id], author.id)
    assert agg == {}
