"""get_history cursor paging — backward (`before`) and forward (`after`).

The forward `after` cursor is what makes B4's reconnect catch-up a crash-resumable
forward fill (design 04 §Gap 2). These tests pin the SQL contract both directions
rely on: a ULID total order, results ALWAYS returned ascending, deleted rows
excluded, and the `> after` / `< before` boundaries being strict (exclusive).
"""
from __future__ import annotations

import datetime as dt

from aiko_gateway.domain import messages_service
from aiko_gateway.domain.models import Channel, Message, Retraction


def _ulid(n: int) -> str:
    """A 26-char lexically-sortable stand-in ULID for ordering tests."""
    return f"{n:026d}"


# A viewer id for the paging tests. These seed messages with sender_user_id=None
# and no UserBlock rows, so the block-visibility filter is a no-op here (NULL
# sender is never blocked) — block-specific visibility is covered in
# test_moderation.py. This just satisfies the required viewer_id parameter.
_VIEWER = _ulid(99)


async def _seed(session, *, count: int = 5, deleted_at_ids: set[int] | None = None) -> str:
    """Seed one channel + `count` messages with ULIDs _ulid(1).._ulid(count)."""
    deleted_at_ids = deleted_at_ids or set()
    channel = Channel(id=_ulid(0), name="general", kind="standard", aiko_channel="general")
    session.add(channel)
    now = dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc)
    for i in range(1, count + 1):
        session.add(Message(
            id=_ulid(i), channel_id=channel.id, sender_kind="human",
            body=f"msg {i}", created_at=now + dt.timedelta(seconds=i),
            deleted_at=now if i in deleted_at_ids else None,
        ))
    await session.commit()
    return channel.id


async def test_default_returns_newest_page_ascending(session):
    cid = await _seed(session, count=5)
    rows = await messages_service.get_history(session, cid, _VIEWER, limit=3)
    # newest 3 (3,4,5), returned ASCENDING
    assert [r.id for r in rows] == [_ulid(3), _ulid(4), _ulid(5)]


async def test_before_is_exclusive_and_pages_older(session):
    cid = await _seed(session, count=5)
    rows = await messages_service.get_history(session, cid, _VIEWER, before=_ulid(3), limit=10)
    # strictly older than 3 → 1,2 (NOT 3), ascending
    assert [r.id for r in rows] == [_ulid(1), _ulid(2)]


async def test_after_forward_fills_oldest_gap_first(session):
    cid = await _seed(session, count=5)
    rows = await messages_service.get_history(session, cid, _VIEWER, after=_ulid(2), limit=2)
    # strictly newer than 2, OLDEST first (forward fill) → 3,4 (not 5 yet), ascending
    assert [r.id for r in rows] == [_ulid(3), _ulid(4)]


async def test_after_is_exclusive_at_the_edge(session):
    cid = await _seed(session, count=3)
    # after = newest → nothing newer (boundary is strict `>`)
    rows = await messages_service.get_history(session, cid, _VIEWER, after=_ulid(3), limit=10)
    assert rows == []


async def test_after_null_pages_from_start(session):
    cid = await _seed(session, count=4)
    rows = await messages_service.get_history(session, cid, _VIEWER, after=None, before=None, limit=2)
    # no cursor → backward default: newest 2 (3,4) ascending
    assert [r.id for r in rows] == [_ulid(3), _ulid(4)]


async def test_after_wins_when_both_passed(session):
    cid = await _seed(session, count=5)
    # both given → `after` direction wins (forward), per the documented contract
    rows = await messages_service.get_history(
        session, cid, _VIEWER, before=_ulid(2), after=_ulid(2), limit=10)
    assert [r.id for r in rows] == [_ulid(3), _ulid(4), _ulid(5)]


async def test_deleted_rows_excluded_both_directions(session):
    cid = await _seed(session, count=5, deleted_at_ids={3})
    fwd = await messages_service.get_history(session, cid, _VIEWER, after=_ulid(1), limit=10)
    assert [r.id for r in fwd] == [_ulid(2), _ulid(4), _ulid(5)]  # 3 skipped
    back = await messages_service.get_history(session, cid, _VIEWER, before=None, limit=10)
    assert _ulid(3) not in [r.id for r in back]


# --- retraction interleave (#7 takedown propagation) ------------------------

async def _retract(session, cid: str, *, rid: int, target: int) -> None:
    """Append a retraction event with ULID _ulid(rid) referencing message target."""
    session.add(Retraction(
        id=_ulid(rid), target_msg_id=_ulid(target), channel_id=cid))
    await session.commit()


