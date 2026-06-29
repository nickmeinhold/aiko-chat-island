"""The boot schema-drift guard (cage-match PR#15 Carnot HIGH; tightened in #14).

Two layers, both fail-closed:

1. **Migrated at all** (#14): a real DB must carry alembic's `alembic_version`
   table. Its absence means the entrypoint migration never ran — refuse to serve.
2. **Social-signin shape** (original PR#15 guard, kept as defense-in-depth): even
   on a migrated DB, assert `users.email` exists and `users.password_hash` is
   nullable — the columns whose absence 500s every user load.

These drive `db._assert_schema_current` directly over a SYNC sqlite connection
(it's a sync-conn callback run via run_sync in production). A bare
`alembic_version` table (empty) stands in for "this DB is alembic-managed" — the
guard checks the table's PRESENCE, not its contents.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from aiko_gateway.db import Base, _assert_schema_current


def _stamp(conn) -> None:
    """Mark the connection's DB as alembic-managed (presence is all the guard
    checks for layer 1)."""
    conn.execute(text(
        "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))


def _check(conn):
    from aiko_gateway.domain import models  # noqa: F401 — register tables
    _assert_schema_current(conn)


def test_current_schema_passes():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    from aiko_gateway.domain import models  # noqa: F401
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        _stamp(conn)
        _assert_schema_current(conn)  # migrated + current schema → must NOT raise


def test_unmigrated_db_raises():
    """No alembic_version at all (e.g. uvicorn run directly without migrating) →
    the new layer-1 guard refuses to serve."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    from aiko_gateway.domain import models  # noqa: F401
    with engine.begin() as conn:
        Base.metadata.create_all(conn)  # full schema, but unmanaged
        with pytest.raises(RuntimeError, match="not migrated"):
            _assert_schema_current(conn)


def test_migrated_but_missing_users_raises():
    """alembic_version present but no users table — inconsistent history; unsafe."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        _stamp(conn)
        with pytest.raises(RuntimeError, match="users` table is missing"):
            _assert_schema_current(conn)


def test_missing_email_column_raises():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        _stamp(conn)
        # An OLD-shape users table: no email column, password_hash NOT NULL.
        conn.execute(text(
            "CREATE TABLE users ("
            " id VARCHAR(26) PRIMARY KEY,"
            " username VARCHAR(64) NOT NULL,"
            " display_name VARCHAR(128) NOT NULL,"
            " password_hash TEXT NOT NULL,"
            " aiko_username VARCHAR(64) NOT NULL,"
            " created_at DATETIME)"
        ))
        with pytest.raises(RuntimeError, match="email"):
            _check(conn)


def test_not_null_password_hash_raises():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        _stamp(conn)
        # Has email, but password_hash is still NOT NULL (half-migrated).
        conn.execute(text(
            "CREATE TABLE users ("
            " id VARCHAR(26) PRIMARY KEY,"
            " username VARCHAR(64) NOT NULL,"
            " display_name VARCHAR(128) NOT NULL,"
            " password_hash TEXT NOT NULL,"
            " aiko_username VARCHAR(64) NOT NULL,"
            " email VARCHAR(320),"
            " created_at DATETIME)"
        ))
        with pytest.raises(RuntimeError, match="NOT NULL"):
            _check(conn)
