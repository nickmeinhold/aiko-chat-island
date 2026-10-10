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

import re

import pytest
from alembic import command
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


def _norm_check_clause(x: str) -> str:
    """Normalise a CHECK clause for comparison WITHOUT folding the literals' case.

    Whitespace and quote style differ between SQLAlchemy's rendering and the
    migrated DDL; letter case INSIDE the literals does not, and must not be
    erased. SQLite compares `IN (...)` strings under BINARY collation, so a
    migration that wrote 'Taken_Down' against an enum value 'taken_down' refuses
    every legitimate write. These gates once lowercased the whole clause, which
    made exactly that drift compare equal (Carnot, PR #191). Only the keyword is
    case-folded, and it is folded BEFORE whitespace is removed, while the word
    boundary in front of it still exists.
    """
    s = re.sub(r"(?i)\s+in\s*\(", " IN (", str(x))
    return "".join(s.split()).replace('"', "'")


def test_check_clause_norm_keeps_literal_case() -> None:
    """Must-fail control for `_norm_check_clause`: a case-only drift in a
    literal must NOT compare equal, while keyword case and spacing may."""
    assert _norm_check_clause("k IN ('Taken_Down')") != _norm_check_clause("k IN ('taken_down')")
    assert _norm_check_clause('k in ( "a",  "b" )') == _norm_check_clause("k IN ('a','b')")


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
            users_sql = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name='users'").scalar()
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
    # users.kind closed set (#3096, migration 0022). compare_metadata is CHECK-blind
    # on SQLite, so assert it structurally in the migrated DDL — this is what proves
    # the MIGRATION enforces the set, not merely the ORM. Both members asserted so a
    # migration that shipped only 'human' (rejecting every agent insert at write
    # time) fails here rather than at the first enrollment.
    assert "ck_users_kind" in users_sql
    assert "'human'" in users_sql and "'agent'" in users_sql
    # device_tokens.apns_environment closed set (#3386, migrations 0023+0024). Same
    # CHECK-blind reasoning as above. The NOT NULL is asserted too, and it is not
    # redundant with the CHECK: `NULL IN ('sandbox','production')` evaluates to
    # UNKNOWN, which a CHECK constraint PASSES — so a nullable column would leave
    # the closed set with a silent third member that `_host` would then raise on
    # mid-fanout.
    assert "ck_device_tokens_apns_environment" in device_sql
    assert "'sandbox'" in device_sql and "'production'" in device_sql
    # COLUMN-SCOPED, not corpus-scoped (cage-match, Carnot). `"NOT NULL" in
    # device_sql` was true of the whole CREATE TABLE — every other non-null column
    # satisfied it, so the assertion could not go red if apns_environment shipped
    # nullable. Match the column's own clause instead: an instrument that cannot
    # detect the failure is not a check.
    assert re.search(r'apns_environment\s+VARCHAR\(16\)\s+.*?NOT NULL',
                     device_sql, re.IGNORECASE), (
        f"apns_environment is not NOT NULL in the migrated DDL:\n{device_sql}")


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


FUTURE_REV = "9999_from_a_newer_image"


