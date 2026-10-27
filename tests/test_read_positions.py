"""Read positions — the island half of #358, under the v2 contract (compare-and-
swap; PR #429's v2 comment is the one statement of it).

THE TESTS THAT MATTER ARE THE CONFLICT ONES. A store that keeps what it was last
told is trivially correct and trivially wrong. The design is what happens when two
devices disagree: the one that loses must be TOLD, with the value that won, and no
client can choose a version that beats everyone forever.
"""
from __future__ import annotations

import time

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from aiko_gateway.domain import channels_service, security
from aiko_gateway.domain import read_positions_service as svc
from aiko_gateway.domain.models import ChannelReadPosition, Channel, User
from aiko_gateway.domain.ids import new_ulid
from aiko_gateway.rest import read_positions as read_position_routes
from aiko_gateway.rest.deps import get_session
from aiko_gateway.rest.errors import register_error_handlers

APPLIED, CONFLICT, SKIPPED = svc.Outcome.APPLIED, svc.Outcome.CONFLICT, svc.Outcome.SKIPPED


def _ulid(n: int) -> str:
    """A canonical 26-char ULID whose lexicographic order tracks n, which is what
    the mark-as-unread test needs to express 'an EARLIER message'."""
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


async def _put(session, user, positions):
    return await svc.set_positions(session, user_id=user.id, positions=positions)


# ---------------------------------------------------------------------------
# compare-and-swap
# ---------------------------------------------------------------------------

async def test_a_first_write_is_applied_at_version_1(session):
    u, ch = await _user(session, "alice"), await _channel(session)
    r = await _put(session, u, {ch.id: (_ulid(10), 0)})
    assert r == {ch.id: svc.EntryResult(APPLIED, _ulid(10), 1)}
    assert await svc.get_positions(session, user_id=u.id) == {ch.id: (_ulid(10), 1)}


async def test_a_write_on_the_current_version_is_applied_and_advances_it(session):
    u, ch = await _user(session, "alice"), await _channel(session)
    await _put(session, u, {ch.id: (_ulid(10), 0)})
    r = await _put(session, u, {ch.id: (_ulid(20), 1)})
    assert r[ch.id] == svc.EntryResult(APPLIED, _ulid(20), 2)


async def test_the_LOSER_of_a_race_is_told_the_winner(session):
    """Two devices both saw version 1. The first write applies; the second is a
    CONFLICT carrying the value that won, so the losing device converges in the
    same round trip. Under the first design this was a silent 204 (#429 r1)."""
    u, ch = await _user(session, "alice"), await _channel(session)
    await _put(session, u, {ch.id: (_ulid(10), 0)})
    phone = await _put(session, u, {ch.id: (_ulid(30), 1)})
    tablet = await _put(session, u, {ch.id: (_ulid(20), 1)})
    assert phone[ch.id] == svc.EntryResult(APPLIED, _ulid(30), 2)
    assert tablet[ch.id] == svc.EntryResult(CONFLICT, _ulid(30), 2), (
        "the losing device must learn what the island holds, including its seq")
    assert await svc.get_positions(session, user_id=u.id) == {ch.id: (_ulid(30), 2)}


async def test_MARK_AS_UNREAD_is_an_ordinary_edit(session):
    """An EARLIER ULID on the current version applies. Nothing compares ULIDs, so
    moving the watermark backwards is never refused for being backwards."""
    u, ch = await _user(session, "alice"), await _channel(session)
    await _put(session, u, {ch.id: (_ulid(50), 0)})
    r = await _put(session, u, {ch.id: (_ulid(10), 1)})
    assert r[ch.id] == svc.EntryResult(APPLIED, _ulid(10), 2), (
        "mark-as-unread was refused")


async def test_a_stale_base_writes_nothing(session):
    u, ch = await _user(session, "alice"), await _channel(session)
    await _put(session, u, {ch.id: (_ulid(10), 0)})
    await _put(session, u, {ch.id: (_ulid(20), 1)})
    r = await _put(session, u, {ch.id: (_ulid(99), 1)})  # still thinks it's v1
    assert r[ch.id] == svc.EntryResult(CONFLICT, _ulid(20), 2)
    assert await svc.get_positions(session, user_id=u.id) == {ch.id: (_ulid(20), 2)}


async def test_a_base_of_0_against_an_existing_row_is_a_conflict(session):
    """A second device's first write must not clobber the first device's."""
    u, ch = await _user(session, "alice"), await _channel(session)
    await _put(session, u, {ch.id: (_ulid(10), 0)})
    r = await _put(session, u, {ch.id: (_ulid(77), 0)})
    assert r[ch.id] == svc.EntryResult(CONFLICT, _ulid(10), 1)


