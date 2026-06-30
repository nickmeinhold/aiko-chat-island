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
# Point-in-time copy of models.DEFAULT_COMMUNITY_ID / 0009's seeded community.
_DEFAULT_COMMUNITY_ID = "0" * 26
# Channel insert AT HEAD: the ck_channels_community_required CHECK (#32) is live, so
# a non-DM channel MUST carry a community_id. Supplying the (seeded) default means a
# failure here can ONLY be the constraint under test — never the community CHECK
# masking it (the test-green-for-the-wrong-reason trap, Carnot PR#24).
_INSERT_CHANNEL = (
    "INSERT INTO channels (id, name, kind, aiko_channel, is_private, "
    "join_policy, community_id, created_at) VALUES "
    "('c1', 'c', 'standard', 'aiko/c', 0, :jp, '" + _DEFAULT_COMMUNITY_ID
    + "', '" + _TS + "')"
)
# Channel insert AT REVISION 0001 ONLY (before 0009 added community_id). Used by the
# 0001->0002 evolution test, which inserts the row pre-community then upgrades; 0009's
# backfill fills community_id before its CHECK is applied.
_INSERT_CHANNEL_0001 = (
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


_INSERT_CHALLENGE = (
    "INSERT INTO passkey_challenges (state, operation, expires_at, consumed, "
    "created_at) VALUES (:state, :op, '" + _TS + "', 0, '" + _TS + "')"
)


def test_passkey_operation_check_rejects_out_of_set(tmp_path, monkeypatch):
    """passkey_challenges.operation is a closed set (register|authenticate) enforced
    by a DB CHECK (#1471) — the same defense-beyond-the-API pattern as role/
    join_policy. A DISTINCT `state` PK per insert so a failure can ONLY be the
    operation CHECK, never a PK collision (the test-green-for-the-wrong-reason
    trap, Carnot PR#24)."""
    engine = _fresh_at_head(tmp_path, monkeypatch)
    try:
        with engine.begin() as c:
            c.execute(text(_INSERT_CHALLENGE), {"state": "s1", "op": "register"})  # valid
            c.execute(text(_INSERT_CHALLENGE),
                      {"state": "s2", "op": "authenticate"})  # valid
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(_INSERT_CHALLENGE), {"state": "s3", "op": "bogus"})
        # Named-constraint assertion: prove it was the operation CHECK that fired,
        # not some incidental violation.
        assert "ck_passkey_challenges_operation" in str(exc.value)
    finally:
        engine.dispose()


def test_community_required_check_rejects_non_dm_null_community(tmp_path, monkeypatch):
    """ck_channels_community_required (#32): a NON-DM channel may not have a NULL
    community_id. Distinct id/aiko_channel so the failure can only be this CHECK,
    not a PK/unique collision. Named-constraint assertion proves WHICH fired."""
    engine = _fresh_at_head(tmp_path, monkeypatch)
    try:
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO channels (id, name, kind, aiko_channel, is_private, "
                    "join_policy, created_at) VALUES "
                    "('cx', 'cx', 'standard', 'aiko/cx', 0, 'open', '" + _TS + "')"))
        assert "ck_channels_community_required" in str(exc.value)
    finally:
        engine.dispose()


def test_community_required_check_allows_dm_null_community(tmp_path, monkeypatch):
    """The other half of the same CHECK: a DM channel (kind='dm') IS allowed to be
    community-less (community_id NULL) — DMs live outside the community hierarchy.
    This is the near-term-DM accommodation the partial CHECK was chosen for; if it
    regressed to a blanket NOT NULL this insert would fail."""
    engine = _fresh_at_head(tmp_path, monkeypatch)
    try:
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO channels (id, name, kind, aiko_channel, is_private, "
                "join_policy, created_at) VALUES "
                "('dm1', 'dm', 'dm', 'aiko/dm1', 0, 'invite_only', '" + _TS + "')"))
            kind = c.execute(text(
                "SELECT kind FROM channels WHERE id='dm1'")).scalar()
        assert kind == "dm"
    finally:
        engine.dispose()


def test_0009_rebuild_preserves_join_policy_check(tmp_path, monkeypatch):
    """0009 rebuilds `channels` (batch) to add the community FK + CHECK. The parity
    gate's compare_metadata is CHECK-BLIND on SQLite, so it cannot prove the
    pre-existing ck_channels_join_policy survived the rebuild — assert directly that
    BOTH CHECKs are present in the migrated channels DDL (the Carnot PR#28 pattern:
    a structural assertion where compare_metadata is blind)."""
    engine = _fresh_at_head(tmp_path, monkeypatch)
    try:
        with engine.connect() as c:
            ddl = c.execute(text(
                "SELECT sql FROM sqlite_master WHERE name='channels'")).scalar()
    finally:
        engine.dispose()
    assert "ck_channels_join_policy" in ddl, (
        "0009's batch rebuild of channels DROPPED the pre-existing join_policy "
        "CHECK — alembic batch reflection lost it; re-declare it in the rebuild.")
    assert "ck_channels_community_required" in ddl


def test_0009_rebuild_preserves_aiko_channel_unique(tmp_path, monkeypatch):
    """0009's batch rebuild of `channels` must preserve the (unnamed) aiko_channel
    UNIQUE from 0001. compare_metadata's unique-reflection on SQLite is exactly the
    kind of thing that can silently leak through a rebuild, so prove it directly
    (verify by RUNNING, not by trusting the parity gate): a duplicate aiko_channel
    insert at head must be rejected (Carnot cage-match, PR#47)."""
    engine = _fresh_at_head(tmp_path, monkeypatch)
    try:
        with engine.begin() as c:
            c.execute(text(_INSERT_CHANNEL), {"jp": "open"})  # 'aiko/c'
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                # Distinct PK, SAME aiko_channel — only the UNIQUE can fire.
                c.execute(text(_INSERT_CHANNEL.replace("'c1'", "'c2'")), {"jp": "open"})
        assert "UNIQUE" in str(exc.value) or "unique" in str(exc.value)
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
            c.execute(text(_INSERT_CHANNEL_0001), {"jp": "invite_only"})
            c.execute(text(_insert_user("u1")))
            c.execute(text(_insert_membership("u1")), {"role": "admin"})
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")  # apply 0002..0009 (batch rebuilds of channels + memberships)

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