def _versions(sync_url: str) -> set[str]:
    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return {r[0] for r in conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version").fetchall()}
    finally:
        engine.dispose()


def _stamp_ahead(sync_url: str, *, keep_known_head: bool) -> set[str]:
    """Simulate a NEWER image having migrated the volume: the version table names a
    revision this image's scripts don't contain. ``keep_known_head`` adds the
    unknown row BESIDE the real head (a branched/merged table, Wu cage-match PR#116)
    instead of replacing it. Returns the version rows as seeded."""
    from sqlalchemy import text

    seed = create_engine(sync_url)
    try:
        with seed.begin() as conn:
            sql = ("INSERT INTO alembic_version (version_num) VALUES (:r)"
                   if keep_known_head else
                   "UPDATE alembic_version SET version_num = :r")
            conn.execute(text(sql), {"r": FUTURE_REV})
    finally:
        seed.dispose()
    return _versions(sync_url)


@pytest.mark.parametrize("keep_known_head", [False, True],
                         ids=["ahead", "mixed-known-and-unknown"])
def test_unknown_revision_refuses_to_start(tmp_path, monkeypatch, keep_known_head) -> None:
    """Compat guard (design 18 §1): an image that meets a DB stamped at a revision it
    doesn't know REFUSES TO START, in the migrator, before uvicorn, with the right
    diagnosis (image older than schema) and a fix that works (stop the stack, restore
    the pre-update backup). PR#116's migrator skipped instead, and lifespan's
    ``_assert_at_head`` then refused with advice that could not work; nothing ever
    served. Both shapes refuse: a table that is simply ahead, and one carrying a known
    head beside the unknown row. Neither is mutated on the way out.
    """
    _, sync_url = _point_app_at(tmp_path, monkeypatch)
    migrate.run()  # managed DB at head
    before = _stamp_ahead(sync_url, keep_known_head=keep_known_head)

    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNKNOWN_REVISION"):
        migrate.run()

    assert _versions(sync_url) == before, (
        "a refusing boot mutated the version table; refusal must leave the volume "
        "exactly as the newer image left it")


@pytest.mark.parametrize("keep_known_head", [False, True],
                         ids=["ahead", "mixed-known-and-unknown"])
def test_both_boot_layers_refuse_a_forward_migrated_db(
        tmp_path, monkeypatch, keep_known_head) -> None:
    """The migrator and lifespan's ``verify_schema()`` must AGREE that an image older
    than the schema does not serve. PR#116 relaxed only the migrator (skip and
    "serve"), while ``db._assert_at_head`` kept refusing, so its serve-anyway was
    unreachable for two months and its tests, which never booted past
    ``migrate.run()``, could not see that. A serve-anyway reintroduced in ONE layer
    goes red here instead of shipping as a dead switch (PR#392 review).
    """
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from aiko_gateway import db

    async_url, sync_url = _point_app_at(tmp_path, monkeypatch)
    migrate.run()
    _stamp_ahead(sync_url, keep_known_head=keep_known_head)

    with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNKNOWN_REVISION") as refused:
        migrate.run()
    # The advice is the load, not the marker (Tesla, PR#392 r2): the message must
    # send the operator to a stopped-stack restore, never to `alembic upgrade head`.
    assert "docker compose stop" in str(refused.value)
    assert "restore the database backup" in str(refused.value)
    assert "upgrade head" not in str(refused.value)

    # Lifespan reaches the SAME verdict through the SAME predicate
    # (migrate.schema_status), so it raises the same refusal for both shapes. Before
    # the shared predicate, the mixed shape died inside alembic's single-revision
    # lookup with an unrelated error (Carnot, Tesla, PR#392 r3).
    engine = create_async_engine(async_url)
    monkeypatch.setattr(db, "engine", engine)
    try:
        with pytest.raises(RuntimeError, match="MIGRATE_REFUSE_UNKNOWN_REVISION") as lifespan:
            asyncio.run(db.verify_schema())
        assert "lifespan check" in str(lifespan.value)
    finally:
        asyncio.run(engine.dispose())


def test_lifespan_refuses_a_db_left_behind_head(tmp_path, monkeypatch) -> None:
    """The other branch of the shared predicate: a DB whose stamp is KNOWN but not
    head (uvicorn started directly, bypassing the entrypoint) is BEHIND, and lifespan
    refuses with the upgrade advice that is right for that case and only that case.
    Previously untested; PR#392 rewrote this branch onto ``schema_status``."""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from aiko_gateway import db

    async_url, _ = _point_app_at(tmp_path, monkeypatch)
    command.upgrade(migrate._alembic_config(), "0027")  # one short of head
    assert migrate.asyncio.run(migrate._schema_status()).state is migrate.SchemaState.BEHIND

    engine = create_async_engine(async_url)
    monkeypatch.setattr(db, "engine", engine)
    try:
        with pytest.raises(RuntimeError, match="Refusing to serve a stale schema") as behind:
            asyncio.run(db.verify_schema())
        assert "alembic upgrade head" in str(behind.value)
        assert "MIGRATE_REFUSE_UNKNOWN_REVISION" not in str(behind.value)
    finally:
        asyncio.run(engine.dispose())


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


def test_0023_backfills_existing_rows_from_the_island_setting(
    tmp_path, monkeypatch
) -> None:
    """A row registered BEFORE the column existed was minted under whatever the
    island's global flag said, so that is the only honest value to backfill it
    with — not the DDL's static 'production' default (#3386).

    Both live islands hold zero device_tokens rows today, so this is a
    correctness test rather than a description of a migration anyone will watch
    run. It pins the distinction that matters: static DDL (identical on every
    island, so the parity gate cannot depend on a box's .env) and settings-aware
    DATA. A migration that skipped the UPDATE would silently route every
    pre-existing sandbox token to the production host on first ring.
    """
    _, sync_url = _point_app_at(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "apns_use_sandbox", True, raising=False)

    command.upgrade(migrate._alembic_config(), "0022")  # stop one short: no push_environment yet
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO users "
                "(id, username, aiko_username, display_name, created_at, kind) "
                "VALUES ('01USERAAAAAAAAAAAAAAAAAAAA', 'u', 'u', 'U', "
                "'2026-08-25T00:00:00+00:00', 'human')")
            conn.exec_driver_sql(
                "INSERT INTO device_tokens "
                "(id, user_id, platform, token, created_at, updated_at) VALUES "
                "('01TOKENAAAAAAAAAAAAAAAAAAA', '01USERAAAAAAAAAAAAAAAAAAAA', "
                "'apns', 'legacy-token', '2026-08-25T00:00:00+00:00', "
                "'2026-08-25T00:00:00+00:00')")

        command.upgrade(migrate._alembic_config(), "0023")

        with engine.connect() as conn:
            value = conn.exec_driver_sql(
                "SELECT push_environment FROM device_tokens "
                "WHERE id='01TOKENAAAAAAAAAAAAAAAAAAA'").scalar()
    finally:
        engine.dispose()
    assert value == "sandbox", (
        "a pre-existing row kept the static DDL default instead of the "
        "environment it was actually registered under")


