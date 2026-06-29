"""role + join_policy CHECK constraints (#11) — the FUNCTIONAL gate.

Why a separate functional test and not the parity test: alembic's
``compare_metadata`` does NOT detect CHECK constraints on SQLite, so the parity
test in test_migrations.py is blind to whether 0002 actually applied. The only
honest verification is behavioural — attempt an out-of-set write and require the
DB to reject it. These also exercise the 0001->0002 *evolution* path (the first
real ALTER on top of the alembic adoption) and prove batch_alter_table preserved
the existing data + structure.
"""
from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from aiko_gateway import migrate
from aiko_gateway.config import settings

# Minimal valid rows (all NOT NULL columns supplied). created_at/joined_at are
# NOT NULL with python-side defaults the ORM fills — raw SQL must supply them.
_TS = "2026-01-01T00:00:00+00:00"
_INSERT_CHANNEL = (
    "INSERT INTO channels (id, name, kind, aiko_channel, is_private, "
    "join_policy, created_at) VALUES "
    "('c1', 'c', 'standard', 'aiko/c', 0, :jp, '" + _TS + "')"
)
def _insert_user(uid: str) -> str:
    return (
        "INSERT INTO users (id, username, display_name, aiko_username, created_at) "
        f"VALUES ('{uid}', '{uid}', '{uid}', '{uid}', '{_TS}')"
    )


def _insert_membership(uid: str) -> str:
    return (
        "INSERT INTO memberships (channel_id, user_id, role, can_post, joined_at) "
        f"VALUES ('c1', '{uid}', :role, 1, '{_TS}')"
    )


def _fresh_at_head(tmp_path, monkeypatch):
    db = tmp_path / "chk.db"
    monkeypatch.setattr(settings, "db_url", f"sqlite+aiosqlite:///{db}")
    migrate.run()  # fresh -> 0001 -> 0002 (head)
    return create_engine(f"sqlite:///{db}")


def test_role_check_rejects_out_of_set(tmp_path, monkeypatch):
    engine = _fresh_at_head(tmp_path, monkeypatch)
    try:
        with engine.begin() as c:
            c.execute(text(_INSERT_CHANNEL), {"jp": "invite_only"})
            c.execute(text(_insert_user("u1")))
            c.execute(text(_insert_user("u2")))
            c.execute(text(_insert_membership("u1")), {"role": "member"})  # valid
        # DISTINCT user (u2) so a failure can ONLY be the role CHECK, never the
        # composite-PK collision that masked it before (Carnot cage-match, PR#24).
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(_insert_membership("u2")), {"role": "superadmin"})
        assert "ck_memberships_role" in str(exc.value) or "CHECK" in str(exc.value)
    finally:
        engine.dispose()


def test_join_policy_check_rejects_out_of_set(tmp_path, monkeypatch):
    engine = _fresh_at_head(tmp_path, monkeypatch)
    try:
        with engine.begin() as c:
            c.execute(text(_INSERT_CHANNEL), {"jp": "open"})  # valid -> ok
        with pytest.raises(IntegrityError):
            with engine.begin() as c:
                c.execute(text(_INSERT_CHANNEL.replace("'c1'", "'c2'")
                               .replace("'aiko/c'", "'aiko/c2'")),
                          {"jp": "anything_else"})  # out of set -> CHECK rejects
    finally:
        engine.dispose()


def test_upgrade_0001_to_0002_preserves_data_structure_and_applies_check(
        tmp_path, monkeypatch):
    """The evolution path: a DB at 0001 with data, upgraded one step to 0002.
    The batch table-rebuild must keep the rows AND the structure (memberships'
    composite PK + both FKs) AND turn the CHECKs on (Carnot cage-match, PR#24)."""
    db = tmp_path / "evolve.db"
    monkeypatch.setattr(settings, "db_url", f"sqlite+aiosqlite:///{db}")
    cfg = migrate._alembic_config()

    command.upgrade(cfg, "0001")  # baseline only — no CHECK yet
    sync_url = f"sqlite:///{db}"
    engine = create_engine(sync_url)
    try:
        with engine.begin() as c:
            c.execute(text(_INSERT_CHANNEL), {"jp": "invite_only"})
            c.execute(text(_insert_user("u1")))
            c.execute(text(_insert_membership("u1")), {"role": "admin"})
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")  # apply 0002 (batch rebuild of channels + memberships)

    engine = create_engine(sync_url)
    try:
        insp = inspect(engine)
        # Rows survived the rebuild.
        with engine.connect() as c:
            assert c.execute(text(
                "SELECT join_policy FROM channels WHERE id='c1'")).scalar() == "invite_only"
            assert c.execute(text(
                "SELECT role FROM memberships WHERE channel_id='c1' AND user_id='u1'"
            )).scalar() == "admin"
        # Structure survived: memberships composite PK + both FKs.
        assert set(insp.get_pk_constraint("memberships")["constrained_columns"]) == {
            "channel_id", "user_id"}
        fk_targets = {fk["referred_table"] for fk in insp.get_foreign_keys("memberships")}
        assert fk_targets == {"channels", "users"}
        # Composite PK still rejects a duplicate membership.
        with pytest.raises(IntegrityError):
            with engine.begin() as c:
                c.execute(text(_insert_membership("u1")), {"role": "member"})
        # And both CHECKs are now live.
        with pytest.raises(IntegrityError):
            with engine.begin() as c:
                c.execute(text(_insert_user("u3")))
                c.execute(text(_insert_membership("u3")), {"role": "bogus"})
        with pytest.raises(IntegrityError):
            with engine.begin() as c:
                c.execute(text(_INSERT_CHANNEL.replace("'c1'", "'c3'")
                               .replace("'aiko/c'", "'aiko/c3'")), {"jp": "bogus"})
    finally:
        engine.dispose()
