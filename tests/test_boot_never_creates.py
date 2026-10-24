"""Starting never creates an island: every door to the database opens it open-only,
and the gateway has no way to make one: ``scripts/build_seed.py`` builds the image's seed.

The bad outcome this file pins: a lost or damaged ``aiko.db`` coming back as a NEW,
EMPTY island that serves (every account, message and key gone). It is not detected
here, it is impossible: no connect can create the file. So each refuse arm asserts the
refusal AND that nothing was created, and each door (the migrator, the app's engine,
lifespan's ``verify_schema``, a hand-run alembic) is driven against a missing file.
The admit arms are positive controls: an existing island must still boot, and
the seed builder must still build one, or a standup or every live island breaks.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from urllib.parse import quote

import pytest
from alembic import command
from sqlalchemy import text

from aiko_gateway import migrate
import build_seed
from aiko_gateway.config import settings
from aiko_gateway.worker_guard import open_only


def _point_app_at(tmp_path, monkeypatch, name: str = "aiko.db"):
    db = tmp_path / name
    monkeypatch.setattr(settings, "db_url", f"sqlite+aiosqlite:///{db}")
    return db


def _ro(db) -> str:
    """Encoded, or a `#` in the name cuts the path short and SQLite opens (and
    creates) a different file: the bug open_only exists to avoid."""
    return f"file:{quote(str(db), safe='/')}?mode=ro"


def _tables(db) -> set[str]:
    with sqlite3.connect(_ro(db), uri=True) as c:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _plant_canary(db, value: str) -> None:
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE IF NOT EXISTS _canary (v TEXT)")
        c.execute("INSERT INTO _canary VALUES (?)", (value,))


def _canary(db) -> list[str]:
    with sqlite3.connect(_ro(db), uri=True) as c:
        return [r[0] for r in c.execute("SELECT v FROM _canary")]


# --- admit arms (positive controls) -------------------------------------------

def test_the_seed_builder_builds_a_fresh_database_at_head(tmp_path, monkeypatch) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    build_seed.build()
    assert {"users", "alembic_version"} <= _tables(db)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["aiko.db"], "no side files"


def test_an_existing_island_boots_without_the_flag(tmp_path, monkeypatch) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    build_seed.build()
    _plant_canary(db, "melb")
    migrate.run()  # every ordinary start
    assert _canary(db) == ["melb"]


@pytest.mark.parametrize("arg", ["--new-island", "--create", "--seed"])
def test_the_migrator_takes_no_arguments_so_no_flag_can_create(tmp_path, monkeypatch, arg) -> None:
    """The create mode PR#405 r1-r3 reviewed (`--new-island`) is gone, not hidden: any
    argument is a usage error, and nothing is created (Nick, 2026-10-25)."""
    db = _point_app_at(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="usage"):
        migrate.main(["migrate", arg])
    assert not db.exists()
    build_seed.build()
    migrate.main(["migrate"])  # the plain start admits an island
    assert "users" in _tables(db)


def test_a_path_with_uri_metacharacters_still_migrates(tmp_path, monkeypatch) -> None:
    """`#`, `%` and a space are legal in a DB_URL path but mean something in an SQLite
    URI: open_only must percent-encode them, and alembic's configparser must not choke
    on the resulting `%` (Noether, PR#399 r2). (`?` can't appear: DB_URL itself reads
    it as a query string.)"""
    db = _point_app_at(tmp_path, monkeypatch, name="50% off #2.db")
    build_seed.build()
    migrate.run()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["50% off #2.db"]
    assert "users" in _tables(db)


def test_a_uri_form_db_url_is_opened_open_only_too(tmp_path, monkeypatch) -> None:
    """`DB_URL` written as an SQLite URI, even one asking for `mode=rwc` (read, write,
    CREATE): open_only overrides it, so the spelling can't reopen the door."""
    db = tmp_path / "aiko.db"
    monkeypatch.setattr(settings, "db_url", f"sqlite+aiosqlite:///file:{db}?mode=rwc&uri=true")
    assert migrate._db_file() == db.resolve()
    assert "mode=rw&" in open_only(settings.db_url) or open_only(settings.db_url).endswith("mode=rw")
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_NO_DATABASE"):
        migrate.run()
    assert not db.exists()
    build_seed.build()
    assert "users" in _tables(db)


def test_a_url_with_no_file_is_left_alone(monkeypatch) -> None:
    for url in ("sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite:///file:x?mode=memory&uri=true",
                "postgresql+asyncpg://u@h/db"):
        assert open_only(url) == url


# --- refuse arms: the door is shut, and nothing was created --------------------

def test_the_migrator_refuses_a_missing_database_and_creates_nothing(tmp_path, monkeypatch) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_NO_DATABASE"):
        migrate.run()
    assert list(tmp_path.iterdir()) == []


