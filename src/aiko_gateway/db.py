"""Async SQLAlchemy engine + session factory.

Both dev and deploy use file-backed SQLite via DB_URL (the #1281 redesign makes
HyperSpace the topology source of truth and the gateway's local store file-backed
SQLite — aiosqlite is a declared prod dep). Dev defaults to ./aiko_dev.db; deploy
sets the absolute /data/aiko.db on a volume. Keeping dev on the SAME engine as
prod is deliberate: SQLite's single-writer locking, type affinity, CHECK quirks,
and FK-enforcement-off (app-level cascades) must be what dev exercises, not hidden
behind Postgres. The engine is DB_URL-driven, so the dialect follows the URL.

Schema authority (#14): **alembic owns the schema for any real database.** The
container entrypoint runs ``aiko_gateway.migrate`` (alembic upgrade head) BEFORE
uvicorn, so the app never builds tables itself — ``create_all`` lives ONLY in the
test fixtures (ephemeral in-memory DBs). At boot the app merely VERIFIES the live
schema is migrated and current (``verify_schema``), failing loud if not. This is
the total source-of-truth posture: one mechanism (migrations) creates AND evolves
the schema, so ``create_all`` can never silently paper over a missing revision.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from sqlalchemy import event
from sqlalchemy.engine import make_url
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


def non_creating_url(url: str, db: Path) -> str:
    """``url`` reopened as an SQLite URI with ``mode=rw``: the file must already exist,
    and no connect through it can create one. The one spelling, shared by the migrator
    (on a lived volume) and the serving engine (in production)."""
    parsed = make_url(url)
    # Percent-encode: `?`, `#` and `%` in a path mean something in an SQLite URI.
    return parsed.set(database=f"file:{quote(str(db), safe='/')}",
                      query={**parsed.query, "mode": "rw", "uri": "true"}
                      ).render_as_string(hide_password=False)


def serving_url() -> str:
    """The URL the SERVING app connects with. In production a file-backed db is opened
    non-creating: the migrator (the entrypoint) is what creates databases, never the
    app. So a db that is missing, or vanishes after the volume witness looked, makes the
    first connect FAIL rather than birth an empty file (Carnot, PR#404 r2). That removes
    the check-then-create window instead of guarding it. Dev and tests keep the plain
    URL; their harnesses legitimately start from no file."""
    from .worker_guard import sqlite_file  # no cycle: worker_guard needs only config

    db = sqlite_file(settings.db_url)
    if db is None or not settings.is_production:
        return settings.db_url
    return non_creating_url(settings.db_url, db)


engine = make_engine(serving_url())
# expire_on_commit=False is LOAD-BEARING, not a default (cage-match Kelvin + Wu). These
# paths read ORM attributes AFTER commit and would MissingGreenlet-crash on a lazy reload
# if it were True: create_outbound -> message_view (WS/REST fanout), ban_user route
# (active-disconnect), and resolve_report's retraction fanout (#7). Flip this to True and
# those three break — grep them before you do.
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# One owner for the "not migrated at all" refusal, raised by whichever layer sees it
# first, so the order of the two checks in verify_schema() cannot change what an
# unmanaged DB is told (Noether, PR#392 r6). The fix it names is the one that works:
# ``python -m aiko_gateway.migrate`` builds a fresh DB AND adopts a pre-alembic one,
# where a bare ``alembic upgrade head`` dies on CREATE TABLE users.
NOT_MIGRATED_MESSAGE = (
    "DB is not migrated: no alembic_version table. The container entrypoint runs "
    "`python -m aiko_gateway.migrate` before serving; if you are running uvicorn "
    "directly (dev), run that first. Refusing to serve an unmanaged schema.")


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
        raise RuntimeError(NOT_MIGRATED_MESSAGE)
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
    if ph is None:
        # Presence-aware like the email check above: an ABSENT column 500s every
        # user load just as surely as a NOT NULL one (Noether, PR#392 r5).
        problems.append("users.password_hash column is missing")
    elif ph.get("nullable") is False:
        problems.append("users.password_hash is still NOT NULL "
                        "(social-only accounts require it nullable)")
    if problems:
        # verify_schema() runs this only AFTER _assert_at_head has accepted the stamp
        # as this code's head, so "run the migrations" would be a no-op here: the
        # schema was hand-edited or partially applied under a head stamp (Noether,
        # Tesla, PR#392 r6). Say so, and name the fix that works.
        raise RuntimeError(
            "DB schema does not match the code: " + "; ".join(problems) + ". The "
            "alembic stamp already claims this code's head, so `alembic upgrade "
            "head` cannot repair it: the schema was hand-edited or a migration was "
            "partially applied. Restore a known-good backup. Refusing to serve a "
            "schema that would 500 on user loads."
        )


def _assert_at_head(conn) -> None:
    """Fail LOUD unless the DB is at exactly the script head (Carnot cage-match,
    PR#23). ``_assert_schema_current`` only checks that the DB is *managed*; this
    checks it is *current*. Decided by ``migrate.schema_status``, the same predicate
    the entrypoint migrator uses, so the two boot layers cannot disagree (PR#392):
    an image OLDER than the schema gets the migrator's refusal and its working fix,
    and a DB left BEHIND (uvicorn started directly, bypassing the entrypoint) gets
    told to upgrade."""
    from .migrate import SchemaState, refuse_unknown_message, schema_status  # lazy: migrate imports db.Base (cycle)

    status = schema_status(conn)
    if status.state is SchemaState.UNMANAGED:
        raise RuntimeError(NOT_MIGRATED_MESSAGE)
    if status.state is SchemaState.UNKNOWN:
        raise RuntimeError(refuse_unknown_message(status) + " (lifespan check)")
    if status.state is not SchemaState.HEAD:
        raise RuntimeError(
            f"DB is at alembic revision(s) {sorted(status.stamped)} but the code's "
            f"head is {status.head!r}. The entrypoint runs `python -m "
            "aiko_gateway.migrate` before serving; if running uvicorn directly "
            "(dev), run that first. Refusing to serve a stale schema.")


async def verify_schema() -> None:
    """Boot-time assertion that the (already-migrated) schema is current. Alembic
    owns creation/evolution via the entrypoint (`aiko_gateway.migrate`); this only
    VERIFIES — it never creates tables. See module docstring for the source-of-
    truth rationale."""
    # Revision FIRST, then column shape. A newer image's migration may change the
    # columns _assert_schema_current inspects, and on that schema the shape check
    # would misdiagnose "behind the code, run upgrade head". The revision check knows
    # the schema is AHEAD and gives the working fix (Tesla, PR#392 r4).
    # (The volume witness is checked earlier, at the top of `main.lifespan`, before the
    # worker lock is created: see the note there. One call site per boot path.)
    async with engine.connect() as conn:
        await conn.run_sync(_assert_at_head)
        await conn.run_sync(_assert_schema_current)
