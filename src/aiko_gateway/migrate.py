"""Bring the database to head — the container entrypoint runs this BEFORE uvicorn.

Why an explicit runner (not just `alembic upgrade head`): the gateway adopted
alembic AFTER a live DB already existed, built by the old ``create_all`` path.
That live DB has every table but no ``alembic_version`` row, so a naive
``upgrade head`` would run the baseline's ``CREATE TABLE users`` against a DB that
already has it and abort. The fix is **stamp-or-upgrade**:

  * fresh DB (no ``alembic_version``, no ``users``)      → ``upgrade head`` builds it
  * pre-alembic DB (``users`` present, no ``alembic_version``) → adopt: VERIFY the
        existing schema matches the baseline, then ``stamp 0001``, then
        ``upgrade head`` applies anything after it
  * already-managed DB (``alembic_version`` present)     → ``upgrade head`` only
  * DB stamped at a revision UNKNOWN to this image (``alembic_version`` names a
        revision this image's ``alembic/versions/`` does not contain) → REFUSE to
        start

**Compat guard: an unknown revision refuses to start (design 18 §1).** The islands
roll back by re-pinning an OLDER image onto the SAME volume, and the volume keeps
whatever revision the newer image migrated it to. PR#116 made this runner skip and
"serve" on that schema (``MIGRATE_SKIP_UNKNOWN_REVISION``), but it never actually
served: ``db._assert_at_head`` (PR#23, run by ``verify_schema()`` in lifespan) refuses
any revision that is not this code's head, so a rolled-back image crash-looped at
startup with advice that could not work ("run ``alembic upgrade head``", which dies on
"Can't locate revision"). Measured 2026-10-10 (PR#392 review). The refusal now happens
HERE, before uvicorn, with the right diagnosis and the right fix: restore the
pre-update database backup, then start the old image. There is deliberately no
serve-anyway override: the one PR#116 added was unreachable, so removing it changes
nothing that ran, and a real one would need ONE predicate shared with
``_assert_at_head`` plus CI proof that the schema is backward-compatible (design 18's
deferred backward floors).

**"Unknown to this image" is NOT the same set as "ahead of head" (Wu cage-match,
PR#116).** Image rollback is the usual cause, but the same detector fires on a
squashed/removed past revision (routine alembic history hygiene) and on a corrupt
``version_num``. In those the volume is behind or garbage, not ahead. Refusing is the
right answer for all three: under serve-anyway they pinned the volume below head
*silently and permanently*. **If you ever squash or rebase migration history, stamp
every live volume to the new baseline FROM THE NEW IMAGE, after its ``compare_metadata``
shows the volume's schema equals that baseline** (the adopt path's discipline), or those
volumes will refuse to boot. Never stamp from a refusing image: stamping its head onto a
newer schema makes ``schema_status`` read HEAD and the guard never fires again (Tesla,
PR#392 r4). The refusal message deliberately does not suggest stamping.
(This cannot co-occur with the adopt path: adopting means there is no
``alembic_version`` at all, hence no unknown revision to find.)

**The adoption is fail-closed (Carnot cage-match, PR#23).** Stamping is an
assertion that "the schema on disk already equals revision 0001". We do NOT take
that on faith from the mere presence of a ``users`` table — a DB that is missing a
table, or has a half-applied/partial schema, would otherwise be falsely marked
"current" and, with ``create_all`` gone, never repaired. So before stamping we run
alembic's own metadata comparison against the ORM and REFUSE to stamp on any
drift, surfacing the diff for an operator. (The one pre-alembic DB that exists —
prod — was verified table-for-table to match the baseline before this shipped;
this check encodes that verification so a future ambiguous DB fails loud instead
of being silently corrupted.)

**A volume that has hosted an island never silently becomes a new one (design 18
§2).** SQLite creates a missing file on the first connect, and a fresh file is
indistinguishable from a brand-new island, so a lost or half-restored ``aiko.db``
used to come back as an EMPTY island that served (every account, message and key
gone, `restart: always` making it permanent). The migrator now records a sentinel,
``.aiko.db.initialized`` (one per db) beside it, after every successful run: on a fresh volume
and, the first time a new image meets an existing db, as adoption. When the volume
shows it has lived (that sentinel, or ``worker_guard``'s ``.aiko-worker.lock``,
which every gateway since Aug 2026 has left there and never deletes, so both live
islands are covered before their first adoption), a missing db or one with no tables
REFUSES (``MIGRATE_REFUSE_MISSING_DB``). The missing-file check runs BEFORE the
first connect, and on a lived volume every later connect is non-creating
(``mode=rw``), so no window exists in which one could birth a new file. A db with
no ``users`` table also refuses: that catches the 0-byte file a uvicorn-only boot
leaves behind and a db holding only alembic's bookkeeping. A dotfile survives
``rm /data/*``, which is the point. Non-file backends (Postgres dev, in-memory) have
no volume and are unaffected.

There is no host orchestrator to sequence "migrate before boot" (the deploy is a
manual ``docker compose up -d`` — see aiko-chat-island#19), so this MUST run in
the container entrypoint. It fails closed: any error propagates a non-zero exit
and the entrypoint never starts uvicorn on an unmigrated schema.

Concurrency note: the deploy runs a single gateway replica, so exactly one
migrator touches the (single-writer) SQLite file at a time. The adoption logic is
NOT safe against two migrators racing one fresh SQLite file (SQLite DDL is not
fully transactional); if this service is ever scaled, gate migrations behind a
one-shot init container or an explicit lock rather than the per-replica entrypoint.
"""
from __future__ import annotations

