"""Migration correctness (#14) — the gate that replaces dead CI.

Three properties, each a way the migration system could silently lie:

1. **Parity** — ``alembic upgrade head`` on a fresh DB must build the SAME schema
   as the ORM models (``create_all``). Without this, a model change without a
   matching revision drifts the live DB from the code invisibly. This is the
   check ``alembic check`` / a CI autogenerate-diff would run; we run it as a unit
   test because there is no CI (aiko_chat_gateway#18).

2. **Fresh upgrade** — an empty DB upgrades to a complete, alembic-managed schema.

3. **Adopt** — a PRE-alembic DB (built by the old ``create_all`` path: all tables,
   no ``alembic_version``) is adopted by stamping the baseline, NOT by trying to
   re-create existing tables (which would abort). This is the exact situation of
   the live prod DB at the moment this ships.

All three drive the real entrypoint runner (``aiko_gateway.migrate.run``) and the
real ``alembic/env.py`` against a throwaway file SQLite DB, so they exercise the
shipped code path, not a reimplementation.
"""
from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from aiko_gateway import migrate
from aiko_gateway.config import settings
from aiko_gateway.db import Base
from aiko_gateway.domain import models  # noqa: F401 — register tables on Base.metadata

# The seven tables the Phase-1 models define (+ alembic_version once managed).
_MODEL_TABLES = {
    "users", "social_identities", "channels", "memberships",
    "messages", "user_blocks", "message_reports",
}


def _point_app_at(tmp_path, monkeypatch) -> tuple[str, str]:
    """Point the app+alembic at a throwaway file DB. Returns (async_url, sync_url).
    monkeypatch on the settings singleton is what both migrate._existing_tables AND
    alembic/env.py read (env.py falls back to settings.db_url when no explicit url
    is set on the alembic config)."""
    db = tmp_path / "mig.db"
    async_url = f"sqlite+aiosqlite:///{db}"
    sync_url = f"sqlite:///{db}"
    monkeypatch.setattr(settings, "db_url", async_url)
    return async_url, sync_url


def test_migrations_match_models(tmp_path, monkeypatch) -> None:
    """The gate: upgrade head, then assert alembic sees NO difference between the
    migrated schema and the ORM metadata."""
    _, sync_url = _point_app_at(tmp_path, monkeypatch)
    migrate.run()  # stamp-or-upgrade -> upgrade head on a fresh DB

    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            # Match env.py's comparison opts (compare_type + compare_server_default)
            # so the gate sees what a real autogenerate would. NOTE the known
            # SQLite blind spots in alembic's reflection: CHECK constraints,
            # partial/expression indexes, and some dialect-normalised types are not
            # reliably diffed. This is a drift SMOKE TEST, strong for tables /
            # columns / nullability / uniques / plain indexes; when #11 adds CHECK
            # constraints (revision 0002) add a targeted assertion for them rather
            # than trusting compare_metadata alone (Carnot cage-match, PR#23).
            ctx = MigrationContext.configure(
                conn, opts={"compare_type": True, "compare_server_default": True,
                            "target_metadata": Base.metadata})
            diffs = compare_metadata(ctx, Base.metadata)
    finally:
        engine.dispose()

    assert diffs == [], (
        "alembic migrations have drifted from the ORM models — a model change is "
        "missing a revision. Generate one with `alembic revision --autogenerate`. "
        f"Diff: {diffs}"
    )


def test_fresh_db_upgrades_to_head(tmp_path, monkeypatch) -> None:
    _, sync_url = _point_app_at(tmp_path, monkeypatch)
    migrate.run()

    engine = create_engine(sync_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert _MODEL_TABLES <= tables
    assert "alembic_version" in tables


def test_adopt_pre_alembic_db_stamps_baseline(tmp_path, monkeypatch) -> None:
    """A DB built by the old create_all path (all tables, no alembic_version) is
    adopted via stamp — migrate.run must NOT try to re-create existing tables."""
    async_url, sync_url = _point_app_at(tmp_path, monkeypatch)

    # 1. Simulate the live pre-alembic DB: full schema, but unmanaged.
    seed = create_engine(sync_url)
    try:
        Base.metadata.create_all(seed)
    finally:
        seed.dispose()

    # 2. Run the real entrypoint migrator against it.
    migrate.run()  # must adopt (stamp 0001), not CREATE TABLE -> abort

    # 3. It is now managed at the baseline, with the schema intact.
    engine = create_engine(sync_url)
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        with engine.connect() as conn:
            version = conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version").scalar()
    finally:
        engine.dispose()

    assert "alembic_version" in tables
    assert _MODEL_TABLES <= tables
    # Adoption stamps the baseline THEN upgrades, so an adopted DB ends at HEAD
    # (not merely baseline) — it is both claimed-as-managed and brought current.
    from alembic.script import ScriptDirectory
    head = ScriptDirectory.from_config(migrate._alembic_config()).get_current_head()
    assert version == head


def test_adopt_refuses_to_stamp_a_mismatched_db(tmp_path, monkeypatch) -> None:
    """A pre-alembic DB whose schema does NOT match the baseline (here: a table
    dropped) must be REFUSED, not falsely stamped current (Carnot cage-match,
    PR#23). Stamping it would mark the DB managed while a table stays missing
    forever (create_all is gone)."""
    import pytest
    from sqlalchemy import text

    async_url, sync_url = _point_app_at(tmp_path, monkeypatch)

    seed = create_engine(sync_url)
    try:
        Base.metadata.create_all(seed)
        # Drift: drop a table so the live schema no longer equals baseline 0001.
        with seed.begin() as conn:
            conn.execute(text("DROP TABLE message_reports"))
    finally:
        seed.dispose()

    with pytest.raises(RuntimeError, match="does not match baseline"):
        migrate.run()

    # And it must NOT have stamped/created anything — still no alembic_version.
    engine = create_engine(sync_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "alembic_version" not in tables