async def test_a_base_ahead_of_a_missing_row_is_a_conflict_at_version_0(session):
    """No row is version 0: a client whose base names a version the island no
    longer holds is told 0, and can resend from there."""
    u, ch = await _user(session, "alice"), await _channel(session)
    r = await _put(session, u, {ch.id: (_ulid(10), 5)})
    assert r[ch.id] == svc.EntryResult(CONFLICT, None, 0)
    assert await svc.get_positions(session, user_id=u.id) == {}


async def test_a_retried_write_whose_reply_was_lost_reports_applied(session):
    """A phone's PUT lands but the reply is lost; it retries the same body. The
    island already holds exactly that write's result, so the retry says applied
    rather than handing the phone a conflict carrying its own value."""
    u, ch = await _user(session, "alice"), await _channel(session)
    await _put(session, u, {ch.id: (_ulid(10), 0)})
    first = await _put(session, u, {ch.id: (_ulid(20), 1)})
    retry = await _put(session, u, {ch.id: (_ulid(20), 1)})
    assert first[ch.id] == retry[ch.id] == svc.EntryResult(APPLIED, _ulid(20), 2)
    assert await svc.get_positions(session, user_id=u.id) == {ch.id: (_ulid(20), 2)}


async def test_no_client_can_choose_a_version_that_wins_forever(session):
    """The first design's freeze: one client sent seq near 2**63, won, and nothing
    could beat it. Now a client never sends a version, only the base it saw, and
    a base beyond the island's range is malformed."""
    u, ch = await _user(session, "alice"), await _channel(session)
    with pytest.raises(svc.InvalidReadPosition):
        await _put(session, u, {ch.id: (_ulid(10), svc.MAX_BASE_SEQ + 1)})
    r = await _put(session, u, {ch.id: (_ulid(10), svc.MAX_BASE_SEQ)})
    assert r[ch.id] == svc.EntryResult(CONFLICT, None, 0), (
        "a huge base must not be able to create or move a row")


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------

async def test_positions_are_per_user(session):
    a, b = await _user(session, "alice"), await _user(session, "bob")
    ch = await _channel(session)
    await _put(session, a, {ch.id: (_ulid(10), 0)})
    await _put(session, b, {ch.id: (_ulid(90), 0)})
    assert await svc.get_positions(session, user_id=a.id) == {ch.id: (_ulid(10), 1)}
    assert await svc.get_positions(session, user_id=b.id) == {ch.id: (_ulid(90), 1)}


async def test_positions_are_per_channel(session):
    u = await _user(session, "alice")
    c1, c2 = await _channel(session, "one"), await _channel(session, "two")
    await _put(session, u, {c1.id: (_ulid(10), 0), c2.id: (_ulid(20), 0)})
    assert await svc.get_positions(session, user_id=u.id) == {
        c1.id: (_ulid(10), 1), c2.id: (_ulid(20), 1)}


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
    u, ch = await _user(session, "alice"), await _channel(session)
    await _put(session, u, {ch.id: ("", 0)})
    got = await svc.get_positions(session, user_id=u.id)
    assert got == {ch.id: ("", 1)}


async def test_a_real_ulid_is_accepted(session):
    """The check must still accept what the island actually mints."""
    u, ch = await _user(session, "alice"), await _channel(session)
    real = new_ulid()
    r = await _put(session, u, {ch.id: (real, 0)})
    assert r[ch.id] == svc.EntryResult(APPLIED, real, 1)


@pytest.mark.parametrize("bad_last_read", [
    "short", "x" * 27, "!" * 26,
    # Crockford-valid letters, but a first character above 7: no real ULID starts
    # there, and these sort above every real one (the unread-freezing value).
    "Z" * 26, "8" + "0" * 25,
    "0" * 25 + "a",   # lowercase: not canonical
    "0" * 25 + "I",   # I, L, O and U are not in Crockford's alphabet
    "0" * 25 + "é",  # isalnum() accepted Unicode letters
])
async def test_a_malformed_last_read_is_refused(session, bad_last_read):
    u, ch = await _user(session, "alice"), await _channel(session)
    with pytest.raises(svc.InvalidReadPosition):
        await _put(session, u, {ch.id: (bad_last_read, 0)})