import asyncio
import errno
import json
import logging
import sys
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, create_engine, inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from .config import settings
from .db import non_creating_url
from .domain import models  # noqa: F401 — register tables on Base.metadata
from .worker_guard import LOCK_FILENAME, _lock_path, sqlite_file

log = logging.getLogger("aiko_gateway.migrate")

BASELINE_REVISION = "0001"
SENTINEL_SUFFIX = ".initialized"


def _sentinel_path(db: Path) -> Path:
    """One record PER DATABASE, named for it (``.aiko.db.initialized``): a record
    for another db beside it is simply a different file, so it can neither count as
    this db's evidence nor block this db's own record (Carnot + Noether + Tesla,
    PR#399 r2). The leading dot keeps it out of ``rm aiko.db*`` and ``rm /data/*``."""
    return db.parent / f".{db.name}{SENTINEL_SUFFIX}"


def _db_file() -> Path | None:
    """The SQLite file behind ``settings.db_url``, or None for a backend with no
    volume (Postgres dev, in-memory). Same derivation as ``worker_guard``."""
    return sqlite_file(settings.db_url)


def _lived_evidence(db: Path) -> list[Path]:
    """The files that prove an island has lived on this volume. Never connects.

    The sentinel is per database (see ``_sentinel_path``), so existence is the whole
    test: nothing is parsed, so nothing can fail to parse. The worker lock counts at its
    default path and, when ``GATEWAY_WORKER_LOCK`` moves it, at the moved path too.
    (A ``GATEWAY_ALLOW_MULTIWORKER`` gateway writes no lock at all; such a volume is
    covered by the sentinel only, after its first successful run.)"""
    found: list[Path] = []
    sentinel = _sentinel_path(db)
    # `_present`, not `.exists()`: `.exists()` answers False when it can't look, which
    # here would mean "no evidence" (the old comment claimed EACCES raised; measured
    # false, PR#404 r2).
    if _present(sentinel):
        found.append(sentinel)
    for lock in {db.parent / LOCK_FILENAME, _lock_path()}:
        if lock is not None and _present(lock):
            found.append(lock)
    return found


def _non_creating_url(db: Path) -> str:
    """``settings.db_url`` reopened with ``mode=rw`` (see ``db.non_creating_url``). On a
    lived volume every connect the migrator makes (its own and alembic's, which reads
    ``settings.db_url``) goes through this, so a db that vanishes after the existence
    check makes the next connect FAIL, not birth an empty island (Carnot + Tesla,
    PR#399 r1)."""
    return non_creating_url(settings.db_url, db)


