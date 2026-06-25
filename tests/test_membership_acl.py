"""I2 membership ACL (#36) — the read/subscribe/post trust boundary.

Semantics under test:
  * PUBLIC channels (is_private=False): open to every authenticated user.
  * PRIVATE channels (is_private=True): require an explicit Membership row to
    read/subscribe; posting additionally needs Membership.can_post.
  * EXISTENCE-HIDING: a non-member of a private channel gets the SAME response as
    for a non-existent channel (REST 404 / WS no_channel) — never a signal that
    the channel exists.

These assert the BOUNDARY (who can see/post what), enforced at every server-side
seam: REST list, REST history, WS subscribe, WS send. The app-under-test is built
from JUST the read routers (never `main`) to keep the suite's "never import
aiko_services" isolation invariant — same pattern as test_rest_auth.
"""
from __future__ import annotations

import datetime as dt

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiko_gateway.domain import acl, messages_service, security, users_service
from aiko_gateway.domain.models import Channel, Membership, Message
from aiko_gateway.realtime.hub import Connection
from aiko_gateway.realtime.ws import _handle_send, _handle_subscribe
from aiko_gateway.rest import channels as channel_routes
from aiko_gateway.rest import messages as message_routes
from aiko_gateway.rest.deps import get_session


def _ulid(n: int) -> str:
    return f"{n:026d}"


def _now() -> dt.datetime:
    return dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc)


async def _user(session, username: str):
    return await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")


async def _public_channel(session, *, cid: int = 0, name: str = "general") -> Channel:
    ch = Channel(id=_ulid(cid), name=name, kind="standard", aiko_channel=name,
                 is_private=False)
    session.add(ch)
    await session.commit()
    return ch


async def _private_channel(session, *, cid: int = 10, name: str = "secret") -> Channel:
    ch = Channel(id=_ulid(cid), name=name, kind="standard", aiko_channel=name,
                 is_private=True)
    session.add(ch)
    await session.commit()
    return ch


async def _join(session, channel: Channel, user, *, can_post: bool = True) -> None:
    session.add(Membership(channel_id=channel.id, user_id=user.id, can_post=can_post))
    await session.commit()


# =========================================================================
# ACL unit layer — the single source of truth the call sites delegate to
# =========================================================================

async def test_readable_channel_public_open_to_any_user(session):
    ch = await _public_channel(session)
    alice = await _user(session, "alice")
    got = await acl.readable_channel(session, alice.id, ch.id)
    assert got is not None and got.id == ch.id


async def test_readable_channel_private_requires_membership(session):
    ch = await _private_channel(session)
    alice = await _user(session, "alice")
    bob = await _user(session, "bob")
    await _join(session, ch, alice)
    assert (await acl.readable_channel(session, alice.id, ch.id)) is not None
    assert (await acl.readable_channel(session, bob.id, ch.id)) is None


async def test_readable_channel_missing_and_private_denied_both_none(session):
    """Existence-hiding at the query level: a missing channel and a private one
    the user is not in both return None — the SAME single-query result, so no
    timing/query-shape oracle distinguishes them (Carnot, cage-match PR #8)."""
    priv = await _private_channel(session, cid=10, name="secret")
    bob = await _user(session, "bob")
    assert (await acl.readable_channel(session, bob.id, priv.id)) is None
    assert (await acl.readable_channel(session, bob.id, "does-not-exist")) is None


async def test_can_post_honours_can_post_flag_on_private(session):
    ch = await _private_channel(session)
    poster = await _user(session, "poster")
    muted = await _user(session, "muted")
    outsider = await _user(session, "outsider")
    await _join(session, ch, poster, can_post=True)
    await _join(session, ch, muted, can_post=False)
    assert await acl.can_post(session, poster.id, ch) is True
    assert await acl.can_post(session, muted.id, ch) is False
    assert await acl.can_post(session, outsider.id, ch) is False


async def test_can_post_public_open_to_all(session):
    ch = await _public_channel(session)
    anyone = await _user(session, "anyone")
    assert await acl.can_post(session, anyone.id, ch) is True