def test_0024_rename_preserves_the_value_a_live_row_already_carries(
    tmp_path, monkeypatch
) -> None:
    """0024 renames push_environment -> apns_environment by REBUILDING the table
    (SQLite cannot alter a CHECK), and a rebuild is exactly where a value can be
    silently re-defaulted. imagineering holds one real production APNs token that
    0023 stamped 'sandbox'; if the copy dropped that and took the DDL default
    ('production') instead, a live handset would move to the wrong Apple host and
    every ring would 400 with nobody watching.

    The island setting is flipped to production BETWEEN 0023 and 0024 on purpose.
    That is what makes this able to go red: 0024 must be a pure name change that
    never re-reads settings, so the row must still say 'sandbox' afterwards even
    though the box now says otherwise. Without the flip the assertion would pass
    for a migration that re-derived the value from config, which is the bug.

    The downgrade is exercised in the same test rather than a separate one — the
    rename is symmetric, and a reversal that lost the value would be a rollback
    that costs a device its environment (this repo has a crucible that converged
    FATAL on image-rollback-is-not-DB-rollback; a lossy downgrade is that hazard
    at the column level).
    """
    _, sync_url = _point_app_at(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "apns_use_sandbox", True, raising=False)

    command.upgrade(migrate._alembic_config(), "0022")
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO users "
                "(id, username, aiko_username, display_name, created_at, kind) "
                "VALUES ('01USERAAAAAAAAAAAAAAAAAAAA', 'u', 'u', 'U', "
                "'2026-08-25T00:00:00+00:00', 'human')")
            conn.exec_driver_sql(
                "INSERT INTO device_tokens "
                "(id, user_id, platform, token, created_at, updated_at) VALUES "
                "('01TOKENAAAAAAAAAAAAAAAAAAA', '01USERAAAAAAAAAAAAAAAAAAAA', "
                "'apns', 'live-token', '2026-08-25T00:00:00+00:00', "
                "'2026-08-25T00:00:00+00:00')")

        command.upgrade(migrate._alembic_config(), "0023")
        # The box changes its mind after the row was stamped. 0024 must not care.
        monkeypatch.setattr(settings, "apns_use_sandbox", False, raising=False)
        command.upgrade(migrate._alembic_config(), "0024")

        with engine.connect() as conn:
            value = conn.exec_driver_sql(
                "SELECT apns_environment FROM device_tokens "
                "WHERE id='01TOKENAAAAAAAAAAAAAAAAAAA'").scalar()
            device_sql = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='device_tokens'").scalar()
        assert value == "sandbox", (
            "the rebuild lost the row's environment and re-defaulted it — a live "
            "token just moved to the wrong APNs host")
        assert "push_environment" not in device_sql, (
            "the old column name survived the rename:\n" + device_sql)
        assert "ck_device_tokens_apns_environment" in device_sql, (
            "the CHECK was not renamed with its column:\n" + device_sql)

        command.downgrade(migrate._alembic_config(), "0023")
        with engine.connect() as conn:
            back = conn.exec_driver_sql(
                "SELECT push_environment FROM device_tokens "
                "WHERE id='01TOKENAAAAAAAAAAAAAAAAAAA'").scalar()
        assert back == "sandbox", (
            "the downgrade lost the row's environment on the way back")
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# THE MIGRATION-vs-ENUM PARITY TEST WAS DELETED HERE (Carnot, cage-match PR#170 r5),
# and the deletion is the fix rather than a retreat from one.
#
# It asserted that revision 0025's frozen `_KIND_CHECK` literal must equal what
# `_in_check` renders from the LIVE TokenKind enum. That pins a HISTORICAL ARTIFACT
# to CURRENT CODE, which is the wrong invariant for a migration chain: the day a
# future revision correctly adds a member and rebuilds the CHECK in 0026, this test
# goes red and STAYS red, and the only ways out are editing history or deleting the
# test. It cannot distinguish real head-schema drift from a correct later migration.
#
# The invariant actually worth holding is "the migrated HEAD's DDL matches the
# model", and `test_the_migrated_ddl_actually_carries_the_check` below already
# asserts exactly that — against the emitted DDL and a behavioural reject, not
# against a literal. So the parity test was both wrong and redundant with a test
# that is right. Subtracting it is the smaller system.
# ---------------------------------------------------------------------------
def test_the_0025_column_is_wide_enough_for_every_member(tmp_path, monkeypatch) -> None:
    """THE SHADOW CLOSED SET — and this test could not catch the bug it exists for
    (Tesla, cage-match PR#170 r4).

    VARCHAR width is a second, quieter constraint beside the CHECK: unenforced on
    SQLite, enforced on Postgres, so it is invisible where we run and bites where we
    might. `_KIND_WIDTH = 16` exists because `String(8)` was the original value and
    would have swallowed a future `background` (10) or `liveactivity` (12).

    The first version asserted `model_width == _KIND_WIDTH` and both `>= longest` —
    where `longest` is `max(len(m.value) for m in TokenKind)` = len('alert') = 5. So
    a regression of BOTH declarations back to `String(8)` passed: 8 == 8, and 8 >= 5.
    The guard was blind to precisely the defect that motivated it.

    Two corrections, both from Tesla:
      * the floor is the set we might ADOPT, not the set we hold. These values are
        Apple's own `apns-push-type` spellings, so the floor is the longest of those,
        not the longest of the two members we happen to use today.
      * strike the EMITTED DDL, not the constant that claims to have produced it.
        `upgrade()` can pass `sa.String(8)` while `_KIND_WIDTH` stays 16 and no
        assertion above would flicker.
    """
    import importlib.util
    import re as _re
    import sqlite3
    from pathlib import Path
    from alembic import command
    from aiko_gateway import migrate
    from aiko_gateway.domain.models import DeviceToken, TokenKind

    # The widest apns-push-type spelling Apple currently defines. The floor is a
    # fact about the VOCABULARY, not about our current membership — which is the
    # whole reason 8 was wrong while every value we stored still fitted in it.
    WIDEST_PUSH_TYPE = len("liveactivity")  # 12

    rev = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0025_device_token_kind.py"
    spec = importlib.util.spec_from_file_location("rev_0025b", rev)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    model_width = DeviceToken.__table__.c.token_kind.type.length
    assert model_width == mod._KIND_WIDTH, (
        f"the ORM column is String({model_width}) and revision 0025 writes "
        f"String({mod._KIND_WIDTH}). SQLite does not enforce VARCHAR width so both "
        "look fine where we run; Postgres does.")
    assert model_width >= WIDEST_PUSH_TYPE, (
        f"String({model_width}) cannot hold the longest apns-push-type spelling "
        f"({WIDEST_PUSH_TYPE} chars, 'liveactivity'). A floor of "
        f"max(len(m.value) for m in TokenKind) = "
        f"{max(len(m.value) for m in TokenKind)} would admit String(8) — the exact "
        "regression this test exists to lock.")

    # THE EMITTED DDL, not the constant. Read VARCHAR(n) back out of the migrated
    # schema so a revision that passes a different width to sa.String() is caught.
    _async_url, sync_url = _point_app_at(tmp_path, monkeypatch)
    command.upgrade(migrate._alembic_config(), "head")
    con = sqlite3.connect(sync_url.replace("sqlite:///", ""))
    ddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='device_tokens'"
    ).fetchone()[0]
    con.close()

    emitted = _re.search(r"token_kind\s+VARCHAR\((\d+)\)", ddl, _re.I)
    assert emitted, f"could not read token_kind's emitted width from: {ddl!r}"
    assert int(emitted.group(1)) == mod._KIND_WIDTH, (
        f"the migrated DDL declares VARCHAR({emitted.group(1)}) while _KIND_WIDTH is "
        f"{mod._KIND_WIDTH} — the constant does not describe what upgrade() emitted.")