def refuse_missing_db_message(db: Path, evidence: list[Path], why: str) -> str:
    """Both fixes below are exercised from the refusing state by the test suite
    (test_volume_sentinel.py), so neither is advice that can't work (#393)."""
    names = " ".join(str(p) for p in evidence)
    raw = settings.witness_dir.strip()
    # On an enrolled host the tally still names the old island after the volume's
    # records are deleted, so the next start would refuse as unenrolled: the "new
    # island" fix has to go through enrolment there (Carnot, PR#404 r2).
    then = ("then run `deploy/compose.sh enroll --replace-lineage <island_id>` (this "
            "host has a volume tally, and only enrolment may record a new island)"
            if raw and raw != WITNESS_DISARMED else "then start")
    return (
        f"MIGRATE_REFUSE_MISSING_DB: the database {db} {why}, but this volume has "
        f"hosted an island before ({names}). Starting would create a NEW, EMPTY island "
        "here and serve it: every account, message and key gone. Refusing to start. "
        "If the database was lost or damaged: take the stack down, restore the newest "
        f"backup to {db} (`deploy/update.sh` keeps them in backups/ beside "
        "docker-compose.yml; see docs/runbooks/backup-and-rollback.md), then start. "
        "Only if you really intend a brand-new, empty island on this volume: take the "
        "stack down first (`deploy/compose.sh down`; the worker lock is held by any "
        "gateway still running, and deleting a held lock lets a second gateway start "
        f"beside it), then delete {names}, {then}.")


# --- the lineage predicate (design 20) -------------------------------------------
#
# A record INSIDE the volume (the sentinel) proves the volume has lived. Only a record
# OUTSIDE it (the host's tally, mounted read-only at WITNESS_DIR) proves the volume
# belongs to this host's island, and then only its LINEAGE: a stale copy of this
# island is indistinguishable from a legitimate restore, and serves by design.
#
# Lineage bytes can be rewritten in place under a stopped container, and
# `restart: always` restarts it with no wrapper in the path, so this is a START-time
# fact: it is checked by whatever serves, on every start. ONE function, called from
# BOTH `run()` (the entrypoint) and lifespan's `verify_schema()`, because the
# entrypoint is the image's CMD and any `run <command>` replaces it (design 20 v4,
# T3-1); the `schema_status` pattern, for the same reason.

TALLY_FILENAME = "volume.tally"
WITNESS_DISARMED = "disarmed"


@dataclass(frozen=True)
class Tally:
    lineage: str   # the sentinel's full content, stripped
    db_path: str   # the db file this island serves from, as the container sees it
    island_id: str


def _witness_dir() -> Path | None:
    """The armed witness directory, or None when the check is off. Off only in a
    non-production environment (unset, or the explicit ``disarmed``). In production,
    absence REFUSES: an unset variable is never permission (design 16's empty-pin
    trap; same posture as ``jwt_secret``)."""
    raw = settings.witness_dir.strip()
    if raw and raw != WITNESS_DISARMED:
        return Path(raw)
    if settings.is_production:
        why = ("is set to 'disarmed', which only a non-production environment may do"
               if raw == WITNESS_DISARMED else "is not set")
        raise RuntimeError(
            f"MIGRATE_REFUSE_UNWITNESSED: WITNESS_DIR {why}, so nothing outside this "
            "volume can say it is the right one. Refusing to start. The compose file "
            "mounts the deploy dir's witness/ read-only at /witness and sets "
            "WITNESS_DIR=/witness; a container created without them must be recreated "
            "from the current compose (`deploy/compose.sh up -d`). See "
            "docs/design/20-what-a-volume-can-witness.md.")
    return None


WITNESS_RECORD_MAX = 4096  # a tally or sentinel is one short line; anything bigger is not one