async def test_can_post_public_non_member_still_open(session):
    """Absence of a Membership row on a public channel preserves the open default."""
    ch = await _public_channel(session)
    stranger = await _user(session, "stranger")
    # No _join — no Membership row at all.
    assert await acl.can_post(session, stranger.id, ch) is True


async def test_can_post_public_member_with_can_post_true_is_allowed(session):
    """An explicit Membership row with can_post=True does not accidentally mute."""
    ch = await _public_channel(session)
    alice = await _user(session, "alice")
    await _join(session, ch, alice, can_post=True)
    assert await acl.can_post(session, alice.id, ch) is True


async def test_can_post_public_member_with_can_post_false_is_forbidden(session):
    """An explicit Membership row with can_post=False mutes a user on a public channel."""
    ch = await _public_channel(session)
    muted = await _user(session, "muted")
    await _join(session, ch, muted, can_post=False)
    assert await acl.can_post(session, muted.id, ch) is False


async def test_visible_channels_is_public_plus_member_private_ordered(session):
    pub = await _public_channel(session, cid=0, name="general")
    priv_in = await _private_channel(session, cid=10, name="club")
    priv_out = await _private_channel(session, cid=20, name="vault")
    alice = await _user(session, "alice")
    await _join(session, priv_in, alice)
    visible = await acl.visible_channels(session, alice.id)
    ids = [c.id for c in visible]
    assert pub.id in ids and priv_in.id in ids
    assert priv_out.id not in ids
    assert ids == sorted(ids), "visible_channels must be id-ascending"


async def test_filter_readable_ids_drops_inaccessible_and_unknown_preserves_order(session):
    pub = await _public_channel(session, cid=0, name="general")
    priv_in = await _private_channel(session, cid=10, name="club")
    priv_out = await _private_channel(session, cid=20, name="vault")
    alice = await _user(session, "alice")
    await _join(session, priv_in, alice)
    requested = [priv_out.id, pub.id, "nonexistent", priv_in.id]
    got = await acl.filter_readable_ids(session, alice.id, requested)
    # priv_out (not a member) + unknown id dropped; order of survivors preserved.
    assert got == [pub.id, priv_in.id]


# =========================================================================
# REST seam — list + history
# =========================================================================

def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(channel_routes.router)
    app.include_router(message_routes.router)
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


async def _headers(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


async def test_list_hides_private_channel_from_non_member(client, session):
    await _public_channel(session, cid=0, name="general")
    await _private_channel(session, cid=10, name="secret")
    bob = await _user(session, "bob")
    resp = await client.get("/v1/channels", headers=await _headers(bob))
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()["channels"]]
    assert names == ["general"], "private channel must not leak into a non-member's list"


async def test_list_shows_private_channel_to_member(client, session):
    await _public_channel(session, cid=0, name="general")
    priv = await _private_channel(session, cid=10, name="secret")
    alice = await _user(session, "alice")
    await _join(session, priv, alice)
    resp = await client.get("/v1/channels", headers=await _headers(alice))
    names = sorted(c["name"] for c in resp.json()["channels"])
    assert names == ["general", "secret"]


async def test_history_private_non_member_is_404_not_403(client, session):
    priv = await _private_channel(session, cid=10, name="secret")
    session.add(Message(id=_ulid(1), channel_id=priv.id, sender_kind="human",
                        body="hush", created_at=_now()))
    await session.commit()
    bob = await _user(session, "bob")
    resp = await client.get(f"/v1/channels/{priv.id}/messages", headers=await _headers(bob))
    # Collapsed to 404 (existence-hiding) — never 403, which would confirm it exists.
    assert resp.status_code == 404


async def test_history_private_member_can_read(client, session):
    priv = await _private_channel(session, cid=10, name="secret")
    session.add(Message(id=_ulid(1), channel_id=priv.id, sender_kind="human",
                        body="hush", created_at=_now()))
    await session.commit()
    alice = await _user(session, "alice")
    await _join(session, priv, alice)
    resp = await client.get(f"/v1/channels/{priv.id}/messages", headers=await _headers(alice))
    assert resp.status_code == 200
    assert [m["body"] for m in resp.json()["messages"]] == ["hush"]


