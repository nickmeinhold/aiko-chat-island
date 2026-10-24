"""The volume witness (design 20): only a record OUTSIDE the volume can say the volume
is the right one, and the check runs on every start, in whatever serves.

Every arm drives the real runner (``migrate.run``) or the real lifespan check
(``db.verify_schema`` / ``main.lifespan``) against a throwaway volume, like
test_volume_sentinel.py. Rows are named for design 20 v4's Q3 matrix. The admit arms
are positive controls: a predicate widened to refuse the enrolled island would
crash-loop both live islands, and only those arms would go red.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from aiko_gateway import db as db_module
from aiko_gateway import migrate
from aiko_gateway.config import settings


def _volume(tmp_path, monkeypatch, *, name: str = "aiko.db"):
    """A data dir and a witness dir, side by side, as the compose file mounts them."""
    monkeypatch.delenv("GATEWAY_WORKER_LOCK", raising=False)
    data = tmp_path / "data"
    witness = tmp_path / "witness"
    data.mkdir()
    witness.mkdir()
    db = data / name
    monkeypatch.setattr(settings, "db_url", f"sqlite+aiosqlite:///{db}")
    return db, witness


def _arm(monkeypatch, witness) -> None:
    monkeypatch.setattr(settings, "witness_dir", str(witness))


def _enroll(db, witness, *, lineage: str | None = None, db_path: str | None = None) -> str:
    """What `deploy/compose.sh enroll` writes: the tally, from the sentinel."""
    lineage = lineage if lineage is not None else migrate._sentinel_path(db).read_text().strip()
    (witness / migrate.TALLY_FILENAME).write_text(json.dumps(
        {"lineage": lineage, "db_path": db_path or str(db), "island_id": "test-island"}))
    return lineage


def _lived_island(tmp_path, monkeypatch):
    """A real island: migrated, sentinel written, enrolled, then armed."""
    db, witness = _volume(tmp_path, monkeypatch)
    migrate.run()  # unarmed (test env): provisions + writes the sentinel
    _enroll(db, witness)
    _arm(monkeypatch, witness)
    return db, witness


# --- admit arms (positive controls) ---------------------------------------------

def test_S1_the_enrolled_island_serves(tmp_path, monkeypatch) -> None:
    db, _ = _lived_island(tmp_path, monkeypatch)
    migrate.run()  # must not raise
    migrate.check_lineage(db)


def test_unset_outside_production_is_todays_behaviour(tmp_path, monkeypatch) -> None:
    db, _ = _volume(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "witness_dir", "")
    migrate.run()
    assert db.exists()


def test_disarmed_outside_production_skips_the_check(tmp_path, monkeypatch) -> None:
    db, _ = _volume(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "witness_dir", migrate.WITNESS_DISARMED)
    migrate.run()
    assert db.exists()


def test_enroll_mode_provisions_an_unenrolled_volume_then_the_tally_admits_it(
        tmp_path, monkeypatch) -> None:
    """The round trip `compose.sh enroll` performs: armed, no tally, `--enroll` runs the
    migrator; the tally is written from the sentinel it leaves; a normal start serves."""
    db, witness = _volume(tmp_path, monkeypatch)
    _arm(monkeypatch, witness)
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNENROLLED"):
        migrate.run()
    assert not db.exists(), "an unenrolled refusal must not create the db"
    migrate.main(["migrate", "--enroll"])
    assert db.exists()
    _enroll(db, witness)
    migrate.run()


def test_a_new_sentinel_names_its_island(tmp_path, monkeypatch) -> None:
    db, _ = _volume(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "island_id", "melb-test")
    migrate.run()
    stamp, island = migrate._sentinel_path(db).read_text().split()
    assert island == "melb-test"
    assert stamp.endswith("+00:00")


def test_a_timestamp_only_sentinel_still_matches_its_tally(tmp_path, monkeypatch) -> None:
    """Both live volumes carry pre-design-20 sentinels (timestamp only). They are never
    rewritten, and the lineage comparison is on the whole string."""
    db, witness = _volume(tmp_path, monkeypatch)
    migrate.run()
    old = "2026-10-20T06:01:11.810364+00:00"
    migrate._sentinel_path(db).write_text(old + "\n")
    _enroll(db, witness, lineage=old)
    _arm(monkeypatch, witness)
    migrate.run()


# --- arming: production refuses absence ----------------------------------------

@pytest.mark.parametrize("value", ["", "   ", migrate.WITNESS_DISARMED])
def test_production_refuses_an_unarmed_start(tmp_path, monkeypatch, value) -> None:
    db, _ = _volume(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "witness_dir", value)
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNWITNESSED"):
        migrate.run()
    assert not db.exists(), "the arming check must run before anything connects"


def test_production_enroll_still_runs_without_a_witness(tmp_path, monkeypatch) -> None:
    """`--enroll` skips the lineage predicate and nothing else; it is the one way a
    production volume gets its first sentinel before the tally exists."""
    db, _ = _volume(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "witness_dir", "")
    migrate.run(enrolling=True)
    assert migrate._sentinel_path(db).exists()


def test_the_enroll_flag_is_exact(tmp_path, monkeypatch) -> None:
    _volume(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        migrate.main(["migrate", "--enrol"])
    with pytest.raises(SystemExit):
        migrate.main(["migrate", "--enroll", "extra"])


# --- refuse arms, by matrix row ---------------------------------------------------

def test_S7_a_missing_tally_refuses(tmp_path, monkeypatch) -> None:
    db, witness = _lived_island(tmp_path, monkeypatch)
    (witness / migrate.TALLY_FILENAME).unlink()
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNENROLLED"):
        migrate.run()


@pytest.mark.parametrize("body", ["", "not json", "{}", '{"lineage": "", "db_path": "x", "island_id": "i"}',
                                  "[1, 2]"])
def test_S7_an_unreadable_tally_refuses(tmp_path, monkeypatch, body) -> None:
    _, witness = _lived_island(tmp_path, monkeypatch)
    (witness / migrate.TALLY_FILENAME).write_text(body)
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNENROLLED"):
        migrate.run()


def test_a_symlinked_tally_refuses(tmp_path, monkeypatch) -> None:
    db, witness = _lived_island(tmp_path, monkeypatch)
    real = tmp_path / "elsewhere.tally"
    (witness / migrate.TALLY_FILENAME).rename(real)
    os.symlink(real, witness / migrate.TALLY_FILENAME)
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNENROLLED"):
        migrate.run()


def test_a_symlinked_sentinel_refuses(tmp_path, monkeypatch) -> None:
    db, _ = _lived_island(tmp_path, monkeypatch)
    sentinel = migrate._sentinel_path(db)
    real = sentinel.with_name("real-sentinel")
    sentinel.rename(real)
    os.symlink(real, sentinel)
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_FOREIGN_LINEAGE"):
        migrate.run()


def test_S3_a_foreign_lineage_refuses_without_touching_the_db(tmp_path, monkeypatch) -> None:
    db, _ = _lived_island(tmp_path, monkeypatch)
    migrate._sentinel_path(db).write_text("2026-07-11T01:28:53+00:00 other-island\n")
    before = db.read_bytes()
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_FOREIGN_LINEAGE"):
        migrate.run()
    assert db.read_bytes() == before


@pytest.mark.parametrize("leave_db", [False, True], ids=["S2/S5b-wiped", "db-only-restore"])
def test_a_missing_sentinel_under_an_enrolled_host_refuses(tmp_path, monkeypatch, leave_db) -> None:
    """S2 (recreated empty) and S5b (wiped in place) leave no sentinel; a db restored
    WITHOUT its sentinel leaves the db and no sentinel. All three refuse, and none of
    them gets a fresh sentinel minted behind the operator's back."""
    db, _ = _lived_island(tmp_path, monkeypatch)
    migrate._sentinel_path(db).unlink()
    if not leave_db:
        db.unlink()
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNENROLLED"):
        migrate.run()
    assert not migrate._sentinel_path(db).exists()
    assert db.exists() is leave_db


