"""Tests for the additive-only migration lint (task #11, A1).

Golden fixtures for every taxonomy row the v2 re-Temper hardened, plus a sweep over
THIS repo's REAL migrations (Tesla: fixtures from real migrations, not just synthetic)
to prove the lint runs and classifies shipped code sanely.

The lint lives in ``tools/`` (a CI/dev tool, not shipped in the wheel), so add it to
the path. It is pure stdlib (``ast``), honouring the CI test-isolation invariant.
"""
from __future__ import annotations

import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import migration_lint as ml  # noqa: E402


def _wrap(upgrade_body: str, extra: str = "") -> str:
    """A migration module source with the given upgrade() body."""
    return f"import sqlalchemy as sa\nfrom alembic import op\n{extra}\n\ndef upgrade():\n{upgrade_body}\n\ndef downgrade():\n    pass\n"


def _sev(findings, method):
    return [f.severity for f in findings if f.op == method]


# ---- safe / additive ----------------------------------------------------------

def test_create_table_only_is_clean():
    src = _wrap('    op.create_table("t", sa.Column("id", sa.Integer(), primary_key=True))')
    assert ml.analyze_source(src) == []


def test_add_nullable_column_is_safe():
    src = _wrap('    op.add_column("users", sa.Column("bio", sa.String(), nullable=True))')
    assert ml.analyze_source(src) == []


def test_plain_index_is_safe():
    src = _wrap('    op.create_index("ix_users_bio", "users", ["bio"])')
    assert ml.analyze_source(src) == []


# ---- contracting (hard FAIL) --------------------------------------------------

def test_drop_column_is_contracting():
    src = _wrap('    op.drop_column("users", "bio")')
    assert ml.CONTRACTING in _sev(ml.analyze_source(src), "drop_column")


def test_drop_table_is_contracting():
    assert ml.CONTRACTING in _sev(ml.analyze_source(_wrap('    op.drop_table("users")')), "drop_table")


def test_add_not_null_without_default_is_contracting():
    src = _wrap('    op.add_column("users", sa.Column("role", sa.String(), nullable=False))')
    assert ml.CONTRACTING in _sev(ml.analyze_source(src), "add_column")


def test_add_unique_column_is_contracting():
    src = _wrap('    op.add_column("users", sa.Column("handle", sa.String(), unique=True))')
    assert ml.CONTRACTING in _sev(ml.analyze_source(src), "add_column")


def test_rename_via_alter_column_kwarg_is_contracting():
    # Wu: a rename hides as an ordinary alter_column kwarg.
    src = _wrap('    op.alter_column("messages", "origin", new_column_name="provenance")')
    assert ml.CONTRACTING in _sev(ml.analyze_source(src), "alter_column")


def test_alter_column_type_change_is_contracting():
    src = _wrap('    op.alter_column("users", "age", type_=sa.SmallInteger())')
    assert ml.CONTRACTING in _sev(ml.analyze_source(src), "alter_column")


def test_alter_column_to_not_null_without_default_is_contracting():
    src = _wrap('    op.alter_column("users", "role", nullable=False)')
    assert ml.CONTRACTING in _sev(ml.analyze_source(src), "alter_column")


def test_drop_constraint_is_contracting():
    src = _wrap('    op.drop_constraint("uq_users_handle", "users", type_="unique")')
    assert ml.CONTRACTING in _sev(ml.analyze_source(src), "drop_constraint")


def test_create_foreign_key_is_contracting():
    src = _wrap('    op.create_foreign_key("fk_m_u", "messages", "users", ["user_id"], ["id"])')
    assert ml.CONTRACTING in _sev(ml.analyze_source(src), "create_foreign_key")


def test_create_check_constraint_is_contracting():
    src = _wrap('    op.create_check_constraint("ck_x", "users", "age > 0")')
    assert ml.CONTRACTING in _sev(ml.analyze_source(src), "create_check_constraint")


def test_unique_index_is_contracting():
    src = _wrap('    op.create_index("uq_users_handle", "users", ["handle"], unique=True)')
    assert ml.CONTRACTING in _sev(ml.analyze_source(src), "create_index")


# ---- review (needs human sign-off) --------------------------------------------

def test_add_not_null_with_server_default_is_review():
    src = _wrap('    op.add_column("users", sa.Column("gen", sa.Integer(), nullable=False, server_default="0"))')
    assert _sev(ml.analyze_source(src), "add_column") == [ml.REVIEW]


def test_add_nullable_with_server_default_is_review():
    # Wu/Tesla: the default applies when the column is OMITTED regardless of nullability,
    # so a nullable server_default is the SAME semantic-poisoning risk — must be REVIEW,
    # not a silent pass.
    src = _wrap('    op.add_column("users", sa.Column("role", sa.String(), nullable=True, server_default="user"))')
    assert _sev(ml.analyze_source(src), "add_column") == [ml.REVIEW]