async def test_forward_catch_up_delivers_retraction_above_watermark(session):
    """THE fix (#7). A client synced msg 1 (watermark=1), then msg 1 was taken down:
    soft-deleted (invisible to future reads) AND a retraction minted ABOVE the
    watermark. Forward catch-up from the client's watermark must surface the
    retraction so the client re-observes the deletion it would otherwise hold
    forever — the soft-delete alone sits below the cursor and never re-appears."""
    cid = await _seed(session, count=3, deleted_at_ids={1})   # msg1 taken down
    await _retract(session, cid, rid=5, target=1)             # retraction id > watermark
    rows = await messages_service.get_history(session, cid, _VIEWER, after=_ulid(1), limit=10)
    # msg 1 stays hidden (soft-deleted); msgs 2,3 + the retraction ride forward.
    assert [r.id for r in rows] == [_ulid(2), _ulid(3), _ulid(5)]
    retraction = rows[-1]
    assert isinstance(retraction, Retraction)
    assert messages_service.retraction_view(retraction) == {
        "type": "retraction", "id": _ulid(5),
        "target_msg_id": _ulid(1), "channel_id": cid}


async def test_retraction_interleaves_by_ulid_both_directions(session):
    """Messages and retractions merge on the single ULID axis, ascending. Messages
    sit at 10/20/30, retractions at 15/35 — a clean interleave proves ordering isn't
    "all messages then all retractions" but a true merge on id."""
    channel = Channel(id=_ulid(0), name="general", kind="standard", aiko_channel="general")
    session.add(channel)
    now = dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc)
    for i in (10, 20, 30):
        session.add(Message(id=_ulid(i), channel_id=channel.id, sender_kind="human",
                            body=f"msg {i}", created_at=now + dt.timedelta(seconds=i)))
    await session.commit()
    await _retract(session, channel.id, rid=15, target=10)
    await _retract(session, channel.id, rid=35, target=30)

    # Forward from the start: full ascending interleave.
    fwd = await messages_service.get_history(
        session, channel.id, _VIEWER, after=_ulid(1), limit=100)
    assert [r.id for r in fwd] == [_ulid(10), _ulid(15), _ulid(20), _ulid(30), _ulid(35)]
    # Backward default returns the same axis ascending, respecting the limit tail.
    back = await messages_service.get_history(
        session, channel.id, _VIEWER, before=None, limit=3)
    assert [r.id for r in back] == [_ulid(20), _ulid(30), _ulid(35)]  # newest 3, interleaved


async def test_interleave_merge_holds_when_both_streams_exceed_limit(session):
    """Boundary case (cage-match Wu): BOTH streams independently exceed `limit`,
    interleaved so the truncation cut lands mid-stream with a retraction exactly at
    position `limit`. This is the adversarial input the other interleave tests never
    hit (they use sub-`limit` streams) — it proves per-stream-limit + merge + truncate
    drops no page-member. A naive "take limit from one stream then fill" would return
    messages-only [10,20,30,40] and fail here."""
    channel = Channel(id=_ulid(0), name="general", kind="standard", aiko_channel="general")
    session.add(channel)
    now = dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc)
    for i in (10, 20, 30, 40, 50):                      # 5 messages (> limit)
        session.add(Message(id=_ulid(i), channel_id=channel.id, sender_kind="human",
                            body=f"m{i}", created_at=now + dt.timedelta(seconds=i)))
    await session.commit()
    for rid, tgt in ((15, 10), (25, 20), (35, 30), (45, 40)):   # 4 retractions (>= limit)
        session.add(Retraction(
            id=_ulid(rid), target_msg_id=_ulid(tgt), channel_id=channel.id))
    await session.commit()

    # Forward, limit=4: union ascending 10,15,20,25,30,... → first 4; the cut lands
    # on the retraction at id 25 — mid-stream for BOTH streams.
    fwd = await messages_service.get_history(session, channel.id, _VIEWER, after=_ulid(1), limit=4)
    assert [r.id for r in fwd] == [_ulid(10), _ulid(15), _ulid(20), _ulid(25)]
    # Backward, limit=4: newest 4 of the union → 35,40,45,50 ascending.
    back = await messages_service.get_history(session, channel.id, _VIEWER, before=None, limit=4)
    assert [r.id for r in back] == [_ulid(35), _ulid(40), _ulid(45), _ulid(50)]


async def test_retraction_returned_even_when_its_target_is_soft_deleted(session):
    """A retraction is channel-scoped, not gated by its target's visibility: the
    target is ALWAYS soft-deleted (that's what a takedown does), yet the retraction
    itself must still be delivered so the client can act on it."""
    cid = await _seed(session, count=2, deleted_at_ids={2})
    await _retract(session, cid, rid=9, target=2)
    rows = await messages_service.get_history(session, cid, _VIEWER, after=_ulid(1), limit=10)
    assert _ulid(9) in [r.id for r in rows]        # retraction present
    assert _ulid(2) not in [r.id for r in rows]    # its taken-down target is not
