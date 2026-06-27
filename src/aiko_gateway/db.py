"""Async SQLAlchemy engine + session factory.

Dev uses a docker Postgres on :5433 (see spike/devstack notes); deploy uses
file-backed SQLite via DB_URL (the #1281 redesign makes HyperSpace the topology
source of truth and the gateway's local store file-backed SQLite — aiosqlite is
a declared prod dep). The engine is DB_URL-driven, so the dialect follows the
URL. Phase 1 creates tables with ``create_all``; alembic migrations (one
revision per phase) land before deploy (tracked task).
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
    """Fail LOUD if the live schema is behind the code (cage-match PR#15, Carnot
    HIGH). `create_all` adds MISSING tables but never alters an EXISTING one — so
    on a deployment that predates social sign-in (#13) it silently leaves
    `users.email` absent and `users.password_hash` NOT NULL. SQLAlchemy would
    then emit `SELECT ... email ...` against a table without that column and 500
    on EVERY user load, not just social inserts. Detect the drift at boot (a
    deploy-time crash with a remediation message) instead of at the first login.
    """
    from sqlalchemy import inspect

    insp = inspect(conn)
    if "users" not in insp.get_table_names():
        return  # fresh DB — create_all just built everything, nothing to check
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
            "create_all does NOT alter existing tables — migrate the live "
            "database, or (if it has no users) recreate the volume DB, before "
            "booting social sign-in (#13). Refusing to serve a schema that "
            "would 500 on user loads."
        )


async def init_models() -> None:
    # Import models so they register on Base.metadata before create_all.
    from .domain import models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_assert_schema_current)
