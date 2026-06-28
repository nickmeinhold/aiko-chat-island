"""Async SQLAlchemy engine + session factory.

Dev uses a docker Postgres on :5433 (see spike/devstack notes); deploy uses
file-backed SQLite via DB_URL (the #1281 redesign makes HyperSpace the topology
source of truth and the gateway's local store file-backed SQLite — aiosqlite is
a declared prod dep). The engine is DB_URL-driven, so the dialect follows the
URL.

Schema authority (#14): **alembic owns the schema for any real database.** The
container entrypoint runs ``aiko_gateway.migrate`` (alembic upgrade head) BEFORE
uvicorn, so the app never builds tables itself — ``create_all`` lives ONLY in the
test fixtures (ephemeral in-memory DBs). At boot the app merely VERIFIES the live
schema is migrated and current (``verify_schema``), failing loud if not. This is
the total source-of-truth posture: one mechanism (migrations) creates AND evolves
the schema, so ``create_all`` can never silently paper over a missing revision.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.db_url, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _assert_schema_current(conn) -> None:
    """Fail LOUD if the live schema is not migrated to head (#14, evolved from the
    PR#15 drift guard).

    Two checks, both fail-closed:

    1. **Migrated at all.** A real DB must carry alembic's ``alembic_version``
       table. Its absence means the entrypoint migration (``aiko_gateway.migrate``)
       did not run — e.g. someone started uvicorn directly in dev without
       ``alembic upgrade head`` first. Refuse to serve rather than run against an
       empty/unmanaged DB.

    2. **Social-signin shape** (the original PR#15/Carnot HIGH guard, kept as
       defense-in-depth). Even with migrations in charge, assert the specific
       columns whose absence 500s every user load: ``users.email`` present and
       ``users.password_hash`` nullable.
    """
    from sqlalchemy import inspect

    insp = inspect(conn)
    tables = set(insp.get_table_names())
    if "alembic_version" not in tables:
        raise RuntimeError(
            "DB is not migrated: no alembic_version table. The container "
            "entrypoint runs `python -m aiko_gateway.migrate` before serving; "
            "if you are running uvicorn directly (dev), run "
            "`alembic upgrade head` first. Refusing to serve an unmanaged schema."
        )
    if "users" not in tables:
        # alembic_version exists but users doesn't — a partially-applied/empty
        # migration state. Still unsafe to serve.
        raise RuntimeError(
            "DB is migrated (alembic_version present) but the `users` table is "
            "missing — the migration history is inconsistent with the models. "
            "Refusing to serve."
        )
    cols = {c["name"]: c for c in insp.get_columns("users")}
    problems = []
    if "email" not in cols:
        problems.append("users.email column is missing")
    ph = cols.get("password_hash")
    if ph is not None and ph.get("nullable") is False:
        problems.append("users.password_hash is still NOT NULL "
                        "(social-only accounts require it nullable)")
    if problems:
        raise RuntimeError(
            "DB schema is behind the code: " + "; ".join(problems) + ". "
            "Run the migrations (`alembic upgrade head`) before booting social "
            "sign-in (#13). Refusing to serve a schema that would 500 on user "
            "loads."
        )


async def verify_schema() -> None:
    """Boot-time assertion that the (already-migrated) schema is current. Alembic
    owns creation/evolution via the entrypoint (`aiko_gateway.migrate`); this only
    VERIFIES — it never creates tables. See module docstring for the source-of-
    truth rationale."""
    async with engine.connect() as conn:
        await conn.run_sync(_assert_schema_current)
