"""UGC moderation (#7) — user blocks + message reports (Apple 1.2 / Google UGC).

Layers, mirroring the rest of the suite:
  * service tests call `moderation_service` directly and assert the data effects
    — block idempotency/guards, the MUTUAL visibility filter on history, the
    fence/history coupling under a block, the fanout exclusion set, the reply
    interaction gate, report idempotency, and the account-deletion cascade.
  * a send-path test drives `_handle_send` to prove a reply across a block is
    refused at the server seam (not just at the service helper).
  * route tests drive the HTTP contract (block/unblock/list/report) over ASGI.

App-under-test is built from JUST the moderation router (never `main`), keeping
the suite's "never import aiko_services" isolation invariant.
"""
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from aiko_gateway.domain import (
    accounts_service, messages_service, moderation_service, security, users_service,
)
from aiko_gateway.domain.models import Channel, Message, MessageReport, UserBlock
from aiko_gateway.realtime.ws import _handle_send
from aiko_gateway.rest import moderation as moderation_routes
from aiko_gateway.rest.deps import get_session


def _ulid(n: int) -> str:
    return f"{n:026d}"


def _now() -> dt.datetime:
    return dt.datetime(2026, 6, 28, tzinfo=dt.timezone.utc)


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


async def _msg(session, *, mid: int, channel: Channel, sender, kind: str = "human",
               sender_user_id: str | None = "__sender__") -> Message:
    """A message in `channel`. By default authored by `sender` (a User); pass
    `sender_user_id=None` to seed an external-actor (NULL-sender) message."""
    uid = sender.id if sender_user_id == "__sender__" else sender_user_id
    msg = Message(
        id=_ulid(mid), channel_id=channel.id, sender_user_id=uid,
        sender_kind=kind, sender_label=(sender.display_name if sender else "bot"),
        body=f"msg {mid}", created_at=_now() + dt.timedelta(seconds=mid))
    session.add(msg)
    await session.commit()
    return msg


# =========================================================================
# blocks — mutations
# =========================================================================