def _read_text_nofollow(path: Path) -> str:
    """Read a small witness record, or raise. Every way a file on an untrusted volume
    could make the read lie or hang is refused, not tolerated (PR#404 r1):
    a symlink (``O_NOFOLLOW``, required, never defaulted to 0 where absent); a FIFO or
    device, which would block ``open`` forever (``O_NONBLOCK`` + ``S_ISREG`` on the
    OPENED descriptor, not a path check that a swap could race); a record longer than
    ``WITNESS_RECORD_MAX`` (a prefix match is not a whole-string match); and bytes that
    aren't UTF-8. ``ValueError``/``OSError`` are the only things this raises."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{path} is not a regular file")
        data = b""
        while len(data) <= WITNESS_RECORD_MAX:  # a short read is not EOF; read to EOF
            chunk = os.read(fd, WITNESS_RECORD_MAX + 1 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > WITNESS_RECORD_MAX:
            raise ValueError(f"{path} is longer than {WITNESS_RECORD_MAX} bytes")
        return data.decode("utf-8")
    finally:
        os.close(fd)


def _unenrolled(why: str) -> RuntimeError:
    return RuntimeError(
        f"MIGRATE_REFUSE_UNENROLLED: {why}. Refusing to start. With the stack down, "
        "run `deploy/compose.sh enroll`: it shows which island is on this volume and "
        "asks you to confirm it before recording it.")


def _foreign(why: str) -> RuntimeError:
    return RuntimeError(
        f"MIGRATE_REFUSE_FOREIGN_LINEAGE: {why}. This is not the island this host "
        "enrolled. Refusing to start: serving it would put another island's (or an "
        "unrelated) database behind this island's name. If the right volume is "
        "mounted elsewhere, mount that one. If this really is the island you want "
        "here now, take the stack down and run `deploy/compose.sh enroll "
        "--replace-lineage <island_id>`.")


def _environmental(e: BaseException) -> bool:
    """An error that says the record couldn't be READ (permissions, a missing mount, a
    failing disk), as opposed to one that says the record is the wrong SHAPE (a
    symlink, a FIFO or device, oversized, not text). The two need opposite advice: a
    wrong shape is suspicious, while an unreadable file is a host problem, and telling
    the operator to re-enrol would have them bless a volume nobody could read
    (Noether, PR#404 r1)."""
    return isinstance(e, OSError) and not isinstance(e, FileNotFoundError) and e.errno != errno.ELOOP


def _unreadable(path: Path, e: BaseException) -> RuntimeError:
    return RuntimeError(
        f"MIGRATE_REFUSE_WITNESS_UNREADABLE: {path} could not be read "
        f"({type(e).__name__}: {e}). This is a permissions, mount or disk problem on "
        "this host, not evidence of a different island. Refusing to start. Fix the "
        "cause (ownership of the file and its directory, the mount, the disk), then start "
        "again. Do NOT re-enrol to make this go away: enrolment would record whatever "
        "becomes readable later, without anyone having checked it.")


def _present(path: Path) -> bool:
    """Whether ``path`` exists, where only "no such file" means no. ``os.path.exists``,
    ``lexists`` AND ``pathlib.Path.exists`` all answer ``False`` when they merely
    COULDN'T LOOK (a parent dir without search permission): measured, PR#404 r2. So
    "can't tell" read as "absent", which on this module's paths means "no evidence an
    island lived here" or "no enrolment to protect". ``lstat`` (no symlink follow: a
    link counts as present, the fail-closed direction for evidence) raises instead, and
    anything but ``FileNotFoundError`` refuses as unreadable."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as e:
        raise _unreadable(path, e) from e
    return True


def read_tally(witness: Path) -> Tally:
    """The host's tally, or a refusal. Missing, a symlink, or malformed all refuse as
    unenrolled; a read error the host caused refuses as unreadable. There is no partial
    witness."""
    path = witness / TALLY_FILENAME
    try:
        raw = _read_text_nofollow(path)
        data = json.loads(raw)
        tally = Tally(lineage=str(data["lineage"]).strip(),
                      db_path=str(data["db_path"]), island_id=str(data["island_id"]))
    except FileNotFoundError:
        raise _unenrolled(f"{path} does not exist, so this host has not enrolled the "
                          "island on this volume") from None
    except (OSError, ValueError, KeyError, TypeError) as e:
        if _environmental(e):
            raise _unreadable(path, e) from e
        raise _unenrolled(f"{path} could not be read as a tally ({type(e).__name__}: {e})") from e
    if not tally.lineage or not tally.db_path or not tally.island_id:
        raise _unenrolled(f"{path} has an empty lineage, db_path or island_id")
    return tally


def check_lineage(db: Path | None) -> None:
    """THE lineage predicate. Never connects, never writes. Raises a ``MIGRATE_REFUSE_*``
    RuntimeError, or returns when the volume is the enrolled island (or the check is
    off outside production)."""
    witness = _witness_dir()
    if witness is None:
        return
    tally = read_tally(witness)
    if db is None:
        # `:memory:` or a non-file backend: there is no volume to be the right one,
        # and nothing to compare. Armed, that is a refusal, not a pass (Tesla, r3).
        raise _foreign(f"DB_URL does not name a database file, but this host enrolled "
                       f"{tally.db_path}")
    if str(db) != tally.db_path:
        raise _foreign(f"this container opens {db}, but this host enrolled {tally.db_path}")
    # The third terminal: the island this process will NAME itself on the mesh. ISLAND_ID
    # is host-forwarded config, so without this the right volume could serve under
    # another island's name (Tesla, PR#404 r2).
    from .domain.peers_service import self_island_id  # lazy: keep migrate's import light

    named = self_island_id(settings)
    if named != tally.island_id:
        raise _foreign(f"this container names itself {named!r}, but this host enrolled "
                       f"the island {tally.island_id!r} on this volume")
    sentinel = _sentinel_path(db)
    try:
        content = _read_text_nofollow(sentinel).strip()
    except FileNotFoundError:
        raise _unenrolled(
            f"{sentinel} is missing, but this host enrolled an island here. The volume "
            "was wiped or replaced, or a database was restored without its sentinel "
            "(restore them as a pair; see docs/runbooks/backup-and-rollback.md)") from None
    except (OSError, ValueError) as e:  # never a pass, but the advice depends on the cause
        if _environmental(e):
            raise _unreadable(sentinel, e) from e
        raise _foreign(f"{sentinel} is not a plain record of this island "
                       f"({type(e).__name__}: {e})") from e
    if content != tally.lineage:
        raise _foreign(f"{sentinel} reads {content!r}, but this host enrolled {tally.lineage!r}")
    # The right lineage with its database gone, or not a database: refuse HERE, before
    # any caller connects, in ONE voice for both entrances (Tesla, PR#404 r1 + r2). The
    # db is typed like the records: a non-empty regular file, not followed through a
    # link. "Something is at the path" let a 0-byte file through to a connect, and on the
    # uvicorn entrance to the worker lock and a refusal in the schema's voice.
    try:
        st = os.lstat(db)
    except FileNotFoundError:
        raise RuntimeError(refuse_missing_db_message(db, [sentinel], "is missing")) from None
    except OSError as e:
        raise _unreadable(db, e) from e
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        raise RuntimeError(refuse_missing_db_message(
            db, [sentinel], "is not a database (empty, or not a regular file)"))


def _sentinel_content() -> str:
    """A new sentinel names its island beside the time it was written, so a later
    enrolment can check the name, not just recognise a timestamp (design 20 v4: the
    db stores no island identity). Older sentinels hold only the timestamp and are
    never rewritten; the lineage comparison is on the whole string either way."""
    from .domain.peers_service import self_island_id  # lazy: keep migrate's import light
    island = self_island_id(settings)
    stamp = datetime.now(timezone.utc).isoformat()
    return f"{stamp} {island}" if island else stamp


def _write_sentinel(db: Path) -> None:
    """Record that this volume now hosts an island: write-tmp, fsync, rename, fsync
    the directory, so a crash leaves either no sentinel or a whole one."""
    final = _sentinel_path(db)
    if _present(final):
        return
    tmp = final.with_name(final.name + ".tmp")
    # The volume is untrusted until enrolled, and enrolment is exactly when this write
    # runs, so the temp file is created, never opened: a planted `.tmp` symlink to
    # aiko.db would otherwise be truncated into a timestamp (Tesla, PR#404 r1). A
    # leftover from a crash is removed first; unlinking a symlink never touches its
    # target.
    if _present(tmp):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(f"{_sentinel_content()}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    _fsync_dir(db.parent)


def _fsync_dir(path: Path) -> None:
    dir_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _root() -> str:
    """Repo/app root: two dirs up from src/aiko_gateway/migrate.py. Asserts the
    expected layout (alembic.ini present) so a future move fails loud here rather
    than with an opaque alembic error (Kelvin cage-match, PR#23)."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ini = os.path.join(root, "alembic.ini")
    if not os.path.isfile(ini):
        raise RuntimeError(
            f"alembic.ini not found at {ini!r} — migrate.py's assumed layout "
            "(root = two dirs above src/aiko_gateway/) has changed. Fix _root().")
    return root


def _alembic_config() -> Config:
    root = _root()
    cfg = Config(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(root, "alembic"))
    return cfg


def _baseline_metadata() -> MetaData:
    """The schema of the BASELINE revision (0001), reflected from a throwaway DB.

    A pre-alembic DB is — by definition — at whatever schema ``create_all``
    produced when alembic was adopted, which is frozen as baseline 0001. It must
    be diffed against THAT, not against the live ORM models (``Base.metadata``):
    the models track HEAD, so every post-baseline migration that adds something
    ``compare_metadata`` can see (e.g. 0003's ``device_tokens`` table) would make a
    genuine baseline-era DB falsely look "drifted" and be wrongly refused. (0002's
    CHECK-only revision masked this because compare_metadata is blind to CHECKs on
    SQLite — a table addition is the first post-baseline change it can detect.)

    We materialise 0001 once in a temp SQLite file by pointing the app's DB_URL at
    it for the single ``upgrade`` call (env.py derives the target from
    settings.db_url; restored in ``finally``), then reflect it. Reflecting BOTH
    sides from SQLite keeps the later comparison apples-to-apples (no
    declared-vs-reflected type-normalisation noise). Only built on the adopt path,
    which is rare (a one-time event per DB)."""
    with tempfile.TemporaryDirectory() as td:
        ref_path = os.path.join(td, "baseline.db")
        original_url = settings.db_url
        settings.db_url = f"sqlite+aiosqlite:///{ref_path}"
        try:
            command.upgrade(_alembic_config(), BASELINE_REVISION)
        finally:
            settings.db_url = original_url
        ref_engine = create_engine(f"sqlite:///{ref_path}")
        try:
            meta = MetaData()
            meta.reflect(bind=ref_engine)
        finally:
            ref_engine.dispose()
    # alembic_version is bookkeeping, not part of the schema being compared.
    if "alembic_version" in meta.tables:
        meta.remove(meta.tables["alembic_version"])
    return meta


def _compare_to(target: MetaData):
    """Return a function that diffs a sync connection's live schema against
    ``target`` via alembic's own ``compare_metadata`` (same opts as env.py)."""
    def _inner(sync_conn) -> list:
        ctx = MigrationContext.configure(
            sync_conn,
            opts={"compare_type": True, "compare_server_default": True,
                  "target_metadata": target},
        )
        return compare_metadata(ctx, target)
    return _inner


async def _table_names() -> set[str]:
    """Live table names over the app's async driver (no sync driver needed: the
    deploy has only aiosqlite, dev has asyncpg). Short-lived NullPool engine."""
    engine = create_async_engine(settings.db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    finally:
        await engine.dispose()


async def _diff_against(target: MetaData) -> list:
    """Diff the live DB against ``target`` over the async driver."""
    engine = create_async_engine(settings.db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_compare_to(target))
    finally:
        await engine.dispose()


class SchemaState(Enum):
    """Where a database's ``alembic_version`` stands relative to THIS image's
    migration scripts. A closed set, so both boot layers branch on the same four
    answers instead of re-deriving their own."""
    HEAD = "head"            # stamped at exactly this image's head
    BEHIND = "behind"        # every stamped revision is known; not (only) head
    UNKNOWN = "unknown"      # some stamped revision is absent from this image
    UNMANAGED = "unmanaged"  # no alembic_version table at all


@dataclass(frozen=True)
class SchemaStatus:
    state: SchemaState
    stamped: frozenset[str]
    unknown: frozenset[str]
    head: str


def schema_status(conn) -> SchemaStatus:
    """THE single predicate for "may this image's code run on this database?",
    shared by the entrypoint migrator (``run()``) and lifespan
    (``db._assert_at_head``). PR#116 relaxed only one of two separately-written
    checks and shipped a serve-anyway that the other check made unreachable;
    deciding in one place is what keeps the two layers from disagreeing again
    (Carnot, PR#392 r3).

    ``get_current_heads()`` returns the raw ``alembic_version`` rows WITHOUT
    resolving them against the script directory, so an unknown id survives to be
    compared, and a two-row (known + unknown) table is classified here instead of
    dying inside alembic's single-revision lookup. "Unknown" covers an image
    rollback onto a forward-migrated volume (the usual cause), a squashed/removed
    past revision, and a corrupt stamp; this comparison cannot tell them apart,
    and all three must not serve. Sync, so either layer can call it via
    ``run_sync``."""
    script = ScriptDirectory.from_config(_alembic_config())
    head = script.get_current_head()
    if "alembic_version" not in inspect(conn).get_table_names():
        return SchemaStatus(SchemaState.UNMANAGED, frozenset(), frozenset(), head)
    known = {rev.revision for rev in script.walk_revisions()}
    stamped = frozenset(MigrationContext.configure(conn).get_current_heads())
    unknown = stamped - known
    if unknown:
        state = SchemaState.UNKNOWN
    elif stamped == {head}:
        state = SchemaState.HEAD
    else:
        state = SchemaState.BEHIND
    return SchemaStatus(state, stamped, frozenset(unknown), head)


def refuse_unknown_message(status: SchemaStatus) -> str:
    """The one refusal text for an image older than its schema. Both layers raise
    it, so the operator gets the same diagnosis and the same (working) fix whichever
    layer trips first."""
    return (
        "MIGRATE_REFUSE_UNKNOWN_REVISION: database is stamped at revision(s) "
        f"{sorted(status.unknown)} not present in this image's migration scripts, so "
        "this image is OLDER than the schema (or the stamp is from a squashed "
        "history, or corrupt). Refusing to start. To roll back across a migration: "
        "take the stack down (`deploy/compose.sh down`; a merely stopped `restart: "
        "always` container comes back when the Docker daemon restarts), restore the "
        "database backup taken before the newer image migrated it (this LOSES every "
        "write made since that backup), then start this image. Do NOT `alembic stamp` this volume to make the error go away: a "
        "stamp claims the schema matches a revision, and stamping this image's "
        "head onto a newer schema silences this guard for good.")


async def _schema_status() -> SchemaStatus:
    """``schema_status`` over the async driver (the deploy has only aiosqlite)."""
    engine = create_async_engine(settings.db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(schema_status)
    finally:
        await engine.dispose()


def _refuse_enroll_over_a_tally() -> None:
    """``--enroll`` exists to create the first tally, so it runs only where none is
    present. Over an existing tally it would skip the very check that says this volume
    is foreign, then migrate it (Tesla, PR#404 r1). Replacing an enrolment is a
    deliberate, separate act: `compose.sh enroll --replace-lineage` moves the old tally
    aside on the host first, so this refusal never blocks it."""
    raw = settings.witness_dir.strip()
    if not raw or raw == WITNESS_DISARMED:
        # Unmounted is not unenrolled: it means nobody looked (Tesla, PR#404 r2).
        # `compose.sh enroll` always mounts witness/, so in production an unarmed
        # `--enroll` refuses exactly as an unarmed start does.
        _witness_dir()  # raises MIGRATE_REFUSE_UNWITNESSED in production
        return  # outside production: no witness, nothing to protect
    tally = Path(raw) / TALLY_FILENAME
    if _present(tally):  # NOT os.path.lexists: it says False when it can't look (r2)
        raise RuntimeError(
            f"MIGRATE_REFUSE_ENROLLED: {tally} already exists, so this host has enrolled "
            "an island. `--enroll` only creates a first enrolment; it will not run over "
            "an existing one. To replace it, take the stack down and run "
            "`deploy/compose.sh enroll --replace-lineage <island_id>`.")


def run(*, enrolling: bool = False) -> None:
    """Bring the database to head. ``enrolling`` is the explicit ``--enroll`` argv
    mode `deploy/compose.sh enroll` uses: it skips the lineage predicate and NOTHING
    else, because enrolment is the act of creating the tally that predicate reads.
    It is a flag given, never inferred from a missing variable (design 20 v4)."""
    db = _db_file()
    # Lineage first, before anything can connect: a foreign or unenrolled volume must
    # not even be opened (design 20).
    if not enrolling:
        check_lineage(db)
    else:
        _refuse_enroll_over_a_tally()
    # Volume sentinel (design 18 §2 — see module docstring). The missing-file check
    # runs BEFORE any connect, and on a lived volume every connect after it is
    # non-creating (`mode=rw`), so the check is the message and `mode=rw` is the
    # guarantee: there is no window in which a connect could birth a new file.
    evidence = _lived_evidence(db) if db is not None else []
    if not evidence:
        _migrate(db, evidence)
        return
    if not _present(db):
        raise RuntimeError(refuse_missing_db_message(db, evidence, "is missing"))
    original = settings.db_url
    settings.db_url = _non_creating_url(db)
    try:
        _migrate(db, evidence)
    finally:
        settings.db_url = original


def _migrate(db: Path | None, evidence: list[Path]) -> None:
    tables = asyncio.run(_table_names())
    # `users`, not "any table": a db holding only alembic's bookkeeping reads BEHIND
    # and would be built into a new, empty island (Carnot, PR#399 r1). This also
    # catches the 0-byte file a uvicorn-only boot's connect leaves behind.
    if evidence and "users" not in tables:
        raise RuntimeError(refuse_missing_db_message(
            db, evidence, "holds no island (no users table: empty, 0-byte, or bookkeeping only)"))
    if db is not None and not evidence and "users" not in tables:
        # Say it, so "fresh volume" and "no evidence found" are never the same silence,
        # whether the file was missing or held no island (Noether, PR#399 r1 + r3).
        log.warning("No sign an island has lived in %s (no %s, no %s): "
                    "creating a NEW, empty island in %s.",
                    db.parent, _sentinel_path(db).name, LOCK_FILENAME, db.name)
    cfg = _alembic_config()
    adopting = "alembic_version" not in tables and "users" in tables
    if adopting:
        # Fail-closed (Carnot cage-match, PR#23): only stamp a pre-alembic DB as
        # baseline if its schema ACTUALLY equals baseline 0001 — diffed against the
        # baseline schema, not HEAD models (see _baseline_metadata).
        diff = asyncio.run(_diff_against(_baseline_metadata()))
        if diff:
            raise RuntimeError(
                "Refusing to adopt a pre-alembic database: its schema does not "
                f"match baseline {BASELINE_REVISION}. Stamping would falsely mark "
                "it current and (with create_all gone) the difference would never "
                "be repaired. Resolve manually. Drift vs the baseline schema:\n  "
                + "\n  ".join(str(d) for d in diff)
            )
        log.warning(
            "Adopting a pre-alembic database (schema matches baseline %s, no "
            "alembic_version) — stamping baseline.", BASELINE_REVISION)
        command.stamp(cfg, BASELINE_REVISION)

    # Compat guard (design 18 §1 — see module docstring). If the DB is stamped at a
    # revision this image doesn't know, the usual cause is an image rollback onto a
    # volume a newer image migrated forward, and this code may not be able to read
    # what that migration wrote. REFUSE to start, loudly, rather than serve and hope.
    # Do NOT stamp down, do NOT mutate, either way.
    status = asyncio.run(_schema_status())
    if status.state is SchemaState.UNKNOWN:
        raise RuntimeError(refuse_unknown_message(status))

    command.upgrade(cfg, "head")
    if db is not None:
        # A failed write may let the island serve ONLY if something else already marks
        # the volume (`evidence`, gathered before this run). When this write is the
        # first witness (a fresh volume, or a multi-worker one with no lock), serving
        # without it leaves nothing for the next boot to believe, so refuse NOW, while
        # aiko.db is still there (Carnot + Tesla, PR#399 r2).
        try:
            _write_sentinel(db)  # fresh volume or first adoption; a no-op thereafter
        except OSError as e:
            sentinel = _sentinel_path(db)
            if _present(sentinel):
                # The rename landed; only the directory fsync failed. The record is
                # there, so refusing would describe a failure that did not happen
                # (Noether, PR#399 r3). Durability across a power cut is what's lost.
                log.warning("Recorded %s, but could not fsync %s (%s).", sentinel, db.parent, e)
            elif not evidence:
                raise RuntimeError(
                    f"MIGRATE_REFUSE_UNRECORDED: the database {db} is migrated, but "
                    f"{sentinel} could not be written ({e}), and nothing else on this "
                    "volume marks it as hosting an island. Serving now would leave a later "
                    "loss of the database indistinguishable from a fresh volume. Refusing "
                    f"to start. Fix the cause (free space, or ownership of {db.parent}), "
                    "then start again: the database is intact.") from e
            else:
                log.error("Could not record %s (%s). The database is at head and will "
                          "serve; until it is written, %s marks this volume as having "
                          "hosted an island.", sentinel, e, " ".join(str(p) for p in evidence))
    log.info("Database is at head.")


def main(argv: list[str]) -> None:
    args = argv[1:]
    if args not in ([], ["--enroll"]):
        raise SystemExit(f"usage: python -m aiko_gateway.migrate [--enroll]  (got {args})")
    run(enrolling=args == ["--enroll"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main(sys.argv)
