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
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import quote

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from .config import settings
from .domain import models  # noqa: F401 — register tables on Base.metadata
from .worker_guard import LOCK_FILENAME, _lock_path

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
    url = make_url(settings.db_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        return Path(url.database).resolve()
    return None


def _lived_evidence(db: Path) -> list[Path]:
    """The files that prove an island has lived on this volume. Never connects.

    The sentinel is per database (see ``_sentinel_path``), so existence is the whole
    test: nothing is parsed, so nothing can fail to parse. The worker lock counts at its
    default path and, when ``GATEWAY_WORKER_LOCK`` moves it, at the moved path too.
    (A ``GATEWAY_ALLOW_MULTIWORKER`` gateway writes no lock at all; such a volume is
    covered by the sentinel only, after its first successful run.)"""
    found: list[Path] = []
    sentinel = _sentinel_path(db)
    if sentinel.exists():  # EACCES et al. raise here: fail closed, never "no evidence"
        found.append(sentinel)
    for lock in {db.parent / LOCK_FILENAME, _lock_path()}:
        if lock is not None and lock.exists():
            found.append(lock)
    return found


def _non_creating_url(db: Path) -> str:
    """``settings.db_url`` reopened as an SQLite URI with ``mode=rw``: the file must
    already exist, and no connect can create it. On a lived volume every connect the
    migrator makes (its own and alembic's, which reads ``settings.db_url``) goes
    through this, so a db that vanishes after the existence check makes the next
    connect FAIL, not birth an empty island (Carnot + Tesla, PR#399 r1)."""
    url = make_url(settings.db_url)
    # Percent-encode: `?`, `#` and `%` in a path mean something in an SQLite URI.
    return url.set(database=f"file:{quote(str(db), safe='/')}", query={**url.query, "mode": "rw", "uri": "true"}
                   ).render_as_string(hide_password=False)


def refuse_missing_db_message(db: Path, evidence: list[Path], why: str) -> str:
    """Both fixes below are exercised from the refusing state by the test suite
    (test_volume_sentinel.py), so neither is advice that can't work (#393)."""
    names = " ".join(str(p) for p in evidence)
    return (
        f"MIGRATE_REFUSE_MISSING_DB: the database {db} {why}, but this volume has "
        f"hosted an island before ({names}). Starting would create a NEW, EMPTY island "
        "here and serve it: every account, message and key gone. Refusing to start. "
        "If the database was lost or damaged: take the stack down, restore the newest "
        f"backup to {db} (`deploy/update.sh` keeps them in backups/ beside "
        "docker-compose.yml; see docs/runbooks/backup-and-rollback.md), then start. "
        "Only if you really intend a brand-new, empty island on this volume: delete "
        f"{names}, then start.")


def _write_sentinel(db: Path) -> None:
    """Record that this volume now hosts an island: write-tmp, fsync, rename, fsync
    the directory, so a crash leaves either no sentinel or a whole one."""
    final = _sentinel_path(db)
    if final.exists():
        return
    tmp = final.with_name(final.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    dir_fd = os.open(db.parent, os.O_RDONLY)
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
        "take the stack down (`docker compose down`; a merely stopped `restart: "
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


def run() -> None:
    # Volume sentinel (design 18 §2 — see module docstring). The missing-file check
    # runs BEFORE any connect, and on a lived volume every connect after it is
    # non-creating (`mode=rw`), so the check is the message and `mode=rw` is the
    # guarantee: there is no window in which a connect could birth a new file.
    db = _db_file()
    evidence = _lived_evidence(db) if db is not None else []
    if not evidence:
        if db is not None and not db.exists():
            # Say it, so "fresh volume" and "no evidence found" are never the same
            # silence (Noether, PR#399 r1).
            log.warning("No sign an island has lived in %s (no %s, no %s): "
                        "creating a NEW, empty database.",
                        db.parent, _sentinel_path(db).name, LOCK_FILENAME)
        _migrate(db, evidence)
        return
    if not db.exists():
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
            if not evidence:
                raise RuntimeError(
                    f"MIGRATE_REFUSE_UNRECORDED: the database {db} is migrated, but "
                    f"{sentinel} could not be written ({e}), and nothing else on this "
                    "volume marks it as hosting an island. Serving now would leave a later "
                    "loss of the database indistinguishable from a fresh volume. Refusing "
                    f"to start. Fix the cause (free space, or ownership of {db.parent}), "
                    "then start again: the database is intact.") from e
            log.error("Could not record %s (%s). The database is at head and will serve; "
                      "until it is written, %s marks this volume as having hosted an "
                      "island.", sentinel, e, " ".join(str(p) for p in evidence))
    log.info("Database is at head.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