def test_a_database_that_vanishes_after_the_check_is_not_reborn(tmp_path, monkeypatch) -> None:
    """The existence check is only the message. The guarantee is that every connect is
    open-only, so a file removed between the check and the connect makes the connect
    FAIL rather than create it."""
    db = _point_app_at(tmp_path, monkeypatch)
    build_seed.build()
    real = migrate._table_names

    async def vanish_then_look():
        db.unlink()
        return await real()

    monkeypatch.setattr(migrate, "_table_names", vanish_then_look)
    with pytest.raises(Exception):
        migrate.run()
    assert not db.exists()


@pytest.mark.parametrize("contents", ["zero-byte", "bookkeeping-only"])
def test_a_database_with_no_island_in_it_is_not_built_into_one(tmp_path, monkeypatch, contents) -> None:
    """A truncated or emptied db exists, so open-only admits the connect. Building the
    schema into it would be the same new, empty island by another route."""
    db = _point_app_at(tmp_path, monkeypatch)
    if contents == "zero-byte":
        db.touch()
    else:
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    before = db.read_bytes()
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_NOT_AN_ISLAND"):
        migrate.run()
    assert db.read_bytes() == before


@pytest.mark.parametrize("what", ["an island", "a symlink to an island"])
def test_the_seed_builder_refuses_when_anything_is_already_there(tmp_path, monkeypatch, what) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    real = tmp_path / "real.db"
    _point_app_at(tmp_path, monkeypatch, name="real.db")
    build_seed.build()
    _plant_canary(real, "melb")
    if what == "a symlink to an island":
        db.symlink_to(real)
        _point_app_at(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="SEED_REFUSE_EXISTS"):
        build_seed.build()
    assert _canary(real) == ["melb"]


def test_the_seed_builder_refuses_a_dangling_symlink_and_creates_nothing(tmp_path, monkeypatch) -> None:
    """The case where only the UNRESOLVED path can refuse: the link's target does not
    exist, so resolving first hands the creator a free path and it creates the target
    (Noether, Carnot, Tesla, PR#405 r1). The arm above can't see this, because its
    target exists and refuses for that reason instead."""
    db = _point_app_at(tmp_path, monkeypatch)
    lost = tmp_path / "elsewhere" / "lost.db"
    lost.parent.mkdir()
    db.symlink_to(lost)
    with pytest.raises(RuntimeError, match="SEED_REFUSE_EXISTS"):
        build_seed.build()
    assert not lost.exists()


@pytest.mark.parametrize("contents", ["zero-byte", "bookkeeping-only"])
def test_a_hand_run_alembic_cannot_build_an_island_into_an_empty_file(
        tmp_path, monkeypatch, contents) -> None:
    """Open-only stops a missing FILE being created; the producer of an empty island
    from an existing empty file is the baseline migration itself, and it refuses unless
    the run carries the seed-build capability (claude-skills control, Carnot 4/4, PR#405 r1)."""
    db = _point_app_at(tmp_path, monkeypatch)
    if contents == "zero-byte":
        db.touch()
    else:
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_NOT_AN_ISLAND"):
        command.upgrade(migrate._alembic_config(), "head")
    assert "users" not in _tables(db)


def _pre_alembic_island(tmp_path, monkeypatch):
    """An island at the baseline schema with no stamp: the live prod db before alembic."""
    db = _point_app_at(tmp_path, monkeypatch)
    build_seed.create_empty(db)
    command.upgrade(migrate._alembic_config(seed_build=True), "0001")
    with sqlite3.connect(db) as c:
        c.execute("DROP TABLE alembic_version")
        # Identity as file metadata, not a table: adoption verifies the schema EQUALS the
        # baseline and rightly refuses an extra canary table.
        c.execute("PRAGMA user_version = 4242")
    return db


def _user_version(db) -> int:
    with sqlite3.connect(_ro(db), uri=True) as c:
        return c.execute("PRAGMA user_version").fetchone()[0]