def test_S6_another_db_path_refuses(tmp_path, monkeypatch) -> None:
    db, witness = _lived_island(tmp_path, monkeypatch)
    other = db.with_name("aiko2.db")
    monkeypatch.setattr(settings, "db_url", f"sqlite+aiosqlite:///{other}")
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_FOREIGN_LINEAGE"):
        migrate.run()
    assert not other.exists()


def test_S6_a_non_file_backend_refuses_when_armed(tmp_path, monkeypatch) -> None:
    _lived_island(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "db_url", "sqlite+aiosqlite:///:memory:")
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_FOREIGN_LINEAGE"):
        migrate.run()


# --- the serving layer: nothing serves without the check ------------------------

def test_verify_schema_refuses_a_foreign_lineage_before_connecting(tmp_path, monkeypatch) -> None:
    """The migrator runs from the image's CMD, so `compose run chat-island uvicorn …`
    skips it. The app's own startup must refuse by itself (design 20 v4, T3-1)."""
    db, _ = _lived_island(tmp_path, monkeypatch)
    migrate._sentinel_path(db).write_text("someone else\n")

    class _NoConnect:
        def connect(self, *a, **k):
            raise AssertionError("verify_schema connected before checking the lineage")

    monkeypatch.setattr(db_module, "engine", _NoConnect())
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_FOREIGN_LINEAGE"):
        asyncio.run(db_module.verify_schema())


def test_verify_schema_refuses_an_unarmed_production_start(tmp_path, monkeypatch) -> None:
    _volume(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "witness_dir", "")
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNWITNESSED"):
        asyncio.run(db_module.verify_schema())


def test_the_app_lifespan_refuses_without_the_migrator(tmp_path, monkeypatch) -> None:
    """Boot the real app (lifespan) with NO migrator run first, on a mismatched tally:
    the shape of `compose run chat-island uvicorn aiko_gateway.main:app`."""
    from aiko_gateway import main

    db, witness = _lived_island(tmp_path, monkeypatch)
    _enroll(db, witness, lineage="not this volume")
    async def _boot() -> None:
        async with main.lifespan(main.app):
            pass

    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_FOREIGN_LINEAGE"):
        asyncio.run(_boot())