def test_rows_written_at_0024_read_alert_at_0025(tmp_path, monkeypatch) -> None:
    """`server_default='alert'` IS THE BACKFILL, and this is the proof.

    0023 needed a settings-aware UPDATE because its safe DDL default and its
    honest per-island value DIFFERED. Here they are the same constant — the wire
    contract says an absent `token_kind` means alert, and an existing row is
    exactly a row whose client never declared one — so no data migration exists
    and none is owed.

    The island setting is flipped between the revisions on purpose, mirroring the
    0024 test: 0025 must not read config at all, so nothing about the box may
    change the answer. Without the flip this would pass for a migration that
    re-derived the value from settings.
    """
    _, sync_url = _point_app_at(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "apns_use_sandbox", True, raising=False)

    command.upgrade(migrate._alembic_config(), "0024")  # stop one short
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO users "
                "(id, username, aiko_username, display_name, created_at, kind) "
                "VALUES ('01USERAAAAAAAAAAAAAAAAAAAA', 'u', 'u', 'U', "
                "'2026-09-09T00:00:00+00:00', 'human')")
            conn.exec_driver_sql(
                "INSERT INTO device_tokens "
                "(id, user_id, platform, token, apns_environment, created_at, "
                " updated_at) VALUES "
                "('01TOKENAAAAAAAAAAAAAAAAAAA', '01USERAAAAAAAAAAAAAAAAAAAA', "
                "'apns', 'live-token', 'sandbox', '2026-09-09T00:00:00+00:00', "
                "'2026-09-09T00:00:00+00:00')")

        monkeypatch.setattr(settings, "apns_use_sandbox", False, raising=False)
        command.upgrade(migrate._alembic_config(), "0025")

        with engine.connect() as conn:
            kind, env = conn.exec_driver_sql(
                "SELECT token_kind, apns_environment FROM device_tokens "
                "WHERE id='01TOKENAAAAAAAAAAAAAAAAAAA'").one()

        # The rebuild must not lose the neighbour column either — 0024's lesson
        # was that a SQLite table rebuild is exactly where a value gets silently
        # re-defaulted, and 0025 rebuilds the same table again.
        assert kind == "alert", (
            "a row that predates token_kind did not read as alert — the "
            "server_default is not doing the backfill it was chosen for")
        # THE DISCRIMINATING FIXTURE (Tesla, cage-match PR#170 r3). This stored
        # 'production' — the column's OWN server_default — and asserted 'production'
        # back, with the island flipped to production as well. So a rebuild that
        # silently re-defaulted the column passed. A rebuild that re-derived it from
        # settings passed. EVERY failure mode of the neighbour agreed with the
        # assertion, which is the whole reason 0024 exists as a lesson.
        #
        # 0024's test had power because it stored 'sandbox' against a 'production'
        # default on a flipped box. This copy kept the choreography and inverted the
        # fixture. imagineering holds a real token that 0023 stamped 'sandbox' and
        # 0025 rebuilds that table: a silent re-default there is every push 400ing
        # BadDeviceToken, never reaped, no ring.
        assert env == "sandbox", (
            "0025's rebuild re-defaulted apns_environment — a live sandbox-stamped "
            "token would now be routed to the production APNs host, 400 "
            "BadDeviceToken on every push, never reaped, no ring")

        command.downgrade(migrate._alembic_config(), "0024")
        with engine.connect() as conn:
            cols = {r[1] for r in conn.exec_driver_sql(
                "PRAGMA table_info(device_tokens)").fetchall()}
            survived = conn.exec_driver_sql(
                "SELECT apns_environment FROM device_tokens").scalar()
    finally:
        engine.dispose()
    assert "token_kind" not in cols
    assert survived == "sandbox", (
        "the downgrade rebuild lost a live row's environment")


