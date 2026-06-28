"""Alembic environment — async-aware, app-settings-driven.

Two deliberate choices that the rest of #14 depends on:

1. **URL comes from the app, not alembic.ini.** We read ``config.settings.db_url``
   so migrations target the exact DB the app does (Postgres in dev, file-backed
   SQLite in deploy) off the one ``DB_URL`` env var. No second source of truth
   for "which database".

2. **batch mode for SQLite.** The deploy DB is SQLite, whose ``ALTER TABLE`` can't
   drop/alter a column or add a CHECK in place. ``render_as_batch=True`` makes
   alembic emit the create-new / copy / swap dance for those ops, so a revision
   that alters an existing table actually applies on SQLite (the whole reason
   ``create_all`` was insufficient — see db.py). It's a no-op on Postgres.

The engine is async (aiosqlite / asyncpg), so online migrations run inside
``connection.run_sync`` — alembic's migration ops are sync, driven over the async
connection. Offline (``--sql``) mode is supported too.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

# Import the app's metadata + settings. prepend_sys_path=src (alembic.ini) makes
# the package importable without an install. Importing models registers every
# table on Base.metadata — this is the autogenerate/target schema.
from aiko_gateway.config import settings
from aiko_gateway.db import Base
from aiko_gateway.domain import models  # noqa: F401  (registers tables on Base)

config = context.config
# URL precedence: an explicitly-provided sqlalchemy.url (a caller did
# `config.set_main_option("sqlalchemy.url", ...)` — e.g. the parity test pointing
# at a throwaway DB) wins; otherwise fall back to the app settings so normal
# runs target the real database off DB_URL. alembic.ini intentionally leaves the
# url unset, so absent an override this always resolves to settings.db_url.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _configure(connection) -> None:
    """Shared context config for both online passes. ``render_as_batch`` is the
    SQLite ALTER enabler; ``compare_type`` so a column type change is detected by
    autogenerate (defends the parity test against silent type drift)."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
