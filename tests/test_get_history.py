"""get_history cursor paging — backward (`before`) and forward (`after`).

The forward `after` cursor is what makes B4's reconnect catch-up a crash-resumable
forward fill (design 04 §Gap 2). These tests pin the SQL contract both directions
rely on: a ULID total order, results ALWAYS returned ascending, deleted rows
excluded, and the `> after` / `< before` boundaries being strict (exclusive).
"""
from __future__ import annotations

import datetime as dt

from aiko_gateway.domain import messages_service
from aiko_gateway.domain.models import Channel, Message


def _ulid(n: int) -> str:
    """A 26-char lexically-sortable stand-in ULID for ordering tests."""
    return f"{n:026d}"


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
    rows = await messages_service.get_history(session, cid, limit=3)
    # newest 3 (3,4,5), returned ASCENDING
    assert [r.id for r in rows] == [_ulid(3), _ulid(4), _ulid(5)]


async def test_before_is_exclusive_and_pages_older(session):
    cid = await _seed(session, count=5)
    rows = await messages_service.get_history(session, cid, before=_ulid(3), limit=10)
    # strictly older than 3 → 1,2 (NOT 3), ascending
    assert [r.id for r in rows] == [_ulid(1), _ulid(2)]


async def test_after_forward_fills_oldest_gap_first(session):
    cid = await _seed(session, count=5)
    rows = await messages_service.get_history(session, cid, after=_ulid(2), limit=2)
    # strictly newer than 2, OLDEST first (forward fill) → 3,4 (not 5 yet), ascending
    assert [r.id for r in rows] == [_ulid(3), _ulid(4)]


async def test_after_is_exclusive_at_the_edge(session):
    cid = await _seed(session, count=3)
    # after = newest → nothing newer (boundary is strict `>`)
    rows = await messages_service.get_history(session, cid, after=_ulid(3), limit=10)
    assert rows == []


async def test_after_null_pages_from_start(session):
    cid = await _seed(session, count=4)
    rows = await messages_service.get_history(session, cid, after=None, before=None, limit=2)
    # no cursor → backward default: newest 2 (3,4) ascending
    assert [r.id for r in rows] == [_ulid(3), _ulid(4)]


async def test_after_wins_when_both_passed(session):
    cid = await _seed(session, count=5)
    # both given → `after` direction wins (forward), per the documented contract
    rows = await messages_service.get_history(
        session, cid, before=_ulid(2), after=_ulid(2), limit=10)
    assert [r.id for r in rows] == [_ulid(3), _ulid(4), _ulid(5)]


async def test_deleted_rows_excluded_both_directions(session):
    cid = await _seed(session, count=5, deleted_at_ids={3})
    fwd = await messages_service.get_history(session, cid, after=_ulid(1), limit=10)
    assert [r.id for r in fwd] == [_ulid(2), _ulid(4), _ulid(5)]  # 3 skipped
    back = await messages_service.get_history(session, cid, before=None, limit=10)
    assert _ulid(3) not in [r.id for r in back]
