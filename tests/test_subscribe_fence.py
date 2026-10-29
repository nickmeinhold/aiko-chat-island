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
from aiko_gateway.domain.models import Channel, Message, Retraction
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


async def test_fence_advances_to_include_a_retraction_above_newest_message(session):
    """The fence axis now carries retractions (#7). A retraction with an id ABOVE
    the newest visible message must PULL THE FENCE UP to it — otherwise it lands in
    the `id > fence -> live` partition on a fresh subscribe and is never replayed.
    get_history returns retractions, so the fence (its paired read) must agree."""
    channel = Channel(id=_ulid(0), name="general", kind="standard", aiko_channel="general")
    session.add(channel)
    session.add(Message(id=_ulid(3), channel_id=channel.id, sender_kind="human",
                        body="newest visible msg", created_at=_now()))
    await session.commit()
    # A takedown of msg 3 mints a retraction ABOVE it and soft-deletes it.
    session.add(Retraction(id=_ulid(7), target_msg_id=_ulid(3), channel_id=channel.id))
    await session.commit()
    conn = _RecordingConn()
    await _handle_subscribe(
        conn, {"type": "subscribe", "channel_ids": [channel.id]}, session)
    # Fence is the retraction id (7), the newest thing on the axis — NOT msg 3.
    assert _suback_fences(conn) == {channel.id: _ulid(7)}


async def test_fence_is_retraction_even_when_its_target_message_is_soft_deleted(session):
    """The retraction's target being soft-deleted (as every takedown target is)
    must not drop the fence back below the retraction — the retraction is on the
    axis regardless of its target's visibility."""
    channel = Channel(id=_ulid(0), name="general", kind="standard", aiko_channel="general")
    session.add(channel)
    session.add(Message(id=_ulid(3), channel_id=channel.id, sender_kind="human",
                        body="taken down", created_at=_now(), deleted_at=_now()))
    session.add(Retraction(id=_ulid(7), target_msg_id=_ulid(3), channel_id=channel.id))
    await session.commit()
    conn = _RecordingConn()
    await _handle_subscribe(
        conn, {"type": "subscribe", "channel_ids": [channel.id]}, session)
    assert _suback_fences(conn) == {channel.id: _ulid(7)}


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

    # THE SEAM IS THE FENCE READ, wherever the route does it. It was `latest_ulid`
    # until design 21 routed the subscribe through `subscribe_view` (the fence can be
    # capped below a held call invite); patching a function the route no longer
    # calls would inject nothing and test nothing.
    real_view = messages_service.subscribe_view

    async def view_then_inject(sess, channel_ids, viewer_id):
        view = await real_view(sess, channel_ids, viewer_id)  # fence read (..3)
        (channel_id,) = channel_ids
        # A brand-new message arrives AFTER the fence read — the racy window.
        row = Message(id=_ulid(4), channel_id=channel_id, sender_kind="human",
                      body="msg 4", created_at=_now() + dt.timedelta(seconds=4))
        sess.add(row)
        await sess.commit()
        await hub.fanout(channel_id, envelopes.message_frame(
            messages_service.message_view(row)))
        return view

    monkeypatch.setattr(messages_service, "subscribe_view", view_then_inject)

    await _handle_subscribe(conn, {"type": "subscribe", "channel_ids": [cid]}, session)

    fence = _suback_fences(conn)[cid]
    delivered_live = _ulid(4) in _live_message_ids(conn)
    in_history = _ulid(4) <= fence
    assert delivered_live or in_history, (
        f"msg 4 LOST in the subscribe-effectiveness gap: not delivered live AND "
        f"not <= fence {fence!r}. `conn.subscribed` must be set BEFORE the fence read.")
    # It persisted strictly after the fence read, so it can only be covered LIVE.
    assert delivered_live


# --- the fence is NOT monotonic, by design (pinned for aiko_chat_app#336) --- #
#
# The app takes its read-position baseline from the CURRENT subscribe's fence and
# relies on that fence moving BACKWARDS when the newest item becomes hidden to this
# viewer (island #431 bounds a watermark by the newest VISIBLE item). A future
# "the fence only grows" optimisation here would silently break that: these two
# tests make it loud.

async def _subscribe_fence(session, channel_id: str, user_id: str = "u1") -> str:
    conn = _RecordingConn(user_id)
    await _handle_subscribe(
        conn, {"type": "subscribe", "channel_ids": [channel_id]}, session)
    return _suback_fences(conn)[channel_id]


async def test_fence_goes_backwards_across_subscribes_when_the_newest_is_soft_deleted(session):
    cid = await _seed(session, count=2)
    assert await _subscribe_fence(session, cid) == _ulid(2)
    (await session.get(Message, _ulid(2))).deleted_at = _now()
    await session.commit()
    assert await _subscribe_fence(session, cid) == _ulid(1), (
        "the fence must fall back to the newest VISIBLE item, not stay at a hidden one")


async def test_fence_goes_backwards_for_a_viewer_who_blocks_the_newest_author(session):
    from aiko_gateway.domain import moderation_service, users_service
    viewer = await users_service.create_user(
        session, username="viewer", display_name="Viewer", password="pw")
    author = await users_service.create_user(
        session, username="author", display_name="Author", password="pw")
    cid = await _seed(session, count=1)
    session.add(Message(id=_ulid(2), channel_id=cid, sender_kind="human",
                        sender_user_id=author.id, body="newest",
                        created_at=_now() + dt.timedelta(seconds=9)))
    await session.commit()
    assert await _subscribe_fence(session, cid, viewer.id) == _ulid(2)
    await moderation_service.block_user(session, viewer.id, author.id)
    assert await _subscribe_fence(session, cid, viewer.id) == _ulid(1), (
        "a block must pull this viewer's fence back below the blocked author's message")