def test_raw_execute_is_review():
    src = _wrap('    op.execute("UPDATE users SET status = \'active\' WHERE status = 1")')
    assert _sev(ml.analyze_source(src), "execute") == [ml.REVIEW]


def test_batch_recreate_via_args_is_review():
    src = _wrap('    with op.batch_alter_table("users", copy_from=None) as b:\n        b.alter_column("x", nullable=True)')
    assert ml.REVIEW in _sev(ml.analyze_source(src), "batch_alter_table")


# ---- batch context ------------------------------------------------------------

def test_batch_drop_column_is_contracting_with_batch_table():
    src = _wrap('    with op.batch_alter_table("channels") as b:\n        b.drop_column("community_id")')
    findings = ml.analyze_source(src)
    drops = [f for f in findings if f.op == "drop_column"]
    assert drops and drops[0].severity == ml.CONTRACTING
    assert drops[0].table == "channels"


def test_batch_add_nullable_column_is_safe():
    src = _wrap('    with op.batch_alter_table("channels") as b:\n        b.add_column(sa.Column("note", sa.String(), nullable=True))')
    assert ml.analyze_source(src) == []


# ---- new-table carve-out (Wu) -------------------------------------------------

def test_new_table_carveout_exempts_contracting_ops_on_that_table():
    # Every "contracting" op here targets a table created in the SAME migration —
    # no deployed code ever wrote it, so all are safe.
    body = (
        '    op.create_table("widgets", sa.Column("id", sa.Integer(), primary_key=True))\n'
        '    op.add_column("widgets", sa.Column("kind", sa.String(), nullable=False))\n'
        '    op.create_index("uq_widgets_kind", "widgets", ["kind"], unique=True)\n'
        '    op.create_check_constraint("ck_widgets_kind", "widgets", "kind != \'\'")'
    )
    assert ml.analyze_source(_wrap(body)) == []


def test_carveout_does_not_mask_ops_on_other_tables():
    body = (
        '    op.create_table("widgets", sa.Column("id", sa.Integer(), primary_key=True))\n'
        '    op.drop_column("users", "bio")'
    )
    findings = ml.analyze_source(_wrap(body))
    assert any(f.op == "drop_column" and f.table == "users" and f.severity == ml.CONTRACTING
               for f in findings)


# ---- escape hatch + downgrade scope -------------------------------------------

def test_annotation_downgrades_contracting_to_review():
    src = _wrap('    op.drop_column("users", "bio")',
                extra='# expand-contract: contract-phase\n# stop-use-shipped-in: v0.3.0')
    findings = ml.analyze_source(src)
    assert [f.severity for f in findings] == [ml.REVIEW]
    assert "contract-phase annotated" in findings[0].reason


def test_downgrade_body_is_ignored():
    # Only upgrade() is analysed — prod never runs downgrade().
    src = (
        "import sqlalchemy as sa\nfrom alembic import op\n\n"
        "def upgrade():\n    op.add_column('users', sa.Column('bio', sa.String(), nullable=True))\n\n"
        "def downgrade():\n    op.drop_column('users', 'bio')\n"
    )
    assert ml.analyze_source(src) == []


# ---- real repo migrations (golden fixtures from THIS repo) ---------------------

def _migration_files():
    here = os.path.dirname(__file__)
    return sorted(glob.glob(os.path.join(here, "..", "alembic", "versions", "*.py")))


def test_lint_runs_on_every_real_migration_without_crashing():
    files = _migration_files()
    assert files, "no migrations found — path wrong?"
    for path in files:
        with open(path, encoding="utf-8") as fh:
            ml.analyze_source(fh.read())  # must not raise on any shipped migration


def test_baseline_migration_is_clean():
    # 0001 is all create_table (+ indexes on those new tables) → additive.
    path = [p for p in _migration_files() if os.path.basename(p).startswith("0001")][0]
    with open(path, encoding="utf-8") as fh:
        assert ml.analyze_source(fh.read()) == []


def test_token_generation_migration_is_review_not_fail():
    # 0015 adds users.token_generation NOT NULL server_default="0" → REVIEW (semantic),
    # never a hard contracting FAIL.
    path = [p for p in _migration_files() if os.path.basename(p).startswith("0015")][0]
    with open(path, encoding="utf-8") as fh:
        findings = ml.analyze_source(fh.read())
    assert findings and all(f.severity == ml.REVIEW for f in findings)


def test_check_constraint_migration_is_flagged_contracting():
    # 0002 adds CHECK constraints to EXISTING tables (channels/memberships) — a genuine
    # backward-incompatible write tighten. Proves the lint has teeth on real shipped code
    # (these pre-date the discipline; pre-prod so it didn't bite).
    path = [p for p in _migration_files() if os.path.basename(p).startswith("0002")][0]
    with open(path, encoding="utf-8") as fh:
        findings = ml.analyze_source(fh.read())
    assert any(f.severity == ml.CONTRACTING for f in findings)