def test_the_migrated_ddl_actually_carries_the_check(tmp_path, monkeypatch) -> None:
    """THE TUNING FORK HELD TO THE BELL, NOT THE SCORE (Tesla, PR#170 r2).

    The parity test above compares two SOURCE literals. It would stay green if
    `upgrade()` never applied the constraint at all — the 0025 docstring claims the
    check is asserted "structurally in the migrated DDL", and until this test that
    sentence was false. Revision 0024 DID inspect `sqlite_master`; this one did not
    inherit the habit.

    So: drive the real migration and read the constraint out of the database.
    """
    import sqlite3
    from alembic import command
    from aiko_gateway import migrate
    from aiko_gateway.domain.models import TokenKind, _in_check

    _async_url, sync_url = _point_app_at(tmp_path, monkeypatch)
    command.upgrade(migrate._alembic_config(), "head")

    con = sqlite3.connect(sync_url.replace("sqlite:///", ""))
    ddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='device_tokens'"
    ).fetchone()[0]
    con.close()

    assert "ck_device_tokens_token_kind" in ddl, (
        "the migrated device_tokens table carries no token_kind CHECK — the "
        "constraint the parity test compares literals about never reached the DB")
    # EXTRACT THE CLAUSE, do not scan the church (Tesla, cage-match PR#170 r3).
    # This looped over the whole CREATE TABLE, which already contains
    # DEFAULT 'alert' — so a CHECK emitted as `IN ('voip')` alone kept this green,
    # because 'alert' was found in the default rather than in the constraint. The
    # member the backfill actually writes is precisely the one the loop could not
    # see.
    # BALANCED extraction. A naive `\(([^)]*)\)` stops at the first close paren,
    # which is the one inside `IN ('alert', 'voip')` — so the captured clause was
    # missing its tail and could never equal what _in_check renders. A parser that
    # silently truncates its input is the same class as a fixture that cannot
    # discriminate: the comparison runs, and it is comparing the wrong thing.
    import re as _re

    def _check_clause(ddl_text: str, name: str) -> str | None:
        anchor = _re.search(rf"{name}\s+CHECK\s*\(", ddl_text, _re.I)
        if not anchor:
            return None
        i = anchor.end()          # first char inside the CHECK's open paren
        depth = 1
        for j in range(i, len(ddl_text)):
            if ddl_text[j] == "(":
                depth += 1
            elif ddl_text[j] == ")":
                depth -= 1
                if depth == 0:
                    return ddl_text[i:j]
        return None

    clause_text = _check_clause(ddl, "ck_device_tokens_token_kind")
    assert clause_text, f"could not extract the token_kind CHECK clause from: {ddl!r}"
    m = _re.match(r"(.*)", clause_text, _re.S)
    clause = m.group(1)

    # THE TARGET EXPRESSION, not just the literals (Carnot, PR#170 r4). Scanning the
    # clause for each member is satisfied by `platform IN ('alert','voip')` or even
    # `'alert' IN ('alert','voip')` — every literal is present and the constraint
    # constrains the wrong thing, or nothing. Compare against what _in_check renders
    # so the column being constrained is part of the assertion.
    assert _norm_check_clause(clause) == _norm_check_clause(_in_check("token_kind", TokenKind)), (
        f"the migrated CHECK clause is {clause!r}, which is not what _in_check "
        f"renders ({_in_check('token_kind', TokenKind)!r}). Matching member literals "
        "is not enough — a constraint on the wrong column contains them all.")

    # THE WORK OUTPUT, not the nameplate (Carnot, cage-match PR#170 r3). Everything
    # above is still a READ of generated text: a CHECK attached to a DIFFERENT
    # column, or a dead literal, satisfies all of it. The only measurement that
    # binds the constraint to THIS column is making the database refuse a bad value.
    # THE FIXTURE MUST FAIL FOR THE RIGHT REASON (Carnot, cage-match PR#170 r4).
    # The first version inserted user_id='u1' with no matching users row.
    # device_tokens.user_id is an FK onto users.id, and SQLite's foreign_keys pragma
    # is OFF by default — so TODAY the CHECK is what rejects it, but the assertion
    # could not tell CHECK enforcement from FK enforcement. Turn FKs on (now, or by
    # someone later) and this silently becomes a foreign-key test that passes with
    # the CHECK absent or attached to the wrong column. Same non-discriminating
    # class this test was written to close, one layer down.
    con = sqlite3.connect(sync_url.replace("sqlite:///", ""))
    con.execute("PRAGMA foreign_keys=ON")   # remove the ambiguity rather than rely on the default
    con.execute("INSERT INTO users (id, username, display_name, aiko_username, "
                "created_at) VALUES "
                "('u1','u1','U One','u1','2026-01-01 00:00:00')")
    con.commit()

    # POSITIVE CONTROL FIRST: the same user and a VALID kind must succeed. Without
    # it, the refusal below proves only "this INSERT always fails".
    con.execute("INSERT INTO device_tokens (id, user_id, platform, token, "
                "token_kind, apns_environment, created_at, updated_at) VALUES "
                "('ok','u1','apns','good','alert','production',"
                "'2026-01-01 00:00:00','2026-01-01 00:00:00')")
    con.commit()

    try:
        con.execute("INSERT INTO device_tokens (id, user_id, platform, token, "
                    "token_kind, apns_environment, created_at, updated_at) VALUES "
                    "('t1','u1','apns','x','shout','production',"
                    "'2026-01-01 00:00:00','2026-01-01 00:00:00')")
        con.commit()
        err = None
    except sqlite3.IntegrityError as ex:
        err = str(ex)
    finally:
        con.close()

    assert err is not None, (
        "the migrated DB accepted token_kind='shout' — the CHECK is present in the "
        "DDL text but is not enforcing on this column. A constraint that reads "
        "correctly and rejects nothing is the exact shape this test exists to catch.")
    assert "CHECK" in err.upper(), (
        f"the insert was rejected, but not by a CHECK — {err!r}. The row's user "
        "exists and the only invalid field is token_kind, so anything other than a "
        "CHECK failure means this test is measuring a different constraint.")


    # AND THE NEIGHBOURS SURVIVED THE REBUILD (Tesla, cage-match PR#170 r5).
    # This asked only about the constraint 0025 ADDS. Revision 0024 exists because a
    # batch_alter_table rebuild silently altered a neighbour, and 0025 rebuilds the
    # SAME table again — but `compare_metadata` is CHECK-blind on SQLite by this
    # file's own admission, so a batch copy that keeps token_kind and DROPS the
    # sibling CHECKs would not flicker anywhere. The values would still copy, so
    # even the sandbox-neighbour test stays green; the closed set on
    # apns_environment would quietly become prose.
    for neighbour in ("ck_device_tokens_platform",
                      "ck_device_tokens_apns_environment"):
        assert neighbour in ddl, (
            f"{neighbour} is absent from the migrated device_tokens DDL — 0025's "
            "rebuild dropped a constraint it did not own. The column's values "
            "survive the copy, so nothing else in this suite can see it.")


def test_downgrading_0025_refuses_while_a_voip_row_exists(tmp_path, monkeypatch) -> None:
    """LOSSY IN THE ONE DIRECTION THAT MATTERS (Carnot, cage-match PR#170 r3).

    `token_kind` is the ONLY durable distinction between a UIKit alert token and a
    PushKit VoIP token — both are 64-hex strings. Dropping the column after any VoIP
    row exists leaves them indistinguishable under the old absent-means-alert
    contract, so they silently become alert tokens and every push to them 400s with
    no reap and no ring: the exact failure this revision exists to prevent, produced
    by its own rollback.
    """
    import sqlite3
    from alembic import command
    from aiko_gateway import migrate

    _async_url, sync_url = _point_app_at(tmp_path, monkeypatch)
    command.upgrade(migrate._alembic_config(), "head")
    path = sync_url.replace("sqlite:///", "")

    # An alert-only DB downgrades cleanly — the expected case, and the control
    # WITHOUT WHICH the refusal below would prove only "downgrade always fails".
    # A VALID PARENT ROW AND FKs ON (Carnot, PR#170 r5). Round 4 fixed exactly this
    # ambiguity in the DDL test and left its sibling here — device_tokens.user_id is
    # an FK onto users.id, so with SQLite's default foreign_keys=OFF this fixture
    # measured SQLite's indulgence as much as the downgrade guard, and would fail
    # for the wrong reason the moment FKs are enabled. The only discriminant must be
    # token_kind != 'alert'.
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("INSERT INTO users (id, username, display_name, aiko_username, "
                "created_at) VALUES "
                "('u1','u1','U One','u1','2026-01-01 00:00:00')")
    con.execute("INSERT INTO device_tokens (id, user_id, platform, token, "
                "token_kind, apns_environment, created_at, updated_at) VALUES "
                "('a1','u1','apns','aaa','alert','production',"
                "'2026-01-01 00:00:00','2026-01-01 00:00:00')")
    con.commit(); con.close()
    command.downgrade(migrate._alembic_config(), "0024")
    command.upgrade(migrate._alembic_config(), "head")

    # Now one VoIP row: the downgrade must refuse and SAY WHAT IT WOULD DESTROY.
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("INSERT INTO device_tokens (id, user_id, platform, token, "
                "token_kind, apns_environment, created_at, updated_at) VALUES "
                "('v1','u1','apns','vvv','voip','production',"
                "'2026-01-01 00:00:00','2026-01-01 00:00:00')")
    con.commit(); con.close()

    with pytest.raises(Exception) as ei:
        command.downgrade(migrate._alembic_config(), "0024")
    assert "refusing to downgrade 0025" in str(ei.value), (
        f"expected the guard's refusal, got {ei.value!r}")

    # And the column survived the refusal — a guard that aborts halfway is worse
    # than no guard, because it destroys data AND reports failure.
    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(device_tokens)")]
    kinds = [r[0] for r in con.execute("SELECT token_kind FROM device_tokens ORDER BY id")]
    con.close()
    assert "token_kind" in cols, "the refused downgrade dropped the column anyway"
    assert kinds == ["alert", "voip"], f"rows were altered by a refused downgrade: {kinds}"


