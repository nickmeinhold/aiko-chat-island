"""Subscription-ack (suback) — the live/history fence + its ordering invariant.

Change B. On `subscribe`, the gateway must:
  (1) add the channel(s) to the connection's `subscribed` set, THEN
  (2) read the fence = MAX(message id) per channel and reply `suback{channel_fences}`.

The ORDERING in (1)->(2) is the whole correctness story. The fence partitions the
client's world: `id <= fence` is fetched from history (REST), `id > fence` arrives
live (WS). If the gateway read the fence BEFORE subscribing, a message that
persists + fanouts in that window is `> fence` (so the client won't fetch it from
history) AND not delivered live (the connection wasn't subscribed yet) -> lost
forever on a then-quiet channel. Subscribe FIRST and the worst case is a harmless
duplicate (`<= fence` AND delivered live), deduped by the cache's serverUlid UNIQUE.

These tests assert the BOUNDARY ("no message lost in the gap"), not just the
mechanism ("subscribed set before fence") — own the boundary, not the proxy.
"""
from __future__ import annotations

import datetime as dt

from aiko_gateway.domain import messages_service
from aiko_gateway.domain.models import Channel, Message
from aiko_gateway.realtime import envelopes
from aiko_gateway.realtime.hub import Connection, Hub
from aiko_gateway.realtime.ws import _handle_subscribe


def _ulid(n: int) -> str:
    """A 26-char lexically-sortable stand-in ULID for ordering tests."""
    return f"{n:026d}"


def _now() -> dt.datetime:
    return dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc)


class _RecordingConn(Connection):
    """A Connection that records sent frames instead of touching a socket."""

    def __init__(self, user_id: str = "u1"):
        self.ws = None  # type: ignore[assignment]
        self.user_id = user_id
        self.subscribed = set()
        self.sent: list[dict] = []

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)


async def _seed(session, *, count: int) -> str:
    """One channel + `count` messages with ULIDs _ulid(1).._ulid(count)."""
    channel = Channel(id=_ulid(0), name="general", kind="standard", aiko_channel="general")
    session.add(channel)
    for i in range(1, count + 1):
        session.add(Message(
            id=_ulid(i), channel_id=channel.id, sender_kind="human",
            body=f"msg {i}", created_at=_now() + dt.timedelta(seconds=i),
        ))
    await session.commit()
    return channel.id


def _suback_fences(conn: _RecordingConn) -> dict:
    subacks = [f for f in conn.sent if f.get("type") == "suback"]
    assert len(subacks) == 1, f"expected exactly one suback, got {subacks}"
    return subacks[0]["channel_fences"]


def _live_message_ids(conn: _RecordingConn) -> list[str]:
    return [f["msg"]["msg_id"] for f in conn.sent if f.get("type") == "message"]


async def test_suback_fence_is_newest_persisted_ulid(session):
    cid = await _seed(session, count=3)
    conn = _RecordingConn()
    await _handle_subscribe(conn, {"type": "subscribe", "channel_ids": [cid]}, session)
    assert _suback_fences(conn) == {cid: _ulid(3)}


async def test_suback_fence_empty_for_channel_with_no_messages(session):
    channel = Channel(id=_ulid(0), name="general", kind="standard", aiko_channel="general")
    session.add(channel)
    await session.commit()
    conn = _RecordingConn()
    await _handle_subscribe(
        conn, {"type": "subscribe", "channel_ids": [channel.id]}, session)
    # Empty fence = "no history boundary; everything is forward/live".
    assert _suback_fences(conn) == {channel.id: ""}


async def test_subscribe_adds_channel_to_subscribed_set(session):
    cid = await _seed(session, count=2)
    conn = _RecordingConn()
    await _handle_subscribe(conn, {"type": "subscribe", "channel_ids": [cid]}, session)
    assert cid in conn.subscribed


async def test_multiple_channels_each_get_their_own_fence(session):
    cid = await _seed(session, count=3)
    other = Channel(id=_ulid(50), name="dev", kind="standard", aiko_channel="dev")
    session.add(other)
    session.add(Message(id=_ulid(60), channel_id=other.id, sender_kind="human",
                        body="dev 1", created_at=_now()))
    await session.commit()
    conn = _RecordingConn()
    await _handle_subscribe(
        conn, {"type": "subscribe", "channel_ids": [cid, other.id]}, session)
    assert _suback_fences(conn) == {cid: _ulid(3), other.id: _ulid(60)}


async def test_fence_excludes_soft_deleted_tail_matching_get_history(session):
    """The fence predicate MUST match get_history's `deleted_at IS NULL`. If the
    newest row is soft-deleted, the fence is the newest VISIBLE row — otherwise
    B4's pager (history until cursor >= fence) could never reach a deleted-tail
    fence by visible rows, false-positiving its empty-page-before-fence check."""
    channel = Channel(id=_ulid(0), name="general", kind="standard", aiko_channel="general")
    session.add(channel)
    session.add(Message(id=_ulid(1), channel_id=channel.id, sender_kind="human",
                        body="visible", created_at=_now()))
    session.add(Message(id=_ulid(2), channel_id=channel.id, sender_kind="human",
                        body="deleted tail", created_at=_now() + dt.timedelta(seconds=2),
                        deleted_at=_now()))
    await session.commit()
    conn = _RecordingConn()
    await _handle_subscribe(
        conn, {"type": "subscribe", "channel_ids": [channel.id]}, session)
    # fence is _ulid(1) (newest visible), NOT _ulid(2) (the soft-deleted tail).
    assert _suback_fences(conn) == {channel.id: _ulid(1)}


async def test_no_message_lost_in_the_subscribe_effectiveness_gap(session, monkeypatch):
    """THE invariant (design 04 §Gap2). A message arriving in the window AFTER the
    fence is read must still reach the client — delivered live because the
    connection was already subscribed. Forces the path R5 demanded: a deliberately
    induced race, not an "unreachable by construction" hand-wave.

    Discriminates ordering: under the correct impl (subscribe THEN fence) the conn
    is subscribed when the injected message fanouts -> delivered live -> GREEN.
    Under the buggy impl (fence THEN subscribe) the conn is NOT yet subscribed ->
    fanout misses AND the post-fence message is `> fence` -> lost -> RED.
    """
    cid = await _seed(session, count=3)
    hub = Hub()
    conn = _RecordingConn()
    hub.register(conn)

    real_latest = messages_service.latest_ulid

    async def latest_then_inject(sess, channel_id, viewer_id):
        fence = await real_latest(sess, channel_id, viewer_id)  # fence read (..3)
        # A brand-new message arrives AFTER the fence read — the racy window.
        row = Message(id=_ulid(4), channel_id=channel_id, sender_kind="human",
                      body="msg 4", created_at=_now() + dt.timedelta(seconds=4))
        sess.add(row)
        await sess.commit()
        await hub.fanout(channel_id, envelopes.message_frame(
            messages_service.message_view(row)))
        return fence

    monkeypatch.setattr(messages_service, "latest_ulid", latest_then_inject)

    await _handle_subscribe(conn, {"type": "subscribe", "channel_ids": [cid]}, session)

    fence = _suback_fences(conn)[cid]
    delivered_live = _ulid(4) in _live_message_ids(conn)
    in_history = _ulid(4) <= fence
    assert delivered_live or in_history, (
        f"msg 4 LOST in the subscribe-effectiveness gap: not delivered live AND "
        f"not <= fence {fence!r}. `conn.subscribed` must be set BEFORE the fence read.")
    # It persisted strictly after the fence read, so it can only be covered LIVE.
    assert delivered_live
