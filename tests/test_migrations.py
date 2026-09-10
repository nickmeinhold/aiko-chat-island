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


def test_mixed_known_and_unknown_heads_skips(tmp_path, monkeypatch) -> None:
    """A branched/merged version table carrying BOTH a known head and an unknown
    revision must take the skip path (Wu cage-match, PR#116). This makes
    "freeze on ANY unknown head" a deliberate decision, not accidental behaviour:
    `command.upgrade(head)` would try to resolve the unknown row and die anyway, so
    skipping is the safe posture — but assert it so a future topology change to
    multi-head can't silently flip it. (The repo's single-head invariant means this
    shouldn't arise in practice; the test pins the semantics regardless.)
    """
    from sqlalchemy import text

    _, sync_url = _point_app_at(tmp_path, monkeypatch)
    migrate.run()  # managed DB at head — alembic_version has the one real head row

    # Add a SECOND version row naming a revision this image doesn't know, so the
    # version table now carries {known_head, unknown}.
    seed = create_engine(sync_url)
    try:
        with seed.begin() as conn:
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:r)"),
                {"r": "9999_from_a_newer_image"},
            )
            rows_before = {r[0] for r in conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version").fetchall()}
    finally:
        seed.dispose()

    migrate.run()  # must NOT raise, must NOT mutate the version table

    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            rows_after = {r[0] for r in conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version").fetchall()}
    finally:
        engine.dispose()
    assert "9999_from_a_newer_image" in rows_before
    assert rows_after == rows_before, (
        "mixed known/unknown heads must skip untouched, got "
        f"{rows_after!r} (was {rows_before!r})")


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
# MIGRATION-vs-ENUM PARITY (Carnot, cage-match PR#170 — a confirmed finding).
#
# `models.py` renders device_tokens' CHECK from the enum via
# `_in_check("token_kind", TokenKind)`; revision 0025 carried a HAND-WRITTEN
# literal saying the same thing. Two sources of truth for one closed set, in a
# change whose own docstring argues the enum is the single source — and nothing
# compared them. Add a third TokenKind member and the model's CHECK updates
# itself while the migration's literal does not, so an island bootstrapped from
# migrations ends up with a DIFFERENT constraint from the one the model declares.
#
# The fix is NOT to import the live enum into the revision: a migration is a
# historical artifact and must keep producing the DDL it always produced. This
# test is the honest form of the guarantee — it fails loudly the day the enum
# grows, which is the day a new revision is owed.
# ---------------------------------------------------------------------------


def test_the_0025_check_still_matches_what_the_enum_renders_today():
    """RED-PROVEN by adding a member to TokenKind: this goes red, the model's
    CHECK silently absorbs it, and the migration's does not."""
    import importlib.util
    from pathlib import Path
    from aiko_gateway.domain.models import TokenKind, _in_check

    rev = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0025_device_token_kind.py"
    spec = importlib.util.spec_from_file_location("rev_0025", rev)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _norm(sql: str) -> str:
        return "".join(str(sql).lower().split()).replace('"', "'")

    assert _norm(mod._KIND_CHECK) == _norm(_in_check("token_kind", TokenKind)), (
        f"revision 0025's CHECK ({mod._KIND_CHECK!r}) has drifted from what "
        f"_in_check renders from TokenKind ({_in_check('token_kind', TokenKind)!r}). "
        "The enum grew without a new migration: a fresh island would get a "
        "different constraint from the one models.py declares. Write the new "
        "revision rather than editing 0025 — it is history, not current state."
    )


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
    def _norm2(x: str) -> str:
        return "".join(str(x).lower().split()).replace('"', "'")

    assert _norm2(clause) == _norm2(_in_check("token_kind", TokenKind)), (
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
    con = sqlite3.connect(path)
    con.execute("INSERT INTO device_tokens (id, user_id, platform, token, "
                "token_kind, apns_environment, created_at, updated_at) VALUES "
                "('a1','u1','apns','aaa','alert','production',"
                "'2026-01-01 00:00:00','2026-01-01 00:00:00')")
    con.commit(); con.close()
    command.downgrade(migrate._alembic_config(), "0024")
    command.upgrade(migrate._alembic_config(), "head")

    # Now one VoIP row: the downgrade must refuse and SAY WHAT IT WOULD DESTROY.
    con = sqlite3.connect(path)
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
