"""The volume sentinel (design 18 §2): a volume that has hosted an island never
silently becomes a new, empty one.

Every arm drives the real entrypoint runner (``migrate.run``) against a throwaway
file DB, like test_migrations.py. The admit arms are positive controls: a refusal
widened to fire on a fresh volume, or on an existing db, would crash-loop a new
standup or every live island, and only these would go red. Both fixes the refusal
prescribes are run FROM the refusing state, so the message cannot become advice
that can't work (#393).
"""
from __future__ import annotations

import shutil
import sqlite3

import pytest

from aiko_gateway import migrate
from aiko_gateway.config import settings
from aiko_gateway.worker_guard import LOCK_FILENAME


def _point_app_at(tmp_path, monkeypatch):
    db = tmp_path / "aiko.db"
    monkeypatch.setattr(settings, "db_url", f"sqlite+aiosqlite:///{db}")
    return db


def _plant_canary(db, value: str) -> None:
    """A row only THIS island's db holds, to prove which file came back."""
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE IF NOT EXISTS _canary (v TEXT)")
        c.execute("INSERT INTO _canary VALUES (?)", (value,))


def _canary(db) -> list[str]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
        return [r[0] for r in c.execute("SELECT v FROM _canary")]


def _sentinel(db):
    return db.parent / migrate.SENTINEL_FILENAME


# --- admit arms (positive controls) -------------------------------------------

def test_fresh_volume_is_admitted_and_gets_a_sentinel(tmp_path, monkeypatch) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    assert not any(tmp_path.iterdir())
    migrate.run()
    assert db.exists() and _sentinel(db).exists()
    assert _sentinel(db).read_text().startswith("aiko.db ")


def test_existing_island_without_a_sentinel_is_adopted(tmp_path, monkeypatch) -> None:
    """Both live islands at the first boot of this image: a managed db, the worker
    lock, no sentinel. Admitted, data untouched, sentinel written."""
    db = _point_app_at(tmp_path, monkeypatch)
    migrate.run()
    _plant_canary(db, "melb")
    _sentinel(db).unlink()
    (tmp_path / LOCK_FILENAME).touch()
    migrate.run()
    assert _sentinel(db).exists()
    assert _canary(db) == ["melb"]


def test_reboot_at_head_keeps_the_original_sentinel(tmp_path, monkeypatch) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    migrate.run()
    first = _sentinel(db).read_text()
    migrate.run()
    assert _sentinel(db).read_text() == first


def test_a_backend_with_no_file_runs_and_writes_nothing(tmp_path, monkeypatch) -> None:
    """Drives run(), not just _db_file(): a refusal or a sentinel write widened to a
    non-file backend goes red here (Noether, PR#399 r1)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "db_url", "sqlite+aiosqlite:///:memory:")
    assert migrate._db_file() is None
    migrate.run()
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(settings, "db_url", "postgresql+asyncpg://u@h/db")
    assert migrate._db_file() is None


def test_lock_filename_is_the_file_worker_guard_creates(tmp_path, monkeypatch) -> None:
    """The bridge only works while the two agree on the name and the directory."""
    from aiko_gateway import worker_guard
    db = _point_app_at(tmp_path, monkeypatch)
    monkeypatch.delenv("GATEWAY_WORKER_LOCK", raising=False)
    assert worker_guard._lock_path() == db.parent / LOCK_FILENAME


def test_a_sentinel_write_failure_does_not_stop_a_migrated_island(tmp_path, monkeypatch) -> None:
    """Recorded by patching the logger, not via caplog: alembic's env.py runs
    fileConfig() during the upgrade, which swaps the root handlers (caplog's among
    them). In production the ERROR still reaches stderr: alembic.ini's root is WARNING."""
    _point_app_at(tmp_path, monkeypatch)
    errors: list[str] = []
    monkeypatch.setattr(migrate.log, "error", lambda msg, *a: errors.append(msg % a))

    def refuse(_db):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(migrate, "_write_sentinel", refuse)
    migrate.run()  # must NOT raise: the schema is at head
    import asyncio
    assert asyncio.run(migrate._schema_status()).state is migrate.SchemaState.HEAD
    assert errors and "Could not record" in errors[0]


def test_creating_a_fresh_db_says_so(tmp_path, monkeypatch, caplog) -> None:
    """'Fresh volume' and 'no evidence found' must not be the same silence."""
    import logging
    _point_app_at(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="aiko_gateway.migrate"):
        migrate.run()
    assert "creating a NEW, empty database" in caplog.text