def test_the_0026_install_id_width_is_one_constant_in_four_places() -> None:
    """THE WIDTH IS A SHADOW CLOSED SET, and it is written down four times.

    `install_id` carries no CHECK — it is an OPEN set — so its only bound is
    length, and that bound is asserted by the ORM column, revision 0026's
    `_INSTALL_WIDTH`, the service door's `INSTALL_ID_MAX_LENGTH`, and the REST
    model's `max_length`. SQLite does not enforce VARCHAR width, so three of those
    four can drift apart with the suite entirely green and the divergence only
    appears on Postgres, or as a value the wire accepts and the door refuses.

    This is 0025's `_KIND_WIDTH` lesson applied at authoring time rather than at
    round 5 of a cage-match: a hand-picked constant does not track anything by
    itself, so what gets enforced is the EQUALITY, not the number.
    """
    import importlib.util
    from pathlib import Path

    from aiko_gateway.domain.devices_service import INSTALL_ID_MAX_LENGTH
    from aiko_gateway.domain.models import DeviceToken
    from aiko_gateway.rest.devices import RegisterDeviceReq

    rev = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
           / "0026_device_token_install_id.py")
    spec = importlib.util.spec_from_file_location("_rev0026", rev)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    orm_width = DeviceToken.__table__.c.install_id.type.length
    wire_width = next(
        m.max_length
        for m in RegisterDeviceReq.model_fields["install_id"].metadata
        if getattr(m, "max_length", None) is not None)

    assert orm_width == mod._INSTALL_WIDTH == INSTALL_ID_MAX_LENGTH == wire_width, (
        f"the install_id width disagrees across its four declarations: ORM "
        f"String({orm_width}), revision 0026 {mod._INSTALL_WIDTH}, service door "
        f"{INSTALL_ID_MAX_LENGTH}, wire model {wire_width}. SQLite enforces none "
        "of them, so nothing else will notice.")


def test_the_0026_column_is_nullable_with_no_default(tmp_path, monkeypatch) -> None:
    """THE SAFETY PROPERTY OF THE WHOLE CHANGE, read off the MIGRATED DDL.

    NULL means "this client did not say which handset". A NOT NULL column, or any
    server_default, would assert an identity nobody established, and the first
    reader of this column would then group unrelated rows: arm (A)'s missed call,
    arrived at by a backfill. 0025's server_default WAS its backfill; this
    column's absence of one is the same argument run the other way, and it is
    only a guarantee if something reads the DDL.
    """
    from alembic import command
    from sqlalchemy import create_engine, inspect

    _async_url, sync_url = _point_app_at(tmp_path, monkeypatch)
    command.upgrade(migrate._alembic_config(), "head")

    engine = create_engine(sync_url, future=True)
    try:
        col = next(c for c in inspect(engine).get_columns("device_tokens")
                   if c["name"] == "install_id")
    finally:
        engine.dispose()

    assert col["nullable"] is True, (
        "install_id is NOT NULL in the migrated schema — every pre-existing row "
        "would need a value, and any value invented for them asserts a shared "
        "handset that nothing established")
    assert col["default"] is None, (
        f"install_id carries a server_default ({col['default']!r}); a default "
        "would put every row that never declared an identity into one handset, "
        "which is a missed call waiting for the first reader of this column")


