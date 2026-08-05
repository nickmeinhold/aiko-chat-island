"""Migration correctness (#14) — the gate that replaces dead CI.

Three properties, each a way the migration system could silently lie:

1. **Parity** — ``alembic upgrade head`` on a fresh DB must build the SAME schema
   as the ORM models (``create_all``). Without this, a model change without a
   matching revision drifts the live DB from the code invisibly. This is the
   check ``alembic check`` / a CI autogenerate-diff would run; we run it as a unit
   test because there is no CI (aiko-chat-island#18).

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

# The tables the models define (+ alembic_version once managed).
_MODEL_TABLES = {
    "users", "social_identities", "channels", "memberships",
    "messages", "user_blocks", "message_reports", "device_tokens",
    "oauth_handoffs", "oauth_states", "social_nonces",
    "communities", "community_memberships",
    "recovery_policies", "recovery_approvers", "pending_recovery",
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
        with engine.connect() as conn:
            device_sql = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name='device_tokens'").scalar()
            challenge_sql = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name='passkey_challenges'"
            ).scalar()
            report_sql = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name='message_reports'").scalar()
    finally:
        engine.dispose()
    assert _MODEL_TABLES <= tables
    assert "alembic_version" in tables
    # Structural assertion for the platform CHECK (cage-match Carnot, PR#28). The
    # parity gate's compare_metadata is CHECK-BLIND on SQLite, so a migration that
    # silently allowed an out-of-set platform would pass it — assert the constraint
    # is actually in the migrated DDL, not just the model (the same targeted check
    # 0002's CHECKs got). This is what proves the MIGRATION enforces the closed set,
    # not only the ORM.
    assert "ck_device_tokens_platform" in device_sql
    assert "'apns'" in device_sql and "'fcm'" in device_sql
    # The passkey_challenges.operation CHECK must include the social-recovery
    # 'recover' member (Design 05, migration 0013 widened it). compare_metadata is
    # CHECK-blind on SQLite, so assert it structurally in the migrated DDL — a
    # migration that forgot to widen the constraint would reject every recovery
    # nonce insert at write time (the closed set = reject-all for 'recover').
    assert "ck_passkey_challenges_operation" in challenge_sql
    assert "'register'" in challenge_sql and "'authenticate'" in challenge_sql
    assert "'recover'" in challenge_sql
    # The message_reports.resolution CHECK (Piece B, migration 0014) enforces the
    # closed ReportResolution set. compare_metadata is CHECK-blind on SQLite, so a
    # migration that dropped or mis-spelled the constraint would let a bogus
    # resolution write succeed — assert it structurally in the migrated DDL.
    assert "ck_message_reports_resolution" in report_sql
    assert "'taken_down'" in report_sql and "'dismissed'" in report_sql


def test_adopt_pre_alembic_db_stamps_baseline(tmp_path, monkeypatch) -> None:
    """A real pre-alembic DB (the BASELINE schema, no alembic_version — exactly the
    live prod DB before #14 shipped) is adopted by stamping baseline, then brought
    to head. migrate.run must NOT re-create existing tables.

    We build the pre-alembic DB by upgrading to 0001 then dropping alembic_version
    — that is the true 0001 schema WITHOUT the later 0002 CHECK constraints. Using
    create_all here would instead bake in the current models' CHECKs (a DB that
    never existed in prod) and make 0002's batch rebuild add a duplicate same-named
    CHECK (Carnot cage-match, PR#24)."""
    from alembic import command
    from sqlalchemy import text

    async_url, sync_url = _point_app_at(tmp_path, monkeypatch)

    # 1. Build a genuine pre-alembic DB AT the baseline schema, then un-manage it.
    command.upgrade(migrate._alembic_config(), "0001")
    seed = create_engine(sync_url)
    try:
        with seed.begin() as conn:
            conn.execute(text("DROP TABLE alembic_version"))
    finally:
        seed.dispose()

    # 2. Run the real entrypoint migrator against it.
    migrate.run()  # must adopt (stamp 0001) then upgrade head, not CREATE-existing

    # 3. Managed, brought to head, schema intact, each CHECK present exactly once.
    engine = create_engine(sync_url)
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        with engine.connect() as conn:
            version = conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version").scalar()
            channels_sql = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name='channels'").scalar()
    finally:
        engine.dispose()

    assert "alembic_version" in tables
    assert _MODEL_TABLES <= tables
    # Adoption stamps the baseline THEN upgrades, so an adopted DB ends at HEAD.
    from alembic.script import ScriptDirectory
    head = ScriptDirectory.from_config(migrate._alembic_config()).get_current_head()
    assert version == head
    # 0002's CHECK was applied exactly once (no duplicate from a double-stamp path).
    assert channels_sql.count("ck_channels_join_policy") == 1


def test_forward_migrated_db_is_served_not_upgraded(tmp_path, monkeypatch) -> None:
    """Image ROLLBACK onto a forward-migrated volume (task #11 Temper — the
    CONVERGENT FATAL). The persistent volume was migrated by a NEWER image, so
    ``alembic_version`` names a revision THIS image's ``alembic/versions/`` does not
    contain. The old entrypoint migrator must recognise "the DB is ahead of me",
    SKIP the upgrade, and serve on the existing schema — NOT die fail-closed on
    alembic's "Can't locate revision" (which crash-loops the rolled-back island even
    though the schema is N-1 compatible). It must not stamp down or otherwise mutate.

    Schema N-1 compatibility (the expand/contract discipline) is useless if the boot
    migrator refuses to start against a future ``alembic_version`` — this is the
    boot-half of rollback safety, the flaw the cross-family Temper caught.
    """
    from sqlalchemy import text

    _, sync_url = _point_app_at(tmp_path, monkeypatch)

    # 1. Build a normal managed DB at head.
    migrate.run()

    # 2. Simulate a NEWER image having migrated the volume forward: stamp
    #    alembic_version at a revision this image's scripts don't know.
    future_rev = "9999_from_a_newer_image"
    seed = create_engine(sync_url)
    try:
        with seed.begin() as conn:
            conn.execute(
                text("UPDATE alembic_version SET version_num = :r"),
                {"r": future_rev},
            )
    finally:
        seed.dispose()

    # 3. The rolled-back image boots. This MUST NOT raise (the pre-fix behaviour is
    #    `command.upgrade(head)` dying on the unknown current revision).
    migrate.run()

    # 4. Skipped, not mutated: the future revision is left exactly as-is (no
    #    downgrade, no stamp).
    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            version = conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version").scalar()
    finally:
        engine.dispose()
    assert version == future_rev, (
        "forward-migrated DB was mutated — the boot migrator must leave a "
        f"future alembic_version untouched, got {version!r}")


def test_adopt_refuses_to_stamp_a_mismatched_db(tmp_path, monkeypatch) -> None:
    """A pre-alembic DB whose schema does NOT match the baseline (here: a table
    dropped) must be REFUSED, not falsely stamped current (Carnot cage-match,
    PR#23). Stamping it would mark the DB managed while a table stays missing
    forever (create_all is gone).

    Build a GENUINE baseline DB (upgrade 0001 → drop alembic_version) and then
    introduce the drift, so the only diff is the dropped table — green for the
    RIGHT reason. (Using create_all here would build HEAD models, whose extra
    post-baseline tables like device_tokens already differ from baseline, so the
    refusal would fire regardless of the drop — proving nothing about it.)"""
    import pytest
    from alembic import command
    from sqlalchemy import text

    async_url, sync_url = _point_app_at(tmp_path, monkeypatch)

    command.upgrade(migrate._alembic_config(), "0001")  # genuine baseline schema
    seed = create_engine(sync_url)
    try:
        with seed.begin() as conn:
            conn.execute(text("DROP TABLE alembic_version"))  # un-manage (pre-alembic)
            # Drift: a baseline table goes missing — the ONLY difference vs baseline.
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
