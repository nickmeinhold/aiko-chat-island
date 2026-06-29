"""Membership concurrency invariants under the PROD engine — file-backed SQLite (#12).

#12 originally asked for a *Postgres* concurrency test. The gateway moved to
file-backed SQLite in prod (#1281), which makes that framing not just stale but
MISLEADING: the last-admin guard used ``SELECT ... FOR UPDATE``, which Postgres
honours but SQLite silently ignores (no row locks). A Postgres test would PASS
while the prod SQLite path orphaned channels.

These tests run against a real file-backed SQLite DB with CONCURRENT connections —
what the in-memory single-connection unit tests structurally cannot exercise —
and pin two properties:

1. Concurrent admin-leaves keep >= 1 admin (the last-admin invariant), enforced by
   the atomic conditional DELETE that replaced FOR UPDATE
   (memberships_service._delete_membership_unless_last_admin).
2. The SQLite engine applies busy_timeout (db.make_engine), so a concurrent writer
   WAITS for the lock instead of getting SQLITE_BUSY — the loser gets a clean
   LastAdmin, not a 500. (WAL is deliberately deferred — it changes the on-disk
   format and would need backup-tooling verification; busy_timeout alone fixes the
   bug, measured.)

RED-proven: revert the atomic DELETE to the old read-then-delete and
test_concurrent_admin_leaves_keep_exactly_one_admin orphans the channel (0 admins).
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiko_gateway.db import Base, make_engine
from aiko_gateway.domain import memberships_service as ms
from aiko_gateway.domain import users_service
from aiko_gateway.domain.models import Channel, Membership, Role

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def file_sessionmaker(tmp_path):
    """A sessionmaker over a real file-backed SQLite engine built by the PROD path
    (db.make_engine -> busy_timeout). Each session() is a SEPARATE connection from
    the pool, so two of them genuinely contend — unlike the in-memory
    single-connection conftest engine."""
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/concurrency.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _admin_count(sm, channel_id: str) -> int:
    async with sm() as s:
        return (
            await s.execute(
                select(func.count()).select_from(Membership).where(
                    Membership.channel_id == channel_id, Membership.role == Role.ADMIN)
            )
        ).scalar()


async def _two_admin_channel(sm) -> tuple[str, str, str]:
    """A channel with two admins (alice, bob). Returns (channel_id, alice_id, bob_id)."""
    async with sm() as s:
        ch = Channel(id="0" * 26, name="c", kind="standard", aiko_channel="c",
                     is_private=True)
        s.add(ch)
        alice = await users_service.create_user(
            s, username="alice", display_name="A", password="pw")
        bob = await users_service.create_user(
            s, username="bob", display_name="B", password="pw")
        s.add(Membership(channel_id=ch.id, user_id=alice.id, role=Role.ADMIN))
        s.add(Membership(channel_id=ch.id, user_id=bob.id, role=Role.ADMIN))
        await s.commit()
        return ch.id, alice.id, bob.id


async def test_concurrent_admin_leaves_keep_exactly_one_admin(file_sessionmaker, monkeypatch):
    """Two admins of a 2-admin channel leave CONCURRENTLY, forced to hit the
    last-admin DELETE at the same instant. Exactly one must succeed and the other
    be cleanly refused (LastAdmin) — the channel must NEVER be orphaned.

    The barrier forces the worst-case interleaving (both inside the guard at once);
    the atomic conditional DELETE + busy_timeout is what makes the loser re-read
    the post-commit admin count and refuse instead of both-deleting (the measured
    pre-fix bug) or 500-ing on SQLITE_BUSY."""
    sm = file_sessionmaker
    cid, aid, bid = await _two_admin_channel(sm)

    # Force both leaves into the atomic DELETE simultaneously.
    real = ms._delete_membership_unless_last_admin
    barrier = asyncio.Barrier(2)

    async def patched(session, **kw):
        try:
            await asyncio.wait_for(barrier.wait(), timeout=5)
        except (asyncio.TimeoutError, asyncio.BrokenBarrierError):
            pass
        return await real(session, **kw)

    monkeypatch.setattr(ms, "_delete_membership_unless_last_admin", patched)

    async def leave(uid):
        try:
            async with sm() as s:
                await ms.leave(s, channel_id=cid, actor_id=uid)
            return "left"
        except ms.LastAdmin:
            return "LastAdmin"

    results = sorted(await asyncio.gather(leave(aid), leave(bid)))

    assert await _admin_count(sm, cid) == 1, "channel must never be orphaned (>=1 admin)"
    assert results == ["LastAdmin", "left"], (
        f"exactly one leave succeeds, one cleanly refused — got {results} "
        "(both 'left' = the orphan bug; an OperationalError = SQLITE_BUSY, "
        "missing busy_timeout)")


async def test_concurrent_admin_removals_keep_exactly_one_admin(file_sessionmaker, monkeypatch):
    """Same invariant via the OTHER caller: two admins each remove the other
    concurrently. remove_member shares the atomic guard, so the channel keeps an
    admin no matter who wins the race."""
    sm = file_sessionmaker
    cid, aid, bid = await _two_admin_channel(sm)

    real = ms._delete_membership_unless_last_admin
    barrier = asyncio.Barrier(2)

    async def patched(session, **kw):
        try:
            await asyncio.wait_for(barrier.wait(), timeout=5)
        except (asyncio.TimeoutError, asyncio.BrokenBarrierError):
            pass
        return await real(session, **kw)

    monkeypatch.setattr(ms, "_delete_membership_unless_last_admin", patched)

    async def remove(actor, target):
        try:
            async with sm() as s:
                await ms.remove_member(s, channel_id=cid, actor_id=actor, target_user_id=target)
            return "removed"
        except ms.LastAdmin:
            return "LastAdmin"

    results = sorted(await asyncio.gather(remove(aid, bid), remove(bid, aid)))
    assert await _admin_count(sm, cid) == 1, "channel must never be orphaned"
    assert results == ["LastAdmin", "removed"]


async def test_concurrent_removal_of_same_admin_is_idempotent_not_lastadmin(
    file_sessionmaker, monkeypatch
):
    """When two admins concurrently remove the SAME third admin (3-admin channel),
    one delete wins and the other matches 0 rows — but that 0 means "already gone",
    NOT "last admin". The loser must get idempotent SUCCESS, never a spurious
    LastAdmin (Kelvin + Carnot cage-match, PR#29: rowcount==0 was ambiguous).

    The target (carnot) is removed exactly once, TWO admins remain, and neither
    caller sees LastAdmin — there were always >=2 other admins, so 'last admin'
    would be a lie."""
    sm = file_sessionmaker
    async with sm() as s:
        ch = Channel(id="0" * 26, name="c", kind="standard", aiko_channel="c",
                     is_private=True)
        s.add(ch)
        alice = await users_service.create_user(s, username="alice", display_name="A", password="pw")
        bob = await users_service.create_user(s, username="bob", display_name="B", password="pw")
        carnot = await users_service.create_user(s, username="carnot", display_name="C", password="pw")
        for u in (alice, bob, carnot):
            s.add(Membership(channel_id=ch.id, user_id=u.id, role=Role.ADMIN))
        await s.commit()
        cid, aid, bid, carid = ch.id, alice.id, bob.id, carnot.id

    real = ms._delete_membership_unless_last_admin
    barrier = asyncio.Barrier(2)

    async def patched(session, **kw):
        try:
            await asyncio.wait_for(barrier.wait(), timeout=5)
        except (asyncio.TimeoutError, asyncio.BrokenBarrierError):
            pass
        return await real(session, **kw)

    monkeypatch.setattr(ms, "_delete_membership_unless_last_admin", patched)

    async def remove_carnot(actor):
        try:
            async with sm() as s:
                await ms.remove_member(s, channel_id=cid, actor_id=actor, target_user_id=carid)
            return "removed"
        except ms.LastAdmin:
            return "LastAdmin"
        except ms.NotAMember:
            return "NotAMember"

    results = sorted(await asyncio.gather(remove_carnot(aid), remove_carnot(bid)))
    # carnot gone, alice+bob remain; the loser saw "already gone" -> idempotent success.
    assert await _admin_count(sm, cid) == 2
    assert "LastAdmin" not in results, (
        f"removing a non-last admin must never report LastAdmin — got {results}")


async def test_sqlite_engine_applies_busy_timeout(tmp_path):
    """The PROD SQLite tuning (db._tune_sqlite_concurrency) is actually applied on
    each connection: busy_timeout=5000, so a concurrent writer WAITS for the lock
    instead of getting SQLITE_BUSY immediately. (WAL is deliberately NOT enabled —
    see db._tune_sqlite_concurrency — so journal_mode stays the default.)"""
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/pragmas.db")
    try:
        async with engine.connect() as conn:
            bt = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()
    finally:
        await engine.dispose()
    assert bt == 5000, f"busy_timeout not applied (got {bt})"
