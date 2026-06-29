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
manual ``docker compose up -d`` — see aiko_chat_gateway#19), so this MUST run in
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

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from .config import settings
from .db import Base
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


def _inspect(sync_conn) -> tuple[set[str], list]:
    """Return (table names, baseline-vs-ORM diff) from one sync connection.

    The diff is alembic's own ``compare_metadata`` against the ORM metadata — an
    empty list means the live schema already equals the models (== baseline 0001,
    which the parity test pins to the models). Used to decide whether a
    pre-alembic DB is safe to adopt by stamping."""
    tables = set(inspect(sync_conn).get_table_names())
    ctx = MigrationContext.configure(
        sync_conn,
        opts={"compare_type": True, "compare_server_default": True,
              "target_metadata": Base.metadata},
    )
    diff = compare_metadata(ctx, Base.metadata)
    return tables, diff


async def _probe() -> tuple[set[str], list]:
    """Inspect over the app's async driver so no sync DB driver is needed (the
    deploy has only aiosqlite; dev has asyncpg). Short-lived NullPool engine."""
    engine = create_async_engine(settings.db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_inspect)
    finally:
        await engine.dispose()


def run() -> None:
    tables, diff = asyncio.run(_probe())
    cfg = _alembic_config()
    adopting = "alembic_version" not in tables and "users" in tables
    if adopting:
        if diff:
            raise RuntimeError(
                "Refusing to adopt a pre-alembic database: its schema does not "
                f"match baseline {BASELINE_REVISION}. Stamping would falsely mark "
                "it current and (with create_all gone) the difference would never "
                "be repaired. Resolve manually. Drift vs the ORM models:\n  "
                + "\n  ".join(str(d) for d in diff)
            )
        log.warning(
            "Adopting a pre-alembic database (schema matches baseline %s, no "
            "alembic_version) — stamping baseline.", BASELINE_REVISION)
        command.stamp(cfg, BASELINE_REVISION)
    command.upgrade(cfg, "head")
    log.info("Database is at head.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
