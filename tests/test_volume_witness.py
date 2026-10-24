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
    from aiko_gateway.domain.peers_service import self_island_id

    lineage = lineage if lineage is not None else migrate._sentinel_path(db).read_text().strip()
    (witness / migrate.TALLY_FILENAME).write_text(json.dumps(
        {"lineage": lineage, "db_path": db_path or str(db), "island_id": self_island_id(settings)}))
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


def test_production_enroll_runs_with_the_witness_mounted_and_no_tally(tmp_path, monkeypatch) -> None:
    """`--enroll` skips the lineage predicate and nothing else; with the witness dir
    mounted and empty it is how a production volume gets its first sentinel."""
    db, witness = _volume(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "environment", "production")
    _arm(monkeypatch, witness)
    migrate.run(enrolling=True)
    assert migrate._sentinel_path(db).exists()


@pytest.mark.parametrize("value", ["", migrate.WITNESS_DISARMED])
def test_production_enroll_without_the_witness_refuses(tmp_path, monkeypatch, value) -> None:
    """Unmounted is not unenrolled: it means nobody looked (Tesla, PR#404 r2)."""
    db, _ = _volume(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "witness_dir", value)
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNWITNESSED"):
        migrate.run(enrolling=True)
    assert not db.exists()


def test_the_container_naming_itself_another_island_refuses(tmp_path, monkeypatch) -> None:
    """The third terminal: the right volume must not serve under another island's
    name, and ISLAND_ID is host-forwarded config (Tesla, PR#404 r2)."""
    _lived_island(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "island_id", "some-other-island")
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_FOREIGN_LINEAGE"):
        migrate.run()


@pytest.mark.parametrize("shape", ["zero-byte", "directory", "symlink"])
def test_a_db_that_is_not_a_database_refuses_in_one_voice(tmp_path, monkeypatch, shape) -> None:
    """The db is typed like the records: a non-empty regular file, not followed through
    a link. Both entrances refuse as MISSING_DB, and the app takes no lock (Tesla r2)."""
    db, _ = _lived_island(tmp_path, monkeypatch)
    real = db.with_name("real.db")
    db.rename(real)
    if shape == "zero-byte":
        db.touch()
    elif shape == "directory":
        db.mkdir()
    else:
        os.symlink(real, db)
    # A link is resolved by the URL->path derivation (sqlite_file), so it refuses
    # earlier and more precisely: "this container opens real.db, but this host enrolled
    # aiko.db". The other two shapes reach the db typing and refuse as MISSING_DB.
    code = "MIGRATE_REFUSE_FOREIGN_LINEAGE" if shape == "symlink" else "MIGRATE_REFUSE_MISSING_DB"
    with pytest.raises(RuntimeError, match=code):
        migrate.run()
    with pytest.raises(RuntimeError, match=code):
        _boot_app_without_the_migrator(monkeypatch)
    _assert_volume_untouched(db)


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

class _NoConnect:
    def connect(self, *a, **k):
        raise AssertionError("the app connected before the volume witness refused")


def _boot_app_without_the_migrator(monkeypatch) -> None:
    """Boot the real app (lifespan) with NO migrator run first: the shape of
    `compose run chat-island uvicorn aiko_gateway.main:app`, which replaces the image's
    CMD (design 20 v4, T3-1). The engine is a stub that fails if touched."""
    from aiko_gateway import main

    monkeypatch.setattr(db_module, "engine", _NoConnect())

    async def _boot() -> None:
        async with main.lifespan(main.app):
            pass

    asyncio.run(_boot())


def _assert_volume_untouched(db) -> None:
    """A refused boot must leave no trace: the worker lock is itself lived evidence
    (`_lived_evidence`), so creating it on a refused volume manufactures the witness
    the check protects (Maxwell, PR#404 r1, measured)."""
    from aiko_gateway.worker_guard import LOCK_FILENAME

    assert not (db.parent / LOCK_FILENAME).exists(), "a refused boot created the worker lock"


@pytest.mark.parametrize("state", ["foreign", "unenrolled-empty", "db-missing"])
def test_the_app_refuses_before_the_lock_and_before_connecting(tmp_path, monkeypatch, state) -> None:
    db, witness = _volume(tmp_path, monkeypatch)
    if state == "unenrolled-empty":
        _enroll(db, witness, lineage="an island that is not on this volume")
        expected = "MIGRATE_REFUSE_UNENROLLED"
    else:
        migrate.run()  # provision + sentinel, unarmed
        _enroll(db, witness)
        if state == "foreign":
            migrate._sentinel_path(db).write_text("someone else\n")
            expected = "MIGRATE_REFUSE_FOREIGN_LINEAGE"
        else:
            db.unlink()
            expected = "MIGRATE_REFUSE_MISSING_DB"
    _arm(monkeypatch, witness)
    with pytest.raises(RuntimeError, match=expected):
        _boot_app_without_the_migrator(monkeypatch)
    _assert_volume_untouched(db)
    if state == "db-missing":
        # The lifespan engine CREATES on connect; refusing earlier is what keeps
        # absence honest (Tesla, PR#404 r1).
        assert not db.exists(), "a refused boot gave birth to an empty db"