def test_a_hand_run_alembic_on_an_unstamped_island_says_adopt_and_harms_nothing(
        tmp_path, monkeypatch) -> None:
    """The baseline's refusal must not tell the owner of a living island to move it
    aside (Tesla, PR#405 r2), and the migrator must still adopt it afterwards, even though
    the refused run leaves an EMPTY alembic_version table behind (measured)."""
    db = _pre_alembic_island(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNSTAMPED_ISLAND") as refused:
        command.upgrade(migrate._alembic_config(), "head")
    assert "aside" not in str(refused.value)
    assert "alembic_version" in _tables(db), "the measured leftover this test exists for"
    migrate.run()  # adopts: stamps the baseline, then upgrades
    assert _user_version(db) == 4242, "the same island file, not a rebuilt one"
    assert asyncio.run(migrate._schema_status()).state is migrate.SchemaState.HEAD


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through mode 000")
def test_an_unreadable_directory_is_named_as_that_not_as_a_missing_database(
        tmp_path, monkeypatch) -> None:
    """`Path.exists()` says False when it can't LOOK, which would send an operator with
    a permissions problem to restore a backup (Maxwell, PR#405 r1)."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(settings, "db_url", f"sqlite+aiosqlite:///{data / 'aiko.db'}")
    data.chmod(0)
    try:
        with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNREADABLE"):
            migrate.run()
    finally:
        data.chmod(0o755)
    assert list(data.iterdir()) == []


def test_the_app_engine_never_creates_the_file(tmp_path) -> None:
    from aiko_gateway.db import make_engine

    db = tmp_path / "aiko.db"
    engine = make_engine(f"sqlite+aiosqlite:///{db}")

    async def connect():
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    with pytest.raises(Exception, match="unable to open database file"):
        asyncio.run(connect())
    assert not db.exists()


def test_lifespan_refuses_a_missing_database_and_creates_nothing(tmp_path, monkeypatch) -> None:
    """The uvicorn-only entrance (`run … uvicorn`, which replaces the image's CMD and
    so never runs the migrator) used to leave a 0-byte file behind."""
    from aiko_gateway import db as db_module

    db = _point_app_at(tmp_path, monkeypatch)
    monkeypatch.setattr(db_module, "engine", db_module.make_engine(settings.db_url))
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_NO_DATABASE"):
        asyncio.run(db_module.verify_schema())
    assert not db.exists()


def test_a_hand_run_alembic_upgrade_creates_nothing(tmp_path, monkeypatch) -> None:
    """`alembic upgrade head` is a door too (env.py reads settings.db_url)."""
    db = _point_app_at(tmp_path, monkeypatch)
    with pytest.raises(Exception, match="unable to open database file"):
        command.upgrade(migrate._alembic_config(), "head")
    assert not db.exists()


# --- what the refusals SAY: they name the repair, never a way to create -----------
# An operator reading a refusal is on a box whose island is in doubt, and "move it
# aside" or "create one" are the two steps that turn a lost island into an empty one
# (PR#405 r3). So every gateway refusal is collected here and held to the same rule; a
# new refusal that isn't added to this list is the gap to look for in review.


def _refusal(tmp_path, monkeypatch, case: str) -> str:
    db = _point_app_at(tmp_path, monkeypatch)
    if case == "missing":
        go = migrate.run
    elif case == "dangling symlink":
        db.symlink_to(tmp_path / "lost.db")
        go = migrate.run
    elif case == "empty file":
        db.touch()
        go = migrate.run
    elif case == "empty file, hand-run alembic":
        db.touch()
        go = lambda: command.upgrade(migrate._alembic_config(), "head")  # noqa: E731
    elif case == "postgres, empty":
        go = lambda: (_ for _ in ()).throw(  # noqa: E731
            RuntimeError(migrate.not_an_island_message(None, "is empty")))
    with pytest.raises(RuntimeError) as refused:
        go()
    return str(refused.value)


REFUSALS = ["missing", "dangling symlink", "empty file", "empty file, hand-run alembic",
            "postgres, empty"]


@pytest.mark.parametrize("case", REFUSALS)
def test_no_refusal_offers_a_way_to_create_or_to_move_the_database(
        tmp_path, monkeypatch, case) -> None:
    msg = _refusal(tmp_path, monkeypatch, case)
    assert "MIGRATE_REFUSE_" in msg
    for never in ("aside", "--new-island", "build_seed", "seed"):
        assert never not in msg.lower(), f"refusal mentions {never!r}: {msg}"
    assert "restore" in msg and "standup" in msg


def test_a_dangling_symlink_is_named_as_one_on_a_plain_start(tmp_path, monkeypatch) -> None:
    """It used to read as a plain missing database and send the operator to a create
    command, which then refused the link: a loop (PR#405 r3)."""
    msg = _refusal(tmp_path, monkeypatch, "dangling symlink")
    assert "is a symlink to" in msg and "lost.db" in msg
    assert not (tmp_path / "lost.db").exists()


def test_offline_sql_of_the_baseline_needs_no_capability_and_touches_nothing(
        tmp_path, monkeypatch, capsys) -> None:
    """`alembic upgrade --sql` prints DDL and opens no database, so the baseline's gate
    has nothing to protect and no connection to inspect (PR#405 r3). To 0001 only:
    measured, offline past it never worked here, since the batch_alter_table rebuilds
    need a live connection to reflect from (alembic CommandError on `memberships`)."""
    db = _point_app_at(tmp_path, monkeypatch)
    command.upgrade(migrate._alembic_config(), "0001", sql=True)
    assert "CREATE TABLE users" in capsys.readouterr().out
    assert not db.exists()