def test_a_sentinel_for_another_db_is_not_evidence(tmp_path, monkeypatch) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    _sentinel(db).write_text("aiko_dev.db 2026-10-20T00:00:00+00:00\n")
    migrate.run()  # this db was never created here: admitted as fresh
    assert db.exists()


# --- refuse arms ---------------------------------------------------------------

@pytest.mark.parametrize("evidence", [migrate.SENTINEL_FILENAME, LOCK_FILENAME])
def test_lived_volume_with_a_missing_db_refuses_WITHOUT_creating_one(
        tmp_path, monkeypatch, evidence) -> None:
    """The LOCK case is the bridge: both live islands carry the worker lock from
    before the sentinel existed. The assert that no file appeared is the point:
    a check placed after the first connect would watch a file SQLite just made."""
    db = _point_app_at(tmp_path, monkeypatch)
    (tmp_path / evidence).write_text(f"{db.name} 2026-10-20T00:00:00+00:00\n")
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_MISSING_DB") as refused:
        migrate.run()
    assert "is missing" in str(refused.value)
    assert evidence in str(refused.value)
    assert not db.exists(), "the refusal must not create the db it refused over"


def test_a_moved_worker_lock_still_counts(tmp_path, monkeypatch) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    moved = tmp_path / "elsewhere" / "gw.lock"
    moved.parent.mkdir()
    moved.touch()
    monkeypatch.setenv("GATEWAY_WORKER_LOCK", str(moved))
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_MISSING_DB") as refused:
        migrate.run()
    assert str(moved) in str(refused.value)
    assert not db.exists()


def test_a_db_that_vanishes_AFTER_the_check_is_not_reborn(tmp_path, monkeypatch) -> None:
    """The race the existence check alone can't close: the file goes between the
    check and the first connect. On a lived volume that connect is mode=rw, so it
    fails instead of creating (Carnot + Tesla, PR#399 r1)."""
    from sqlalchemy.exc import OperationalError
    db = _point_app_at(tmp_path, monkeypatch)
    migrate.run()
    real = migrate._table_names

    async def vanish_then_connect():
        db.unlink()
        return await real()
    monkeypatch.setattr(migrate, "_table_names", vanish_then_connect)
    with pytest.raises(OperationalError, match="unable to open"):
        migrate.run()
    assert not db.exists(), "a non-creating connect must not birth the file"
    assert settings.db_url.endswith("aiko.db"), "the mode=rw URL must not leak out of run()"


def test_lived_volume_with_only_alembic_bookkeeping_refuses(tmp_path, monkeypatch) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    migrate.run()
    db.unlink()
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    with pytest.raises(RuntimeError, match="holds no island"):
        migrate.run()


def test_lived_volume_with_a_zero_byte_db_refuses(tmp_path, monkeypatch) -> None:
    """What a uvicorn-only boot leaves behind: verify_schema()'s connect creates an
    empty file, refuses UNMANAGED, and the next real boot finds a file that exists."""
    db = _point_app_at(tmp_path, monkeypatch)
    migrate.run()
    db.unlink()
    db.touch()
    with pytest.raises(RuntimeError, match="holds no island"):
        migrate.run()
    assert db.stat().st_size == 0, "refused without building a schema into it"


# --- both prescribed fixes, run from the refusing state -------------------------

def test_fix_restore_the_backup_brings_the_same_island_back(tmp_path, monkeypatch) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    migrate.run()
    _plant_canary(db, "the island")
    backup = tmp_path / "backups" / "aiko.db.preupdate-1"
    backup.parent.mkdir()
    shutil.copy(db, backup)
    db.unlink()
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_MISSING_DB") as refused:
        migrate.run()
    assert "restore the newest backup" in str(refused.value)
    shutil.copy(backup, db)  # the fix, as the message states it
    migrate.run()
    assert _canary(db) == ["the island"]


def test_fix_delete_the_evidence_starts_a_deliberately_new_island(
        tmp_path, monkeypatch) -> None:
    db = _point_app_at(tmp_path, monkeypatch)
    migrate.run()
    (tmp_path / LOCK_FILENAME).touch()
    db.unlink()
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_MISSING_DB") as refused:
        migrate.run()
    for name in (migrate.SENTINEL_FILENAME, LOCK_FILENAME):
        assert str(tmp_path / name) in str(refused.value)
        (tmp_path / name).unlink()  # the fix, as the message states it
    migrate.run()
    assert db.exists() and _sentinel(db).exists()