@pytest.mark.parametrize("future", [
    "7" + "Z" * 25,                                     # canonical, millennia out
    new_ulid(int(time.time() * 1000) + 3 * 3600 * 1000),  # three hours out
])
async def test_a_watermark_from_the_future_is_refused(session, future):
    """A watermark names a message the island already minted, so it can't be later
    than now. Under the app's "a forward read wins" policy a future value would
    stick on every device (Tesla, #429 r2)."""
    u, ch = await _user(session, "alice"), await _channel(session)
    with pytest.raises(svc.InvalidReadPosition):
        await _put(session, u, {ch.id: (future, 0)})


async def test_a_deleted_user_cannot_get_rows_recreated(session):
    """A PUT that authenticated, then lost the race to account deletion, must not
    re-create the deleted user's rows: the first-write insert selects FROM users
    as well as channels (Carnot, #429 r2). FK is off in prod, so nothing else
    would stop it."""
    ch = await _channel(session)
    ghost = new_ulid()  # a user id with no users row: the account is gone
    await svc.set_positions(session, user_id=ghost, positions={ch.id: (_ulid(10), 0)})
    rows = (await session.execute(select(ChannelReadPosition))).scalars().all()
    assert rows == []


async def test_a_negative_base_is_refused(session):
    u, ch = await _user(session, "alice"), await _channel(session)
    with pytest.raises(svc.InvalidReadPosition):
        await _put(session, u, {ch.id: (_ulid(10), -1)})


async def test_a_refusal_names_the_channel(session):
    """So a client can find and drop the one bad entry (Tesla, #429 r1)."""
    u = await _user(session, "alice")
    c1, c2 = await _channel(session, "one"), await _channel(session, "two")
    with pytest.raises(svc.InvalidReadPosition) as exc:
        await _put(session, u, {c1.id: (_ulid(10), 0), c2.id: ("nope", 0)})
    assert exc.value.channel_id == c2.id
    assert c2.id in str(exc.value)


async def test_one_malformed_entry_stores_NONE_of_the_batch(session):
    u = await _user(session, "alice")
    c1, c2 = await _channel(session, "one"), await _channel(session, "two")
    with pytest.raises(svc.InvalidReadPosition):
        await _put(session, u, {c1.id: (_ulid(10), 0), c2.id: ("nope", 0)})
    assert await svc.get_positions(session, user_id=u.id) == {}


async def test_an_empty_batch_is_a_no_op(session):
    u = await _user(session, "alice")
    assert await _put(session, u, {}) == {}
    assert await svc.get_positions(session, user_id=u.id) == {}


# ---------------------------------------------------------------------------
# existence, and the channel cascade
# ---------------------------------------------------------------------------

async def test_an_unknown_channel_is_SKIPPED_and_the_rest_is_stored(session):
    """Skipped, not refused: malformed entries refuse the whole batch, so refusing
    a merely STALE one would let one deleted channel block every future sync."""
    u, ch = await _user(session, "alice"), await _channel(session)
    r = await _put(session, u, {ch.id: (_ulid(10), 0),
                                "no-such-channel": (_ulid(20), 0)})
    assert r == {ch.id: svc.EntryResult(APPLIED, _ulid(10), 1),
                 "no-such-channel": svc.EntryResult(SKIPPED)}
    rows = (await session.execute(select(ChannelReadPosition))).scalars().all()
    assert [r.channel_id for r in rows] == [ch.id]


async def test_deleting_a_channel_removes_its_read_positions(session):
    """FK is OFF in prod (ISL-0002): the owning service must clear the children."""
    u = await _user(session, "alice")
    keep, gone = await _channel(session, "keep"), await _channel(session, "gone")
    await _put(session, u, {keep.id: (_ulid(10), 0), gone.id: (_ulid(20), 0)})
    assert await channels_service.hard_delete_channel(session, "gone") is True
    await session.commit()
    assert await svc.get_positions(session, user_id=u.id) == {keep.id: (_ulid(10), 1)}


# ---------------------------------------------------------------------------
# the constraint, at the DB rather than only above it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_seq", [-5, 0])
async def test_the_db_itself_refuses_a_seq_below_1(session, bad_seq):
    """The service never writes below 1; this proves the column refuses it too.
    0 matters most: it is the wire's name for "no row", and a stored row at 0
    would answer every write with "resend from 0", which could never move it."""
    u, ch = await _user(session, "alice"), await _channel(session)
    with pytest.raises(Exception) as exc:
        await session.execute(text(
            "INSERT INTO channel_read_positions "
            "(user_id, channel_id, last_read, seq, updated_at) "
            "VALUES (:u, :c, '', :s, '2026-09-26 00:00:00')"),
            {"u": u.id, "c": ch.id, "s": bad_seq})
        await session.commit()
    assert "ck_channel_read_positions_seq" in str(exc.value) or "CHECK" in str(
        exc.value), (
        f"the row was refused, but not demonstrably by the seq CHECK: {exc.value}")


