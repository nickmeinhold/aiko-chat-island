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


def test_a_volume_with_no_file_backend_is_untouched(monkeypatch) -> None:
    monkeypatch.setattr(settings, "db_url", "sqlite+aiosqlite:///:memory:")
    assert migrate._db_file() is None
    monkeypatch.setattr(settings, "db_url", "postgresql+asyncpg://u@h/db")
    assert migrate._db_file() is None


# --- refuse arms ---------------------------------------------------------------

@pytest.mark.parametrize("evidence", [migrate.SENTINEL_FILENAME, LOCK_FILENAME])
def test_lived_volume_with_a_missing_db_refuses_WITHOUT_creating_one(
        tmp_path, monkeypatch, evidence) -> None:
    """The LOCK case is the bridge: both live islands carry the worker lock from
    before the sentinel existed. The assert that no file appeared is the point:
    a check placed after the first connect would watch a file SQLite just made."""
    db = _point_app_at(tmp_path, monkeypatch)
    (tmp_path / evidence).touch()
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_MISSING_DB") as refused:
        migrate.run()
    assert "is missing" in str(refused.value)
    assert evidence in str(refused.value)
    assert not db.exists(), "the refusal must not create the db it refused over"


def test_lived_volume_with_a_zero_byte_db_refuses(tmp_path, monkeypatch) -> None:
    """What a uvicorn-only boot leaves behind: verify_schema()'s connect creates an
    empty file, refuses UNMANAGED, and the next real boot finds a file that exists."""
    db = _point_app_at(tmp_path, monkeypatch)
    migrate.run()
    db.unlink()
    db.touch()
    with pytest.raises(RuntimeError, match="has no tables"):
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
