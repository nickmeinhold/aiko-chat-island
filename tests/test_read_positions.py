"""Read positions — the island half of #4834a.

THE TESTS THAT MATTER ARE THE MERGE ONES. A store that keeps what it was last
told is trivially correct and trivially wrong; the whole design is which write
WINS when two devices disagree, and that mark-as-unread survives it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select, text

from aiko_gateway.domain import read_positions_service as svc
from aiko_gateway.domain.models import ChannelReadPosition, Channel, User
from aiko_gateway.domain.ids import new_ulid


def _ulid(n: int) -> str:
    """A sortable 26-char ULID-shaped id. Lexicographic order tracks n, which is
    what the mark-as-unread test needs to express 'an EARLIER message'."""
    return str(n).rjust(26, "0")


async def _user(session, name: str) -> User:
    u = User(id=new_ulid(), username=name, display_name=name,
             aiko_username=name)
    session.add(u)
    await session.commit()
    return u


async def _channel(session, name: str = "general") -> Channel:
    ch = Channel(id=new_ulid(), name=name, kind="standard",
                 aiko_channel=name)
    session.add(ch)
    await session.commit()
    return ch


# ---------------------------------------------------------------------------
# the merge rule
# ---------------------------------------------------------------------------

async def test_a_higher_seq_wins(session):
    u = await _user(session, "alice")
    ch = await _channel(session)
    await svc.set_positions(session, user_id=u.id,
                            positions={ch.id: (_ulid(10), 1)})
    await svc.set_positions(session, user_id=u.id,
                            positions={ch.id: (_ulid(20), 2)})
    assert await svc.get_positions(session, user_id=u.id) == {
        ch.id: (_ulid(20), 2)}


async def test_MARK_AS_UNREAD_survives_the_merge(session):
    """A LOWER last_read with a HIGHER seq must WIN. This is the whole design.

    Merging by max(ULID) — the obvious rule, and the app tab's first proposal,
    which Nick rejected — makes this impossible: moving the watermark backwards
    writes a lower ULID that always loses to the stored one, so a user can never
    mark a channel unread again from any device. The explicit seq is what buys
    it back, and this test is the reason the column exists.
    """
    u = await _user(session, "alice")
    ch = await _channel(session)
    await svc.set_positions(session, user_id=u.id,
                            positions={ch.id: (_ulid(50), 1)})
    # The user marks it unread: an EARLIER position, a LATER version.
    await svc.set_positions(session, user_id=u.id,
                            positions={ch.id: (_ulid(10), 2)})
    assert await svc.get_positions(session, user_id=u.id) == {
        ch.id: (_ulid(10), 2)}, "mark-as-unread was swallowed by the merge"


async def test_a_stale_devices_late_replay_is_a_no_op(session):
    """Device B synced first; device A's older write arrives after. Nothing moves.

    This is the ordering the island cannot control — it sees network order, not
    causal order — and it is why the client owns the counter.
    """
    u = await _user(session, "alice")
    ch = await _channel(session)
    await svc.set_positions(session, user_id=u.id,
                            positions={ch.id: (_ulid(20), 5)})
    await svc.set_positions(session, user_id=u.id,
                            positions={ch.id: (_ulid(99), 3)})  # older device
    assert await svc.get_positions(session, user_id=u.id) == {
        ch.id: (_ulid(20), 5)}


async def test_an_equal_seq_does_not_overwrite(session):
    """Strictly greater, not >=. Two devices that both reached seq 4 independently
    have no ordering between them, and picking the later arrival would be
    last-write-wins wearing a version number."""
    u = await _user(session, "alice")
    ch = await _channel(session)
    await svc.set_positions(session, user_id=u.id,
                            positions={ch.id: (_ulid(20), 4)})
    await svc.set_positions(session, user_id=u.id,
                            positions={ch.id: (_ulid(77), 4)})
    assert await svc.get_positions(session, user_id=u.id) == {
        ch.id: (_ulid(20), 4)}


async def test_replaying_the_same_write_is_idempotent(session):
    u = await _user(session, "alice")
    ch = await _channel(session)
    for _ in range(3):
        await svc.set_positions(session, user_id=u.id,
                                positions={ch.id: (_ulid(20), 2)})
    rows = (await session.execute(
        select(ChannelReadPosition).where(
            ChannelReadPosition.user_id == u.id))).scalars().all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------

async def test_positions_are_per_user(session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    ch = await _channel(session)
    await svc.set_positions(session, user_id=a.id,
                            positions={ch.id: (_ulid(10), 1)})
    await svc.set_positions(session, user_id=b.id,
                            positions={ch.id: (_ulid(90), 1)})
    assert await svc.get_positions(session, user_id=a.id) == {
        ch.id: (_ulid(10), 1)}
    assert await svc.get_positions(session, user_id=b.id) == {
        ch.id: (_ulid(90), 1)}


async def test_positions_are_per_channel(session):
    u = await _user(session, "alice")
    c1 = await _channel(session, "one")
    c2 = await _channel(session, "two")
    await svc.set_positions(session, user_id=u.id, positions={
        c1.id: (_ulid(10), 1), c2.id: (_ulid(20), 1)})
    got = await svc.get_positions(session, user_id=u.id)
    assert got == {c1.id: (_ulid(10), 1), c2.id: (_ulid(20), 1)}


async def test_no_positions_is_an_empty_map_not_an_error(session):
    u = await _user(session, "alice")
    assert await svc.get_positions(session, user_id=u.id) == {}


# ---------------------------------------------------------------------------
# validation, refused at the write
# ---------------------------------------------------------------------------

async def test_the_empty_floor_is_a_VALUE_not_a_missing_one(session):
    """'' is the app's first-sight baseline for a channel empty at settle time
    (channel_read_store.dart). Storing it must work, and must be distinguishable
    from no row at all — an absence with two causes is the defect class this repo
    keeps re-finding."""
    u = await _user(session, "alice")
    ch = await _channel(session)
    await svc.set_positions(session, user_id=u.id, positions={ch.id: ("", 1)})
    got = await svc.get_positions(session, user_id=u.id)
    assert got == {ch.id: ("", 1)}
    assert ch.id in got  # present, not absent


@pytest.mark.parametrize("bad_last_read", ["short", "x" * 27, "!" * 26])
async def test_a_malformed_last_read_is_refused(session, bad_last_read):
    """Not cosmetic: the app sorts watermarks lexicographically, so a value above
    every real ULID pins a bad monotonic floor and freezes unread at zero forever
    (cage-match #109). The app refuses it at its end; this is the other end."""
    u = await _user(session, "alice")
    ch = await _channel(session)
    with pytest.raises(svc.InvalidReadPosition):
        await svc.set_positions(session, user_id=u.id,
                                positions={ch.id: (bad_last_read, 1)})


async def test_a_negative_seq_is_refused(session):
    u = await _user(session, "alice")
    ch = await _channel(session)
    with pytest.raises(svc.InvalidReadPosition):
        await svc.set_positions(session, user_id=u.id,
                                positions={ch.id: (_ulid(10), -1)})


async def test_one_bad_entry_stores_NONE_of_the_batch(session):
    """A partial store would leave the client believing a sync succeeded while
    some channels silently kept an old watermark — worse than a refusal, because
    it is invisible."""
    u = await _user(session, "alice")
    c1 = await _channel(session, "one")
    c2 = await _channel(session, "two")
    with pytest.raises(svc.InvalidReadPosition):
        await svc.set_positions(session, user_id=u.id, positions={
            c1.id: (_ulid(10), 1), c2.id: ("nope", 1)})
    assert await svc.get_positions(session, user_id=u.id) == {}


async def test_an_empty_batch_is_a_no_op(session):
    u = await _user(session, "alice")
    await svc.set_positions(session, user_id=u.id, positions={})
    assert await svc.get_positions(session, user_id=u.id) == {}


# ---------------------------------------------------------------------------
# the constraint, at the DB rather than only above it
# ---------------------------------------------------------------------------

async def test_the_db_itself_refuses_a_negative_seq(session):
    """The service refuses first; this proves the column would too.

    Same posture as every other closed-set/range guard in this schema: a gate the
    service alone enforces is a gate the next caller can walk past — which is the
    hole 0028 was written to close for message_reports.reason.
    """
    u = await _user(session, "alice")
    ch = await _channel(session)
    with pytest.raises(Exception) as exc:
        await session.execute(text(
            "INSERT INTO channel_read_positions "
            "(user_id, channel_id, last_read, seq, updated_at) "
            "VALUES (:u, :c, '', -5, '2026-09-26 00:00:00')"),
            {"u": u.id, "c": ch.id})
        await session.commit()
    assert "ck_channel_read_positions_seq" in str(exc.value) or "CHECK" in str(
        exc.value), (
        f"the row was refused, but not demonstrably by the seq CHECK: {exc.value}")
