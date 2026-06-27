"""The boot schema-drift guard (cage-match PR#15, Carnot HIGH).

`create_all` adds missing tables but never alters an existing one. On a
deployment that predates social sign-in (#13), the live `users` table lacks
`email` and still has `password_hash NOT NULL`, so the running code would 500 on
every user load. `db._assert_schema_current` turns that silent runtime failure
into a loud boot crash. These tests drive it directly over a SYNC sqlite
connection (it's a sync-conn callback run via run_sync in production).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from aiko_gateway.db import Base, _assert_schema_current


def _check(conn):
    # Register the ORM tables on Base.metadata, then run the guard.
    from aiko_gateway.domain import models  # noqa: F401
    _assert_schema_current(conn)


def test_current_schema_passes():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    from aiko_gateway.domain import models  # noqa: F401
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        _assert_schema_current(conn)  # must NOT raise on a fresh, current schema


def test_missing_email_column_raises():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
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


def test_no_users_table_is_treated_as_fresh():
    # Before create_all there's no users table — the guard must not false-positive
    # (create_all runs first in init_models; this is just the empty-DB case).
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        _assert_schema_current(conn)  # no users table → returns without raising