async def test_history_public_channel_open_to_any_authed_user(client, session):
    pub = await _public_channel(session, cid=0, name="general")
    session.add(Message(id=_ulid(1), channel_id=pub.id, sender_kind="human",
                        body="hi", created_at=_now()))
    await session.commit()
    bob = await _user(session, "bob")  # no membership anywhere
    resp = await client.get(f"/v1/channels/{pub.id}/messages", headers=await _headers(bob))
    assert resp.status_code == 200


# =========================================================================
# WS seam — subscribe + send
# =========================================================================

class _RecordingConn(Connection):
    def __init__(self, user_id: str):
        self.ws = None  # type: ignore[assignment]
        self.user_id = user_id
        self.subscribed = set()
        self.sent: list[dict] = []

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)


class _FakeBus:
    def __init__(self):
        self.sent: list = []

    def send(self, username, channel, text) -> bool:
        self.sent.append((username, channel, text))
        return True


class _FakeHub:
    async def fanout(self, channel_id, frame) -> None:
        pass


class _FakeGw:
    def __init__(self):
        self.bus = _FakeBus()
        self.hub = _FakeHub()


class _StubSessionLocal:
    """Yields the test session in place of ws.SessionLocal (which targets
    Postgres). __aexit__ does NOT close it — the conftest fixture owns its life."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def test_subscribe_omits_private_channel_for_non_member(session):
    pub = await _public_channel(session, cid=0, name="general")
    priv = await _private_channel(session, cid=10, name="secret")
    bob = await _user(session, "bob")
    conn = _RecordingConn(bob.id)
    await _handle_subscribe(
        conn, {"type": "subscribe", "channel_ids": [pub.id, priv.id]}, session)
    suback = [f for f in conn.sent if f["type"] == "suback"][0]
    # The private channel is silently dropped from both the subscribed set and
    # the suback — a non-member cannot subscribe, so fanout can never reach them.
    assert priv.id not in conn.subscribed
    assert priv.id not in suback["channel_fences"]
    assert pub.id in conn.subscribed


async def test_subscribe_includes_private_channel_for_member(session):
    priv = await _private_channel(session, cid=10, name="secret")
    alice = await _user(session, "alice")
    await _join(session, priv, alice)
    conn = _RecordingConn(alice.id)
    await _handle_subscribe(
        conn, {"type": "subscribe", "channel_ids": [priv.id]}, session)
    assert priv.id in conn.subscribed


async def test_send_to_private_non_member_is_no_channel_and_not_persisted(session, monkeypatch):
    import aiko_gateway.realtime.ws as ws_mod
    monkeypatch.setattr(ws_mod, "SessionLocal", lambda: _StubSessionLocal(session))
    priv = await _private_channel(session, cid=10, name="secret")
    bob = await _user(session, "bob")
    conn = _RecordingConn(bob.id)
    await _handle_send(_FakeGw(), conn, bob, {
        "type": "send", "channel_id": priv.id, "body": "intrusion",
        "client_msg_id": "c1", "reply_to": None,
    })
    errors = [f for f in conn.sent if f["type"] == "error"]
    assert errors and errors[0]["code"] == "no_channel"
    # Nothing persisted: the boundary rejected before create_outbound.
    rows = await messages_service.get_history(session, priv.id, limit=10)
    assert rows == []


async def test_send_to_private_member_without_can_post_is_forbidden(session, monkeypatch):
    import aiko_gateway.realtime.ws as ws_mod
    monkeypatch.setattr(ws_mod, "SessionLocal", lambda: _StubSessionLocal(session))
    priv = await _private_channel(session, cid=10, name="secret")
    muted = await _user(session, "muted")
    await _join(session, priv, muted, can_post=False)
    conn = _RecordingConn(muted.id)
    await _handle_send(_FakeGw(), conn, muted, {
        "type": "send", "channel_id": priv.id, "body": "let me in",
        "client_msg_id": "c1", "reply_to": None,
    })
    errors = [f for f in conn.sent if f["type"] == "error"]
    # A member knows the channel exists, so this is an honest 'forbidden' (no leak).
    assert errors and errors[0]["code"] == "forbidden"
    rows = await messages_service.get_history(session, priv.id, limit=10)
    assert rows == []
