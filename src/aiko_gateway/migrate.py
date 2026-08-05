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
        revision this image's ``alembic/versions/`` does not contain) → SERVE, do
        not upgrade

**Forward-revision tolerance (task #11 Temper — the boot-half of rollback safety).**
The islands roll back by re-pinning an OLDER container IMAGE onto the SAME persistent
volume; the volume keeps whatever revision the newer image migrated it to. Because
this runner fails closed, a plain ``upgrade head`` on that forward-migrated volume
would die with "Can't locate revision …" (the old image's script directory has no
such revision) and the entrypoint would refuse to serve — crash-looping the
rolled-back island even though its schema is backward- (N-1) compatible. So when the
DB is stamped at a revision this image doesn't know, we SERVE on the existing schema:
no upgrade, no stamp-down, no mutation. The schema ratchets forward and stays.

**"Unknown to this image" is NOT the same set as "ahead of head" (Wu cage-match,
PR#116).** An image rollback is the *intended and usual* cause, but the same detector
fires on two others we CANNOT distinguish from the script directory alone: a
squashed/removed past revision (routine alembic history hygiene → a volume stamped at
the now-deleted revision), and a corrupt ``version_num``. In those two the volume is
NOT ahead — it is behind or garbage — and serving-without-upgrading pins it below head
*silently and permanently* (every boot re-takes this skip; ``upgrade head`` is never
reached again), where the pre-fix code crash-looped *loudly*. We accept that cost
because (a) rollback is the overwhelmingly common trigger, (b) distinguishing the
cases needs information not in the script graph, and (c) the skip is emitted under a
distinct, alertable log marker (``MIGRATE_SKIP_UNKNOWN_REVISION``) so an operator — and
the reactive-deploy watcher (#10) — can catch a frozen volume. **If you squash or
rebase migration history, stamp every live volume to the new baseline**, or those
volumes will silently stop migrating. Safety of the served schema is guaranteed by the
expand/contract CI gate — which is NOT yet built (task #11) — NOT by this function.
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

log = logging.getLogger("aiko_gateway.migrate")

BASELINE_REVISION = "0001"


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


async def _unknown_db_heads(cfg: Config) -> set[str]:
    """Revision(s) the DB is stamped at that this image's migration scripts do NOT
    contain. A non-empty result USUALLY means an image rollback onto a volume a newer
    image already migrated forward — but "unknown" also covers a squashed/removed past
    revision or a corrupt stamp, which this comparison cannot tell apart (see the
    module docstring's forward-revision tolerance).

    ``get_current_heads()`` returns the raw ``alembic_version`` rows without
    resolving them against the script directory, so an unknown revision id survives to
    be compared here (an empty tuple on a DB with no version table — a fresh or
    pre-alembic DB — which correctly yields no unknowns). Read over the async driver
    (the deploy has only aiosqlite)."""
    known = {rev.revision for rev in ScriptDirectory.from_config(cfg).walk_revisions()}
    engine = create_async_engine(settings.db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            heads = await conn.run_sync(
                lambda c: MigrationContext.configure(c).get_current_heads())
    finally:
        await engine.dispose()
    return set(heads) - known


def run() -> None:
    tables = asyncio.run(_table_names())
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

    # Forward-revision tolerance (task #11 Temper — see module docstring). If the DB
    # is stamped at a revision this image doesn't know, the USUAL cause is an image
    # rollback onto a forward-migrated volume and we are the rollback target: SERVE on
    # the existing schema rather than dying fail-closed in `upgrade head`. Do NOT stamp
    # down, do NOT mutate. NB "unknown" is not PROVABLY "ahead" (a squash/removed
    # revision or corrupt stamp looks identical) — hence the distinct, alertable marker
    # and the honest warning below (Wu cage-match, PR#116).
    unknown = asyncio.run(_unknown_db_heads(cfg))
    if unknown:
        log.warning(
            "MIGRATE_SKIP_UNKNOWN_REVISION: database is stamped at revision(s) %s not "
            "present in this image's migration scripts. Usually an image rollback onto "
            "a volume a newer image migrated forward (the DB is ahead of this code) — "
            "then serving on the existing schema is correct. But an unknown revision "
            "is NOT provably ahead: a squashed/removed past revision, or a corrupt "
            "version stamp, is indistinguishable here and would leave this volume "
            "silently pinned BELOW head (this skip re-fires every boot). Serving "
            "WITHOUT upgrading; not stamping down, not mutating. Safe ONLY if the "
            "schema is backward- (N-1) compatible — a property the expand/contract CI "
            "gate WILL enforce once built (task #11); it is NOT yet enforced. If this "
            "fired after a migration-history squash/rebase, stamp this volume to the "
            "new baseline.",
            sorted(unknown))
        return

    command.upgrade(cfg, "head")
    log.info("Database is at head.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
