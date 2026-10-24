"""Starting never creates an island: every door to the database opens it open-only,
and ``--new-island`` is the one way a database is made.

The bad outcome this file pins: a lost or damaged ``aiko.db`` coming back as a NEW,
EMPTY island that serves (every account, message and key gone). It is not detected
here, it is impossible: no connect can create the file. So each refuse arm asserts the
refusal AND that nothing was created, and each door (the migrator, the app's engine,
lifespan's ``verify_schema``, a hand-run alembic) is driven against a missing file.
The admit arms are positive controls: an existing island must still boot, and
``--new-island`` must still build one, or a standup or every live island breaks.
"""
from __future__ import annotations

import asyncio
import sqlite3
from urllib.parse import quote

import pytest
from alembic import command
from sqlalchemy import text

from aiko_gateway import migrate
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

def test_new_island_builds_a_fresh_database_at_head(tmp_path, monkeypatch) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    migrate.run(new_island=True)
    assert {"users", "alembic_version"} <= _tables(db)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["aiko.db"], "no side files"


def test_an_existing_island_boots_without_the_flag(tmp_path, monkeypatch) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    migrate.run(new_island=True)
    _plant_canary(db, "melb")
    migrate.run()  # every ordinary start
    assert _canary(db) == ["melb"]


def test_main_routes_the_argument_and_rejects_anything_else(tmp_path, monkeypatch) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="usage"):
        migrate.main(["migrate", "--new-islnd"])
    assert not db.exists()
    migrate.main(["migrate", "--new-island"])
    assert "users" in _tables(db)
    migrate.main(["migrate"])  # and the plain start admits it


def test_a_path_with_uri_metacharacters_still_migrates(tmp_path, monkeypatch) -> None:
    """`#`, `%` and a space are legal in a DB_URL path but mean something in an SQLite
    URI: open_only must percent-encode them, and alembic's configparser must not choke
    on the resulting `%` (Noether, PR#399 r2). (`?` can't appear: DB_URL itself reads
    it as a query string.)"""
    db = _point_app_at(tmp_path, monkeypatch, name="50% off #2.db")
    migrate.run(new_island=True)
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
    migrate.run(new_island=True)
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
    migrate.run(new_island=True)
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
def test_new_island_refuses_when_anything_is_already_there(tmp_path, monkeypatch, what) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    real = tmp_path / "real.db"
    _point_app_at(tmp_path, monkeypatch, name="real.db")
    migrate.run(new_island=True)
    _plant_canary(real, "melb")
    if what == "a symlink to an island":
        db.symlink_to(real)
        _point_app_at(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_EXISTS"):
        migrate.run(new_island=True)
    assert _canary(real) == ["melb"]


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