# ---------------------------------------------------------------------------
# the wire, through the route
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(session):
    async def _override_session():
        yield session

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(read_position_routes.router)
    app.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


async def _rput(client, user, positions):
    return await client.put("/v1/read-positions", headers=_auth(user),
                            json={"positions": positions})


async def test_route_reply_carries_every_outcome(client, session):
    """The wire shape the app tab builds against: per entry, a status and (unless
    skipped) the value the island now holds. A conflict carries the STORED seq,
    which the client needs to resend (app tab, #429)."""
    u = await _user(session, "alice")
    c1, c2 = await _channel(session, "one"), await _channel(session, "two")
    await _rput(client, u, {c2.id: {"last_read": _ulid(5), "base_seq": 0}})
    r = await _rput(client, u, {
        c1.id: {"last_read": _ulid(10), "base_seq": 0},
        c2.id: {"last_read": _ulid(99), "base_seq": 7},
        "no-such-channel": {"last_read": "", "base_seq": 0}})
    assert r.status_code == 200, r.text
    assert r.json() == {"positions": {
        c1.id: {"status": "applied", "last_read": _ulid(10), "seq": 1},
        c2.id: {"status": "conflict", "last_read": _ulid(5), "seq": 1},
        "no-such-channel": {"status": "skipped"}}}


async def test_route_a_conflict_with_nothing_stored_is_version_0(client, session):
    u, ch = await _user(session, "alice"), await _channel(session)
    r = await _rput(client, u, {ch.id: {"last_read": "", "base_seq": 3}})
    assert r.json()["positions"][ch.id] == {
        "status": "conflict", "last_read": None, "seq": 0}


async def test_route_a_first_design_body_is_a_loud_422(client, session):
    """`seq` instead of `base_seq` must not be silently misread: an unknown key is
    ignored, so the missing required base_seq is what makes this loud."""
    u, ch = await _user(session, "alice"), await _channel(session)
    r = await _rput(client, u, {ch.id: {"last_read": "", "seq": 1}})
    assert r.status_code == 422, r.text
    assert await svc.get_positions(session, user_id=u.id) == {}


async def test_route_a_batch_with_the_empty_floor_is_applied(client, session):
    """The app tab's catch (#429): '' is a real watermark the app persists. A check
    that refused it would 422 the whole batch, and that device would never sync."""
    u = await _user(session, "alice")
    c1, c2 = await _channel(session, "one"), await _channel(session, "two")
    r = await _rput(client, u, {c1.id: {"last_read": "", "base_seq": 0},
                                c2.id: {"last_read": new_ulid(), "base_seq": 0}})
    assert r.status_code == 200, r.text
    assert {v["status"] for v in r.json()["positions"].values()} == {"applied"}
    got = (await client.get("/v1/read-positions", headers=_auth(u))).json()
    assert got["positions"][c1.id] == {"last_read": "", "seq": 1}


async def test_route_a_malformed_entry_is_422_naming_the_channel(client, session):
    u, ch = await _user(session, "alice"), await _channel(session)
    r = await _rput(client, u, {ch.id: {"last_read": "Z" * 26, "base_seq": 0}})
    assert r.status_code == 422, r.text
    assert ch.id in r.text


async def test_route_an_oversized_base_is_422_not_500(client, session):
    u, ch = await _user(session, "alice"), await _channel(session)
    r = await _rput(client, u, {ch.id: {"last_read": "", "base_seq": 2**63}})
    assert r.status_code == 422, r.text


async def test_route_a_batch_over_the_cap_is_422(client, session):
    """The 1000-entry cap is a pydantic `Field(max_length=...)` on a dict: pin
    that it is live on the pinned pydantic rather than decorative."""
    u = await _user(session, "alice")
    over = {f"c{i}": {"last_read": "", "base_seq": 0} for i in range(1001)}
    r = await _rput(client, u, over)
    assert r.status_code == 422, r.text


async def test_route_put_is_rate_limited_per_user(client, session, monkeypatch):
    """Prod SQLite has one writer; an unthrottled PUT loop queues every message
    persist behind it (#429 r1). The 61st PUT in a window is refused."""
    from aiko_gateway.config import settings
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    u, ch = await _user(session, "alice"), await _channel(session)
    body = {ch.id: {"last_read": "", "base_seq": 0}}
    codes = [(await _rput(client, u, body)).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60, codes
    assert codes[60] == 429, codes[60]