async def test_block_then_listed(session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    await moderation_service.block_user(session, a.id, b.id)
    blocks = await moderation_service.list_blocks(session, a.id)
    assert [x["user_id"] for x in blocks] == [b.id]
    assert blocks[0]["display_name"] == "Bob"


async def test_block_is_idempotent(session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    await moderation_service.block_user(session, a.id, b.id)
    await moderation_service.block_user(session, a.id, b.id)  # no duplicate row
    assert (await session.execute(
        select(func.count()).select_from(UserBlock))).scalar_one() == 1


async def test_block_self_raises(session):
    a = await _user(session, "alice")
    with pytest.raises(moderation_service.CannotBlockSelf):
        await moderation_service.block_user(session, a.id, a.id)


async def test_block_unknown_user_raises(session):
    a = await _user(session, "alice")
    with pytest.raises(moderation_service.UserNotFound):
        await moderation_service.block_user(session, a.id, _ulid(999))


async def test_unblock_removes(session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    await moderation_service.block_user(session, a.id, b.id)
    await moderation_service.unblock_user(session, a.id, b.id)
    assert await moderation_service.list_blocks(session, a.id) == []


async def test_unblock_never_blocked_is_noop(session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    await moderation_service.unblock_user(session, a.id, b.id)  # must not raise


# =========================================================================
# blocks — MUTUAL visibility on history
# =========================================================================

async def test_blocker_does_not_see_blocked_messages(session):
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    await _msg(session, mid=1, channel=ch, sender=a)
    await _msg(session, mid=2, channel=ch, sender=b)
    await moderation_service.block_user(session, a.id, b.id)

    rows = await messages_service.get_history(session, ch.id, a.id, limit=50)
    assert [r.id for r in rows] == [_ulid(1)]  # bob's msg 2 hidden from alice


async def test_block_is_mutual_blocked_user_also_loses_sight(session):
    """A blocks B. The MUTUAL guarantee: B also stops seeing A — even though B
    never pressed block. One directional row, symmetric visibility."""
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    await _msg(session, mid=1, channel=ch, sender=a)
    await _msg(session, mid=2, channel=ch, sender=b)
    await moderation_service.block_user(session, a.id, b.id)  # A blocks B only

    rows = await messages_service.get_history(session, ch.id, b.id, limit=50)
    assert [r.id for r in rows] == [_ulid(2)]  # alice's msg 1 hidden from bob too


async def test_third_party_sees_everyone(session):
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    c = await _user(session, "carol")
    await _msg(session, mid=1, channel=ch, sender=a)
    await _msg(session, mid=2, channel=ch, sender=b)
    await moderation_service.block_user(session, a.id, b.id)

    rows = await messages_service.get_history(session, ch.id, c.id, limit=50)
    assert [r.id for r in rows] == [_ulid(1), _ulid(2)]  # carol unaffected


async def test_external_actor_messages_always_visible(session):
    """A NULL-sender message (LLM/robot/REPL) can't be in a block relationship,
    so it is visible to everyone regardless of blocks."""
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    await _msg(session, mid=1, channel=ch, sender=b)
    await _msg(session, mid=2, channel=ch, sender=None, kind="llm", sender_user_id=None)
    await moderation_service.block_user(session, a.id, b.id)

    rows = await messages_service.get_history(session, ch.id, a.id, limit=50)
    assert [r.id for r in rows] == [_ulid(2)]  # bot visible, bob hidden


# =========================================================================
# the crux: fence (latest_ulid) MUST match history under a block
# =========================================================================

async def test_fence_matches_history_under_block(session):
    """THE coupling (design 04 + #7). B's message is the channel's newest. After
    A blocks B, A's fence must point at the newest message A can still SEE — not
    B's (which A's history will never return). Otherwise B4's reconnect pager
    ("history until cursor >= fence") can never reach the fence by visible rows
    and false-positives its empty-page-before-fence invariant check.

    Discriminates: with the block filter on BOTH reads, A's fence == A's newest
    visible row (== max of get_history(A)). With it on history only, the fence
    would be B's _ulid(2) — unreachable.
    """
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    await _msg(session, mid=1, channel=ch, sender=a)   # alice
    await _msg(session, mid=2, channel=ch, sender=b)   # bob — the newest overall
    await moderation_service.block_user(session, a.id, b.id)

    a_history = await messages_service.get_history(session, ch.id, a.id, limit=50)
    a_fence = await messages_service.latest_ulid(session, ch.id, a.id)
    # The fence is reachable: it equals the newest row A's history actually returns.
    assert a_fence == a_history[-1].id == _ulid(1)
    assert a_fence != _ulid(2)  # NOT bob's newest-overall message

    # Sanity: an unblocked third party's fence IS the newest overall.
    c = await _user(session, "carol")
    assert await messages_service.latest_ulid(session, ch.id, c.id) == _ulid(2)


async def test_fence_empty_when_only_blocked_messages_visible(session):
    """If every message in a channel is from a blocked user, the viewer's fence
    is "" (empty) — the same as an empty channel — so the pager treats it as
    "no history boundary" rather than chasing an invisible fence."""
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    await _msg(session, mid=1, channel=ch, sender=b)
    await _msg(session, mid=2, channel=ch, sender=b)
    await moderation_service.block_user(session, a.id, b.id)
    assert await messages_service.latest_ulid(session, ch.id, a.id) == ""


# =========================================================================
# fanout exclusion (live delivery twin of the history filter)
# =========================================================================

async def test_blocked_pair_user_ids_both_directions(session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    c = await _user(session, "carol")
    await moderation_service.block_user(session, a.id, b.id)  # a blocks b
    await moderation_service.block_user(session, c.id, a.id)  # c blocks a
    # Everyone in a block relationship with A, either direction: {b (a→b), c (c→a)}.
    assert await moderation_service.blocked_pair_user_ids(session, a.id) == {b.id, c.id}


async def test_fanout_skips_blocked_connection(session):
    """hub.fanout must not deliver to a connection in the exclusion set — the live
    twin of the history visibility filter."""
    from aiko_gateway.realtime.hub import Connection, Hub

    class _Rec(Connection):
        def __init__(self, uid):
            self.user_id = uid
            self.subscribed = {"chan"}
            self.sent: list[dict] = []

        async def send(self, frame):
            self.sent.append(frame)

    hub = Hub()
    blocked = _Rec("bob")
    other = _Rec("carol")
    hub.register(blocked)
    hub.register(other)
    await hub.fanout("chan", {"type": "message"}, exclude_user_ids={"bob"})
    assert blocked.sent == []          # excluded
    assert len(other.sent) == 1        # delivered


# =========================================================================
# no-interaction: reply across a block is refused at the send seam
# =========================================================================

class _FakeBus:
    def __init__(self):
        self.sent: list = []

    def send(self, username, channel, text) -> bool:
        self.sent.append((username, channel, text))
        return True


class _RecordingHub:
    def __init__(self):
        self.fanned: list = []

    async def fanout(self, channel_id, frame, *, exclude_user_ids=None) -> None:
        self.fanned.append((channel_id, frame, exclude_user_ids))


class _FakeGw:
    def __init__(self):
        self.bus = _FakeBus()
        self.hub = _RecordingHub()


class _RecordingConn:
    def __init__(self, user_id):
        self.ws = None
        self.user_id = user_id
        self.subscribed: set[str] = set()
        self.sent: list[dict] = []

    async def send(self, frame):
        self.sent.append(frame)


class _StubSessionLocal:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def test_reply_to_blocked_user_is_refused(session, monkeypatch):
    import aiko_gateway.realtime.ws as ws_mod
    monkeypatch.setattr(ws_mod, "SessionLocal", lambda: _StubSessionLocal(session))
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    target = await _msg(session, mid=1, channel=ch, sender=b)  # bob's message
    await moderation_service.block_user(session, a.id, b.id)

    gw = _FakeGw()
    conn = _RecordingConn(a.id)
    await _handle_send(gw, conn, a, {
        "type": "send", "channel_id": ch.id, "body": "@bob ...",
        "client_msg_id": "c1", "reply_to": target.id,
    })
    errors = [f for f in conn.sent if f["type"] == "error"]
    assert errors and errors[0]["code"] == "blocked"
    # Nothing persisted, nothing fanned out, nothing put on the bus.
    assert gw.hub.fanned == [] and gw.bus.sent == []
    assert (await session.execute(
        select(func.count()).select_from(Message)
        .where(Message.client_msg_id == "c1"))).scalar_one() == 0


async def test_reply_to_missing_target_rejected(session, monkeypatch):
    """A reply_to pointing at a non-existent message id is refused (would FK-500
    at insert otherwise) — cage-match Carnot MEDIUM."""
    import aiko_gateway.realtime.ws as ws_mod
    monkeypatch.setattr(ws_mod, "SessionLocal", lambda: _StubSessionLocal(session))
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    gw = _FakeGw()
    conn = _RecordingConn(a.id)
    await _handle_send(gw, conn, a, {
        "type": "send", "channel_id": ch.id, "body": "re: ghost",
        "client_msg_id": "c1", "reply_to": _ulid(777),  # no such message
    })
    errors = [f for f in conn.sent if f["type"] == "error"]
    assert errors and errors[0]["code"] == "no_reply_target"
    assert gw.hub.fanned == []


async def test_reply_to_cross_channel_rejected(session, monkeypatch):
    """A reply_to referencing a message in ANOTHER channel is refused — no minting
    a reference to a message outside this channel's context."""
    import aiko_gateway.realtime.ws as ws_mod
    monkeypatch.setattr(ws_mod, "SessionLocal", lambda: _StubSessionLocal(session))
    here = await _public_channel(session, cid=0, name="here")
    there = await _public_channel(session, cid=1, name="there")
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    foreign = await _msg(session, mid=1, channel=there, sender=b)
    gw = _FakeGw()
    conn = _RecordingConn(a.id)
    await _handle_send(gw, conn, a, {
        "type": "send", "channel_id": here.id, "body": "cross",
        "client_msg_id": "c1", "reply_to": foreign.id,
    })
    errors = [f for f in conn.sent if f["type"] == "error"]
    assert errors and errors[0]["code"] == "no_reply_target"


async def test_reply_to_same_channel_unblocked_is_allowed(session, monkeypatch):
    """The happy path still works: a reply to a same-channel, non-blocked message
    goes through (the new integrity check doesn't over-reject)."""
    import aiko_gateway.realtime.ws as ws_mod
    monkeypatch.setattr(ws_mod, "SessionLocal", lambda: _StubSessionLocal(session))
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    target = await _msg(session, mid=1, channel=ch, sender=b)  # not blocked
    gw = _FakeGw()
    conn = _RecordingConn(a.id)
    await _handle_send(gw, conn, a, {
        "type": "send", "channel_id": ch.id, "body": "good reply",
        "client_msg_id": "c1", "reply_to": target.id,
    })
    assert [f["type"] for f in conn.sent] == ["ack"]
    assert len(gw.hub.fanned) == 1


async def test_fence_stranding_across_block_self_heals_on_recompute(session):
    """Documents cage-match Carnot HIGH: the fence/history coupling is within one
    DB instant. A fence captured BEFORE a block can point at a row the viewer's
    post-block history will never return (the stranding). The durable fix is
    client-side (B4 treats empty-before-fence as a benign re-sync), but the server
    property that makes that safe is SELF-HEALING: recomputing the fence (the next
    subscribe) yields a value the current history actually reaches."""
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    await _msg(session, mid=1, channel=ch, sender=a)
    await _msg(session, mid=2, channel=ch, sender=b)  # bob's, newest overall

    # Fence read at subscribe, BEFORE any block: includes bob's newest.
    stale_fence = await messages_service.latest_ulid(session, ch.id, a.id)
    assert stale_fence == _ulid(2)

    await moderation_service.block_user(session, a.id, b.id)

    # The stale fence now strands: alice's history will never return _ulid(2).
    a_hist = await messages_service.get_history(session, ch.id, a.id, limit=50)
    assert stale_fence not in [r.id for r in a_hist]

    # SELF-HEAL: recompute the fence (next subscribe) → reachable by current history.
    fresh_fence = await messages_service.latest_ulid(session, ch.id, a.id)
    assert fresh_fence == _ulid(1) == a_hist[-1].id


async def test_normal_send_carries_block_exclusion_to_fanout(session, monkeypatch):
    """A non-reply send still goes through, and fanout receives the sender's
    block-exclusion set (so blocked parties' live connections are skipped)."""
    import aiko_gateway.realtime.ws as ws_mod
    monkeypatch.setattr(ws_mod, "SessionLocal", lambda: _StubSessionLocal(session))
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    await moderation_service.block_user(session, a.id, b.id)

    gw = _FakeGw()
    conn = _RecordingConn(a.id)
    await _handle_send(gw, conn, a, {
        "type": "send", "channel_id": ch.id, "body": "hi all",
        "client_msg_id": "c9", "reply_to": None,
    })
    assert [f["type"] for f in conn.sent] == ["ack"]
    assert len(gw.hub.fanned) == 1
    _, _, exclude = gw.hub.fanned[0]
    assert exclude == {b.id}  # bob excluded from alice's live fanout


# =========================================================================
# reports
# =========================================================================

async def test_report_inserts(session):
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    m = await _msg(session, mid=1, channel=ch, sender=b)
    rep = await moderation_service.report_message(
        session, reporter_id=a.id, message_id=m.id, reason="harassment")
    assert rep.message_id == m.id and rep.reason == "harassment"
    assert rep.reporter_user_id == a.id and rep.resolved_at is None


async def test_report_is_idempotent_per_reporter(session):
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    m = await _msg(session, mid=1, channel=ch, sender=b)
    r1 = await moderation_service.report_message(
        session, reporter_id=a.id, message_id=m.id, reason="spam")
    r2 = await moderation_service.report_message(
        session, reporter_id=a.id, message_id=m.id, reason="hate")  # same pair
    assert r1.id == r2.id  # existing returned, not stacked
    assert (await session.execute(
        select(func.count()).select_from(MessageReport))).scalar_one() == 1


async def test_report_unknown_message_raises(session):
    a = await _user(session, "alice")
    with pytest.raises(moderation_service.MessageNotFound):
        await moderation_service.report_message(
            session, reporter_id=a.id, message_id=_ulid(999), reason="spam")


async def test_cannot_report_message_in_unreadable_channel(session):
    """get_reportable_message is None for a message in a private channel the
    reporter can't see — existence-hidden behind the same None as 'missing'."""
    priv = await _private_channel(session)
    a = await _user(session, "alice")       # NOT a member of priv
    b = await _user(session, "bob")
    m = await _msg(session, mid=1, channel=priv, sender=b)
    assert await moderation_service.get_reportable_message(session, a.id, m.id) is None


# =========================================================================
# account-deletion cascade (protect the just-shipped neighbour)
# =========================================================================

async def test_delete_purges_blocks_both_directions(session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    c = await _user(session, "carol")
    await moderation_service.block_user(session, a.id, b.id)  # a→b
    await moderation_service.block_user(session, c.id, a.id)  # c→a
    await accounts_service.delete_user_account(session, a.id)
    # Every block touching A is gone (both directions); unrelated blocks would stay.
    assert (await session.execute(
        select(func.count()).select_from(UserBlock))).scalar_one() == 0


async def test_delete_anonymizes_reports_keeping_them(session):
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    m = await _msg(session, mid=1, channel=ch, sender=b)
    rep = await moderation_service.report_message(
        session, reporter_id=a.id, message_id=m.id, reason="spam")
    await accounts_service.delete_user_account(session, a.id)
    refreshed = await session.get(MessageReport, rep.id)
    # The report survives for ops; only the reporter link is severed.
    assert refreshed is not None
    assert refreshed.reporter_user_id is None
    assert refreshed.reason == "spam"


async def test_delete_user_with_moderation_rows_does_not_fk_violate(session):
    """The full account-deletion cascade completes with blocks + reports present
    — the regression guard for the new tables vs the shipped deletion path."""
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    m = await _msg(session, mid=1, channel=ch, sender=b)
    await moderation_service.block_user(session, a.id, b.id)
    await moderation_service.report_message(
        session, reporter_id=a.id, message_id=m.id, reason="spam")
    await accounts_service.delete_user_account(session, a.id)  # must not raise
    assert await users_service.get_by_id(session, a.id) is None


# =========================================================================
# route layer — the HTTP contract
# =========================================================================

@pytest_asyncio.fixture
async def client(session):
    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(moderation_routes.router)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


async def test_block_route_204_and_listed(client, session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    resp = await client.post(f"/v1/users/{b.id}/block", headers=_auth(a))
    assert resp.status_code == 204
    listed = await client.get("/v1/blocks", headers=_auth(a))
    assert [x["user_id"] for x in listed.json()["blocks"]] == [b.id]


async def test_block_self_is_400(client, session):
    a = await _user(session, "alice")
    resp = await client.post(f"/v1/users/{a.id}/block", headers=_auth(a))
    assert resp.status_code == 400


async def test_block_unknown_user_is_404(client, session):
    a = await _user(session, "alice")
    resp = await client.post(f"/v1/users/{_ulid(999)}/block", headers=_auth(a))
    assert resp.status_code == 404


async def test_unblock_route_204(client, session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    await moderation_service.block_user(session, a.id, b.id)
    resp = await client.delete(f"/v1/users/{b.id}/block", headers=_auth(a))
    assert resp.status_code == 204
    assert await moderation_service.list_blocks(session, a.id) == []


async def test_blocks_unauthenticated_is_401(client, session):
    assert (await client.get("/v1/blocks")).status_code == 401


async def test_report_route_201(client, session):
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    m = await _msg(session, mid=1, channel=ch, sender=b)
    resp = await client.post(
        f"/v1/messages/{m.id}/report", json={"reason": "harassment"}, headers=_auth(a))
    assert resp.status_code == 201 and resp.json()["report_id"]


async def test_report_invalid_reason_is_422(client, session):
    ch = await _public_channel(session)
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    m = await _msg(session, mid=1, channel=ch, sender=b)
    resp = await client.post(
        f"/v1/messages/{m.id}/report", json={"reason": "nonsense"}, headers=_auth(a))
    assert resp.status_code == 422


async def test_report_unknown_message_is_404(client, session):
    a = await _user(session, "alice")
    resp = await client.post(
        f"/v1/messages/{_ulid(999)}/report", json={"reason": "spam"}, headers=_auth(a))
    assert resp.status_code == 404