def test_sender_kind_check_literal_matches_the_enum(tmp_path, monkeypatch) -> None:
    """Parity gate for ck_messages_sender_kind (#3144 — the sender_kind closed set).

    Revision 0027 hand-writes its CHECK literal because alembic's compare_metadata
    is CHECK-blind on SQLite, so nothing but this test stops the migration's set and
    the SenderKind enum from drifting apart. Both halves are asserted the way PR#170
    (token_kind) established: the SOURCE literals must agree, AND the constraint must
    actually be in the migrated DDL attached to the right column, AND the database
    must refuse an out-of-set value. Any one alone stays green while the others rot.
    """
    import re as _re
    import sqlite3
    from alembic import command
    from aiko_gateway import migrate
    from aiko_gateway.domain.models import SenderKind, _in_check

    _async_url, sync_url = _point_app_at(tmp_path, monkeypatch)
    command.upgrade(migrate._alembic_config(), "head")

    con = sqlite3.connect(sync_url.replace("sqlite:///", ""))
    ddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()[0]

    assert "ck_messages_sender_kind" in ddl, (
        "the migrated messages table carries no sender_kind CHECK — 0027 did not "
        "reach the DB")

    # Balanced extraction, same reason as the token_kind gate above: a naive
    # `\(([^)]*)\)` stops at the close paren inside `IN ('human', 'agent', 'unknown')`
    # and compares a truncated clause that can never match.
    def _check_clause(ddl_text: str, name: str) -> str | None:
        anchor = _re.search(rf"{name}\s+CHECK\s*\(", ddl_text, _re.I)
        if not anchor:
            return None
        i = anchor.end()
        depth = 1
        for j in range(i, len(ddl_text)):
            if ddl_text[j] == "(":
                depth += 1
            elif ddl_text[j] == ")":
                depth -= 1
                if depth == 0:
                    return ddl_text[i:j]
        return None

    clause = _check_clause(ddl, "ck_messages_sender_kind")
    assert clause, f"could not extract the sender_kind CHECK clause from: {ddl!r}"

    # Compare the TARGET EXPRESSION, not just the member literals: scanning for
    # 'human'/'agent'/'unknown' is satisfied by a CHECK on the wrong column that
    # happens to contain them.
    assert _norm_check_clause(clause) == _norm_check_clause(_in_check("sender_kind", SenderKind)), (
        f"the migrated CHECK clause is {clause!r}, which is not what _in_check "
        f"renders ({_in_check('sender_kind', SenderKind)!r})")

    # THE WORK OUTPUT, not the nameplate: everything above still reads generated
    # text. Only a refused write binds the constraint to this column.
    con.execute(
        "INSERT INTO channels (id, name, kind, aiko_channel, is_private, "
        "join_policy, community_id, created_at) VALUES "
        "('pc1','c','standard','aiko/pc',0,'invite_only',?, '2026-01-01T00:00:00+00:00')",
        ("0" * 26,))
    con.execute(
        "INSERT INTO messages (id, channel_id, sender_user_id, sender_kind, body, "
        "aiko_origin, created_at) VALUES "
        "('pm1','pc1',NULL,'unknown','hi',0,'2026-01-01T00:00:00+00:00')")
    try:
        # NAME THE CONSTRAINT. A bare raises() hears any integrity failure as proof
        # this CHECK fired — a PK collision, a NOT NULL, an FK — so the assertion
        # would stay green while observing something it never measured. Third
        # instance of one class in this change (Tesla found the first in
        # test_check_constraints; a class sweep found this one and its sibling
        # below), which is why it is fixed as a class rather than per finding.
        with pytest.raises(sqlite3.IntegrityError) as exc:
            con.execute(
                "INSERT INTO messages (id, channel_id, sender_user_id, sender_kind, "
                "body, aiko_origin, created_at) VALUES "
                "('pm2','pc1',NULL,'hologram','hi',0,'2026-01-01T00:00:00+00:00')")
        assert "ck_messages_sender_kind" in str(exc.value) or "CHECK" in str(exc.value), (
            "'hologram' was refused, but not demonstrably by the sender_kind CHECK — "
            f"this gate cannot tell you the constraint reached the DB. Error: {exc.value}")
    finally:
        con.close()