def test_the_app_refuses_an_unarmed_production_start(tmp_path, monkeypatch) -> None:
    db, _ = _volume(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "witness_dir", "")
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNWITNESSED"):
        _boot_app_without_the_migrator(monkeypatch)
    _assert_volume_untouched(db)


# --- PR#404 round 1: what a file on an untrusted volume can do to the reader ------

@pytest.mark.parametrize("which", ["sentinel", "tally"])
def test_a_padded_record_is_not_a_prefix_match(tmp_path, monkeypatch, which) -> None:
    """The comparison is on the WHOLE record. A matching first line followed by
    padding past any read buffer and then other bytes must not pass (Carnot r1)."""
    db, witness = _lived_island(tmp_path, monkeypatch)
    path = migrate._sentinel_path(db) if which == "sentinel" else witness / migrate.TALLY_FILENAME
    path.write_text(path.read_text() + " " * migrate.WITNESS_RECORD_MAX + "tail")
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_"):
        migrate.run()


@pytest.mark.parametrize("which", ["sentinel", "tally"])
def test_a_fifo_record_refuses_instead_of_hanging(tmp_path, monkeypatch, which) -> None:
    """`open(O_RDONLY)` on a FIFO with no writer blocks forever; startup would never
    reach its refusal (Carnot r1). O_NONBLOCK + S_ISREG on the opened fd."""
    db, witness = _lived_island(tmp_path, monkeypatch)
    path = migrate._sentinel_path(db) if which == "sentinel" else witness / migrate.TALLY_FILENAME
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_"):
        migrate.run()


def test_a_sentinel_that_is_not_utf8_refuses_in_the_refusal_vocabulary(tmp_path, monkeypatch) -> None:
    db, _ = _lived_island(tmp_path, monkeypatch)
    migrate._sentinel_path(db).write_bytes(b"\xff\xfe not text")
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_FOREIGN_LINEAGE"):
        migrate.run()


def test_enroll_refuses_over_an_existing_tally(tmp_path, monkeypatch) -> None:
    """`--enroll` creates a FIRST enrolment only. Over a tally it would skip the check
    that calls the volume foreign, then migrate it (Tesla r1)."""
    db, _ = _lived_island(tmp_path, monkeypatch)
    migrate._sentinel_path(db).write_text("someone else\n")
    before = db.read_bytes()
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_ENROLLED"):
        migrate.main(["migrate", "--enroll"])
    assert db.read_bytes() == before


def test_a_planted_tmp_symlink_cannot_truncate_the_db(tmp_path, monkeypatch) -> None:
    """The sentinel's temp file is CREATED (O_EXCL|O_NOFOLLOW), never opened: a planted
    `.aiko.db.initialized.tmp -> aiko.db` must not pour a timestamp into the db (Tesla r1)."""
    db, witness = _volume(tmp_path, monkeypatch)
    migrate.run()  # a real island, unarmed
    migrate._sentinel_path(db).unlink()  # db-only state: the next run adopts and writes
    before = db.read_bytes()
    tmp = migrate._sentinel_path(db).with_name(migrate._sentinel_path(db).name + ".tmp")
    os.symlink(db, tmp)
    migrate.run()
    assert db.read_bytes() == before, "the sentinel write went through the planted link"
    assert migrate._sentinel_path(db).read_text().strip()
    assert not os.path.islink(migrate._sentinel_path(db))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file; EACCES can't be produced")
