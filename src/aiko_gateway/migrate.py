"""Bring the database to head — the container entrypoint runs this BEFORE uvicorn.

Why an explicit runner (not just `alembic upgrade head`): the gateway adopted
alembic AFTER a live DB already existed, built by the old ``create_all`` path.
That live DB has every table but no ``alembic_version`` row, so a naive
``upgrade head`` would run the baseline's ``CREATE TABLE users`` against a DB that
already has it and abort. The fix is **stamp-or-upgrade**:

  * fresh DB (no ``alembic_version``, no ``users``)      → REFUSE, always (this runner
        never builds an island; see "Starting never creates an island" below)
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

**Starting never creates an island.** SQLite creates a missing file on the first
connect, and a fresh file is indistinguishable from a brand-new island, so a lost or
half-restored ``aiko.db`` used to come back as an EMPTY island that served (every
account, message and key gone, `restart: always` making it permanent). The fix is
not to detect that, it is to remove the code that did it:

  * every door to the database (this runner, the app's engine, alembic's env.py)
    opens the file OPEN-ONLY (``worker_guard.open_only``, ``mode=rw``), so no connect
    can create it;
  * a plain run refuses a database with no ``users`` table (empty, truncated to 0
    bytes, or alembic's bookkeeping only): building the schema into it would be the
    same new, empty island by another route;
  * the gateway has NO way to create an island, by flag, setting or otherwise. An
    island is born as a COPY: ``docker build`` runs ``scripts/build_seed.py`` once,
    which builds an empty island at head into ``/app/seed/aiko.db`` in an image layer
    (where no volume exists to harm), and standup copies that file into a ``/data``
    that is completely empty. A volume that has ever served is never empty (the
    worker lock lives beside the db), so a lost database can't qualify (Nick,
    2026-10-25, replacing the ``--new-island`` mode PR#405 r1-r3 reviewed).

This replaced the volume sentinel (``.aiko.db.initialized``, PR#399) and the design-20
witness built on top of it: both detected the wrong moment to create, and once
nothing creates, there is nothing left to detect. Sentinel files already on a volume
are inert.

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
from .domain import models  # noqa: F401 — register tables on Base.metadata
from .worker_guard import open_only, sqlite_file

log = logging.getLogger("aiko_gateway.migrate")

BASELINE_REVISION = "0001"


def _db_file() -> Path | None:
    """The SQLite file behind ``settings.db_url``, or None for a backend with no
    volume (Postgres dev, in-memory). Same derivation as ``worker_guard``."""
    return sqlite_file(settings.db_url)


_RESTORE = (
    "If the database was lost or damaged: take the stack down, restore the newest "
    "backup (`deploy/update.sh` keeps them in backups/ beside docker-compose.yml; see "
    "docs/runbooks/backup-and-rollback.md), then start.")
# A refusal names the cause and the repair (restore), and never "move it aside": an
# operator following a refusal is on a box whose island is in doubt (PR#405 r3).
# Naming standup is safe: it only ever copies into an EMPTY /data, which no volume
# that has served can be, so on this box it refuses.
_NEVER_HELD_ONE = (
    "A box that has never held an island gets one from standup, never from this "
    "program.")


def no_database_message(db: Path, *, via: Path | None = None) -> str:
    """Raised by both boot layers (this runner and lifespan's ``verify_schema``), so the
    operator gets one diagnosis whichever trips first. ``via``: the configured path, when
    it is a symlink whose target ``db`` is what's missing."""
    where = f"{via} is a symlink to {db}, which" if via else str(db)
    return (
        f"MIGRATE_REFUSE_NO_DATABASE: {where} does not exist. The gateway never creates a "
        "database when it starts, so a lost one can't come back as a new, empty island. "
        f"Refusing to start. {_RESTORE} {_NEVER_HELD_ONE}")


def not_an_island_message(db: Path | None, why: str) -> str:
    return (
        f"MIGRATE_REFUSE_NOT_AN_ISLAND: the database {db or settings.db_url} {why}. "
        "Building the schema into it would start a new, empty island in its place. "
        f"Refusing to start. {_RESTORE} {_NEVER_HELD_ONE}")


def require_database_file(db_url: str) -> None:
    """The message, not the guarantee: every connect is open-only, so a missing file
    could not be created either way. ``lstat``, because "can't look" must never read as
    "missing", and the stdlib's answers differ by function AND by version: measured on
    3.12.8, ``os.path.exists`` says False on EACCES while ``Path.exists`` raises a raw
    PermissionError (3.13 moved ``Path.exists`` to the False behaviour). ``lstat`` raises
    the same everywhere, so the two causes get their own messages (PR#405 r1).
    Takes the URL so every caller derives the file the same way, and so a dangling
    symlink is named as one (the configured path, unresolved) rather than reported as a
    plain missing file (PR#405 r3). No file behind the URL (Postgres, in-memory): no-op."""
    db = sqlite_file(db_url)
    if db is None:
        return
    given = sqlite_file(db_url, follow_symlinks=False)
    try:
        os.lstat(db)
    except FileNotFoundError:
        via = given if given != db and os.path.islink(given) else None
        raise RuntimeError(no_database_message(db, via=via)) from None
    except OSError as e:
        raise RuntimeError(
            f"MIGRATE_REFUSE_UNREADABLE: {db} could not be checked ({e}). That is a "
            "permissions, mount or disk problem on this host, not a missing database. "
            "Refusing to start. Fix the cause, then start again.") from e


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


SEED_BUILD_ATTR = "aiko_seed_build"


def _alembic_config(*, seed_build: bool = False) -> Config:
    """``seed_build`` is the capability the baseline migration requires before it builds
    an island from nothing. Only Python grants it: ``scripts/build_seed.py`` at image
    build time, the scratch database ``_baseline_metadata`` makes for itself, and tests.
    No command line, setting or hand-run ``alembic`` can."""
    root = _root()
    cfg = Config(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(root, "alembic"))
    if seed_build:
        cfg.attributes[SEED_BUILD_ATTR] = True
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
        open(ref_path, "x").close()  # alembic opens open-only; this scratch db is ours to create
        try:
            command.upgrade(_alembic_config(seed_build=True), BASELINE_REVISION)  # our scratch db
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
    engine = create_async_engine(open_only(settings.db_url), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    finally:
        await engine.dispose()


async def _diff_against(target: MetaData) -> list:
    """Diff the live DB against ``target`` over the async driver."""
    engine = create_async_engine(open_only(settings.db_url), poolclass=NullPool)
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
    engine = create_async_engine(open_only(settings.db_url), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(schema_status)
    finally:
        await engine.dispose()



def run() -> None:
    """Bring an existing island's database to head. Never creates one: islands are
    born by standup copying the image's seed (see the module docstring)."""
    db = _db_file()
    require_database_file(settings.db_url)
    tables = asyncio.run(_table_names())
    # THE GUARANTEE is the baseline's own gate (alembic/versions/0001_baseline.py): it
    # builds an island from nothing, and refuses without the seed-build capability, so
    # no door (this runner, a hand-run alembic) can build one into an empty file. This
    # pre-check adds a different property: a plain start that refuses TOUCHES NOTHING,
    # where the baseline's refusal comes after alembic has already written its own
    # bookkeeping table. `users`, not "any table": bookkeeping alone is not an island.
    if "users" not in tables:
        raise RuntimeError(not_an_island_message(
            db, "holds no island (no users table: empty, 0-byte, or alembic's bookkeeping only)"))
    cfg = _alembic_config()
    # Adopt when the island is there and NOTHING is stamped: no alembic_version table,
    # or an EMPTY one. Measured (PR#405 r2): a hand-run alembic that the baseline gate
    # refused leaves an empty alembic_version table behind, and keying on the table's
    # absence would then refuse this island forever (Tesla).
    adopting = "alembic_version" not in tables or not asyncio.run(_schema_status()).stamped
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
    log.info("Database is at head.")


def main(argv: list[str]) -> None:
    if argv[1:]:
        raise SystemExit(f"usage: python -m aiko_gateway.migrate  (takes no arguments; got "
                         f"{argv[1:]}). It never creates a database: standup does that.")
    run()


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    main(sys.argv)
