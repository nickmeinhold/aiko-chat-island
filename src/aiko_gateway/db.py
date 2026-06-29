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

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import settings


class Base(DeclarativeBase):
    pass


def _tune_sqlite_concurrency(engine: AsyncEngine) -> None:
    """Make file-backed SQLite — the PROD store (#1281) — behave correctly under
    the async connection pool's concurrent connections (#12). One PRAGMA, set on
    every new connection:

    * ``busy_timeout=5000`` — the default is 0, so a second concurrent writer gets
      ``SQLITE_BUSY`` ("database is locked") IMMEDIATELY rather than waiting. That
      surfaces as 500s under contention; with a timeout the writer waits (up to 5s)
      for the lock and serializes cleanly. (The last-admin guard is made correct
      separately, by an atomic conditional DELETE — see
      memberships_service._delete_membership_unless_last_admin — since SQLite has
      no row locks for a FOR UPDATE to take.)

    busy_timeout is per-connection and does NOT change the on-disk format, so it
    has zero interaction with the backup/restore tooling. WAL mode (better
    read/write concurrency) is DELIBERATELY NOT enabled here: it adds -wal/-shm
    sidecars and changes the on-disk format, which would invalidate the #17 restore
    drill until the imagineering-infra backup tooling is verified to checkpoint /
    capture WAL. Measured: busy_timeout alone fixes the concurrency bug; WAL is a
    separate, must-be-verified durability decision (tracked).

    SQLite only — a guard in make_engine keeps this off Postgres (the dev engine)."""
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


def make_engine(url: str) -> AsyncEngine:
    """Build the async engine for ``url``, applying the SQLite concurrency tuning
    when the URL is SQLite. Factored out (not inlined) so tests can build a
    prod-equivalent file-backed engine and exercise the real tuning (#12)."""
    engine = create_async_engine(url, echo=False, pool_pre_ping=True)
    if url.startswith("sqlite"):
        _tune_sqlite_concurrency(engine)
    return engine


engine = make_engine(settings.db_url)
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


def _assert_at_head(conn) -> None:
    """Fail LOUD if the DB's alembic revision is not the script head (Carnot
    cage-match, PR#23). ``_assert_schema_current`` only checks that the DB is
    *managed* (alembic_version present); this checks it is *current*. Without it, a
    DB left at 0001 after a later 0002 ships — e.g. uvicorn started directly,
    bypassing the entrypoint migration — would boot and serve a stale schema."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from .migrate import _alembic_config  # lazy: migrate imports db.Base (cycle)

    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    current = MigrationContext.configure(conn).get_current_revision()
    if current != head:
        raise RuntimeError(
            f"DB is at alembic revision {current!r} but the code's head is "
            f"{head!r}. The entrypoint runs `python -m aiko_gateway.migrate` "
            "before serving; if running uvicorn directly (dev), run "
            "`alembic upgrade head` first. Refusing to serve a stale schema."
        )


async def verify_schema() -> None:
    """Boot-time assertion that the (already-migrated) schema is current. Alembic
    owns creation/evolution via the entrypoint (`aiko_gateway.migrate`); this only
    VERIFIES — it never creates tables. See module docstring for the source-of-
    truth rationale."""
    async with engine.connect() as conn:
        await conn.run_sync(_assert_schema_current)
        await conn.run_sync(_assert_at_head)