@pytest.mark.parametrize("which", ["sentinel", "tally"])
def test_an_unreadable_record_says_so_and_does_not_send_you_to_enrol(tmp_path, monkeypatch, which) -> None:
    """A permissions/mount/disk failure is not a foreign island. Its refusal must not
    recommend `enroll --replace-lineage`, which would bless an unread volume (Noether r1)."""
    db, witness = _lived_island(tmp_path, monkeypatch)
    path = migrate._sentinel_path(db) if which == "sentinel" else witness / migrate.TALLY_FILENAME
    path.chmod(0)
    try:
        with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_WITNESS_UNREADABLE") as e:
            migrate.run()
        assert "Do NOT re-enrol" in str(e.value)
        assert "--replace-lineage" not in str(e.value)
    finally:
        path.chmod(0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches a mode-000 dir; EACCES can't be produced")
def test_enroll_over_a_tally_it_cannot_see_still_refuses(tmp_path, monkeypatch) -> None:
    """`os.path.lexists` says False when it CAN'T LOOK. A real tally inside an
    unsearchable witness dir must not read as "no enrolment", or `--enroll` skips the
    lineage check and migrates whatever is mounted (Maxwell, PR#404 r2, measured)."""
    db, witness = _lived_island(tmp_path, monkeypatch)
    before = db.read_bytes()
    witness.chmod(0)
    try:
        with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_WITNESS_UNREADABLE"):
            migrate.main(["migrate", "--enroll"])
    finally:
        witness.chmod(0o755)
    assert db.read_bytes() == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches a mode-000 dir; EACCES can't be produced")
def test_evidence_that_cannot_be_looked_at_is_not_no_evidence(tmp_path, monkeypatch) -> None:
    """The class, at the pre-existing sentinel site: `_lived_evidence` once trusted
    `.exists()` to raise on EACCES (its comment said so). It returns False instead."""
    db, _ = _volume(tmp_path, monkeypatch)
    migrate.run()  # unarmed: a real island
    real = migrate._sentinel_path(db)
    monkeypatch.setattr(migrate, "_sentinel_path", lambda d: tmp_path / "locked" / real.name)
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked").chmod(0)
    try:
        with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_WITNESS_UNREADABLE"):
            migrate._lived_evidence(db)
    finally:
        (tmp_path / "locked").chmod(0o755)


def test_the_new_island_remedy_goes_through_enrolment_on_an_enrolled_host(tmp_path, monkeypatch) -> None:
    """Deleting the volume's records is not enough where a tally exists: the next start
    would refuse as unenrolled, so the advice must name enrolment (Carnot, PR#404 r2)."""
    db, _ = _lived_island(tmp_path, monkeypatch)  # armed
    msg = migrate.refuse_missing_db_message(db, [migrate._sentinel_path(db)], "is missing")
    assert "enroll --replace-lineage" in msg
    monkeypatch.setattr(settings, "witness_dir", "")
    assert "enroll" not in migrate.refuse_missing_db_message(db, [migrate._sentinel_path(db)], "is missing")


def test_the_serving_engine_never_creates_the_db_in_production(tmp_path, monkeypatch) -> None:
    """The app is not what creates databases; the migrator is. In production the
    serving URL is non-creating, so a db that vanishes after the witness looked makes
    the connect fail instead of birthing an empty file: the window is removed, not
    guarded (Carnot, PR#404 r2)."""
    from sqlalchemy import text

    db, _ = _volume(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "environment", "production")
    url = db_module.serving_url()
    assert "mode=rw" in url
    engine = db_module.make_engine(url)

    async def _connect() -> None:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    with pytest.raises(Exception):
        asyncio.run(_connect())
    assert not db.exists(), "the serving engine created a database"
    monkeypatch.setattr(settings, "environment", "test")
    assert db_module.serving_url() == settings.db_url


def test_compose_pins_the_witness_as_a_literal_read_only_directory() -> None:
    """WITNESS_DIR is excluded from the forwarding invariant, which also removes it from
    every value check there, so THIS test is what pins its spelling. A `${WITNESS_DIR…}`
    interpolation would let a host `.env` or shell disarm or redirect the check
    (Noether, PR#404 r1)."""
    import yaml
    from pathlib import Path

    from test_config import _chat_island_environment

    assert _chat_island_environment()["WITNESS_DIR"] == "/witness"
    compose = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text())
    volumes = compose["services"]["chat-island"]["volumes"]
    assert "./witness:/witness:ro" in volumes, volumes


def test_a_link_planted_after_the_cleanup_still_cannot_truncate_the_db(tmp_path, monkeypatch) -> None:
    """The race the stale-tmp unlink can't close: a link that appears between the
    unlink and the open. Simulated by skipping the unlink. O_CREAT|O_EXCL|O_NOFOLLOW
    must then refuse to write rather than follow the link into aiko.db."""
    db, _ = _volume(tmp_path, monkeypatch)
    migrate.run()
    migrate._sentinel_path(db).unlink()
    before = db.read_bytes()
    tmp = migrate._sentinel_path(db).with_name(migrate._sentinel_path(db).name + ".tmp")
    os.symlink(db, tmp)
    real_present = migrate._present
    monkeypatch.setattr(migrate, "_present",
                        lambda p: False if str(p) == str(tmp) else real_present(p))
    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNRECORDED"):
        migrate.run()
    assert db.read_bytes() == before, "the sentinel write followed a link into the db"