def test_0027_renames_actor_rows_and_then_closes_the_set(tmp_path, monkeypatch) -> None:
    """The DATA half of 0027, which the parity gate above cannot reach.

    That test drives a FRESH database, so it proves the CHECK exists and bites but
    never exercises the UPDATE — there are no legacy rows to convert. This one stops
    at 0026, seeds the exact shape both live islands hold, then upgrades and reads
    the rows back.

    It also pins the ORDER, which is the part that could silently rot. The UPDATE
    must run BEFORE create_check_constraint: batch_alter_table rebuilds `messages`
    and copies every row through, so a surviving 'actor' row would fail the new
    constraint mid-rebuild and abort the migration on a live box. Swap the two
    statements in 0027 and this test goes red rather than discovering it during a
    production deploy.

    Seeded counts are the measured live ones (chat.enspyr.co, 2026-09-20): 'human'
    rows that must be untouched alongside 'actor' rows that must all move.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    _async_url, sync_url = _point_app_at(tmp_path, monkeypatch)
    command.upgrade(migrate._alembic_config(), "0026")

    engine = create_engine(sync_url)
    # NO community seed here, and NO try/except around the setup. An earlier version
    # of this test inserted a community inside a bare `except Exception: pass`, and
    # the insert named a `slug` column that does not exist on this table — so it threw
    # on every run and the swallow hid it. The test still passed (FK enforcement is off
    # per ISL-0002, so the channel below did not need a real parent), which made the
    # fixture unable to distinguish "already seeded" from "my SQL is wrong" while it
    # was in fact the second. Migration 0009 already seeds the default community, so
    # the insert was never needed; asserting that is both the fix and the guard.
    with engine.connect() as c:
        seeded = c.execute(text(
            "SELECT id FROM communities WHERE id = :cid"
        ), {"cid": "0" * 26}).scalar_one_or_none()
    assert seeded == "0" * 26, (
        "migration 0009 should have seeded the default community by revision 0026; "
        f"found {seeded!r}. The channel insert below depends on it.")
    try:
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO channels (id, name, kind, aiko_channel, is_private, "
                "join_policy, community_id, created_at) VALUES "
                "('mc1','c','standard','aiko/mc',0,'invite_only',:cid,:ts)"
            ), {"cid": "0" * 26, "ts": "2026-01-01T00:00:00+00:00"})
            for mid, kind in (("ma1", "actor"), ("ma2", "actor"), ("mh1", "human")):
                c.execute(text(
                    "INSERT INTO messages (id, channel_id, sender_user_id, "
                    "sender_kind, body, aiko_origin, created_at) VALUES "
                    "(:mid,'mc1',NULL,:kind,'hi',1,:ts)"
                ), {"mid": mid, "kind": kind, "ts": "2026-01-01T00:00:00+00:00"})

        # Legacy rows are present and the constraint is NOT yet there — if this
        # fails, the fixture is not reproducing the pre-migration state and
        # everything below would be testing nothing.
        with engine.connect() as c:
            before = dict(c.execute(text(
                "SELECT sender_kind, count(*) FROM messages GROUP BY 1")).all())
        assert before == {"actor": 2, "human": 1}, before

        command.upgrade(migrate._alembic_config(), "0027")

        with engine.connect() as c:
            after = dict(c.execute(text(
                "SELECT sender_kind, count(*) FROM messages GROUP BY 1")).all())
        assert after == {"unknown": 2, "human": 1}, (
            f"0027 left rows unconverted: {after}. Every 'actor' row must become "
            "'unknown', and 'human' rows must not be touched.")

        # The constraint is live on the MIGRATED (not freshly created) table, and
        # the OLD name is now refused — the rename is a closure, not a relabel.
        # Named, for the same reason as its siblings: without it, 'ma9' colliding
        # on the primary key would read as "the old name was rejected".
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO messages (id, channel_id, sender_user_id, "
                    "sender_kind, body, aiko_origin, created_at) VALUES "
                    "('ma9','mc1',NULL,'actor','hi',1,'2026-01-01T00:00:00+00:00')"))
        assert "ck_messages_sender_kind" in str(exc.value) or "CHECK" in str(exc.value), (
            "the old value was refused, but not demonstrably by the sender_kind "
            f"CHECK — the rename may be a relabel. Error: {exc.value}")

        # THE REBUILD MUST NOT COST THE UNIQUE CONSTRAINT. batch_alter_table
        # recreates `messages` (create-new + copy + swap), and this table carries
        # uq_channel_client_msg — the constraint that makes optimistic send
        # idempotent. If a rebuild silently dropped it, every client retry would
        # become a duplicate message and nothing would object. 0027's docstring
        # argues FK-safety for the swap at length and says nothing about this, so
        # the risk was considered in one dimension only. Asserted functionally
        # (the database refuses the duplicate), not by reading the DDL — a
        # constraint present in CREATE TABLE text but not enforced is exactly the
        # check-that-cannot-report-its-own-absence this suite hunts.
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO messages (id, channel_id, sender_user_id, "
                "sender_kind, body, client_msg_id, aiko_origin, created_at) VALUES "
                "('mu1','mc1',NULL,'human','hi','dup',0,:ts)"
            ), {"ts": "2026-01-01T00:00:00+00:00"})
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO messages (id, channel_id, sender_user_id, "
                    "sender_kind, body, client_msg_id, aiko_origin, created_at) "
                    "VALUES ('mu2','mc1',NULL,'human','hi','dup',0,:ts)"
                ), {"ts": "2026-01-01T00:00:00+00:00"})
        assert "client_msg_id" in str(exc.value), (
            "the 0027 rebuild lost uq_channel_client_msg — a resent client_msg_id "
            f"would now duplicate instead of no-op. Error was: {exc.value}")

        # And the downgrade puts the data back, in the reverse order (drop the
        # constraint, THEN rename) — otherwise it would write a value the
        # still-present CHECK rejects.
        command.downgrade(migrate._alembic_config(), "0026")
        with engine.connect() as c:
            back = dict(c.execute(text(
                "SELECT sender_kind, count(*) FROM messages GROUP BY 1")).all())
        assert back == {"actor": 2, "human": 2}, (
            f"the downgrade did not restore the old value: {back}")
    finally:
        engine.dispose()


def test_every_in_check_constraint_reached_the_db_and_matches_its_enum(
        tmp_path, monkeypatch) -> None:
    """GENERIC parity gate: every `_in_check` constraint, migrated DDL vs enum.

    The per-constraint gates above (token_kind, sender_kind) assert more than this
    one does — data-migration order, specific witnesses — and are not replaced by
    it. What they cannot do is speak about a constraint nobody wrote a gate for,
    and that enumerative shape is the same one that let `messages.sender_kind`
    into the schema unobserved and `message_reports.reason` sit unconstrained
    behind a route-only pydantic enum (0028).

    So this iterates `Base.metadata` instead of a list: EVERY `x IN (...)` CHECK
    the models declare must be present in the migrated DDL, on the right table,
    with a clause that matches what `_in_check` renders today. A hand-written
    migration literal that drifts from its enum goes red here without anyone
    remembering to add a gate for it. Companion to
    tests/test_closed_set_guard.py, which catches the column that has no
    constraint at all; this catches the constraint that has drifted or never
    landed.

    alembic's compare_metadata is CHECK-blind on SQLite (ISL-0001), so the parity
    test in this file is otherwise structurally unable to see any of this.
    """
    import re as _re
    import sqlite3
    from alembic import command
    from aiko_gateway import migrate
    from aiko_gateway.domain.models import Base
    from sqlalchemy import CheckConstraint

    _async_url, sync_url = _point_app_at(tmp_path, monkeypatch)
    command.upgrade(migrate._alembic_config(), "head")
    con = sqlite3.connect(sync_url.replace("sqlite:///", ""))
    try:
        ddl_by_table = {
            name: sql for name, sql in con.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'")
            if sql
        }

        def _clause(ddl_text: str, name: str) -> str | None:
            # Balanced extraction: a naive `\(([^)]*)\)` stops at the close paren
            # inside `IN ('a', 'b')` and compares a truncated clause that can
            # never match. Same helper the per-constraint gates use.
            anchor = _re.search(rf"{name}\s+CHECK\s*\(", ddl_text, _re.I)
            if not anchor:
                return None
            depth, i = 1, anchor.end()
            for j in range(i, len(ddl_text)):
                if ddl_text[j] == "(":
                    depth += 1
                elif ddl_text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        return ddl_text[i:j]
            return None

        expected: list[tuple[str, str, str]] = []
        for table in Base.metadata.tables.values():
            for c in table.constraints:
                if isinstance(c, CheckConstraint) and _re.match(
                        r"^\w+ IN \(.+\)$", str(c.sqltext)):
                    expected.append((table.name, c.name, str(c.sqltext)))

        # Guard against the whole gate passing vacuously if the introspection
        # above ever stops finding anything — a green empty loop is the exact
        # failure this file keeps re-learning.
        assert len(expected) >= 14, (
            f"only {len(expected)} IN-checks found in Base.metadata; this gate "
            "has stopped observing the thing it exists to observe")

        problems: list[str] = []
        for table_name, cname, sqltext in sorted(expected):
            ddl = ddl_by_table.get(table_name)
            if ddl is None:
                problems.append(f"{table_name}: table absent from migrated DDL")
                continue
            got = _clause(ddl, cname)
            if got is None:
                problems.append(
                    f"{table_name}.{cname}: declared in models but NOT in the "
                    "migrated DDL — its migration never reached the DB")
                continue
            if _norm_check_clause(got) != _norm_check_clause(sqltext):
                problems.append(
                    f"{table_name}.{cname}: migrated clause {got!r} != "
                    f"_in_check rendering {sqltext!r}")
        assert not problems, "\n".join(problems)
    finally:
        con.close()
