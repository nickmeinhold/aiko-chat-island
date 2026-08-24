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
        # composite-PK collision that masked it before.
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
    regressed to a blanket NOT NULL this insert would fail. is_private=1 because a DM
    must be private (ck_channels_dm_private, #2633 cage-match PR#124)."""
    engine = _fresh_at_head(tmp_path, monkeypatch)
    try:
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO channels (id, name, kind, aiko_channel, is_private, "
                "join_policy, created_at) VALUES "
                "('dm1', 'dm', 'dm', 'dm:1', 1, 'invite_only', '" + _TS + "')"))
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
    insert at head must be rejected."""
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
    composite PK + both FKs) AND turn the CHECKs on."""
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


def test_users_kind_check_rejects_out_of_set(tmp_path, monkeypatch):
    """users.kind is a closed set at the DB (#3096, migration 0022).

    The kind decides how a sender is RENDERED (human vs agent), so leaving it an
    unenforced open string is the same posture the #2633 cage-match rejected for
    channels.kind. A direct SQL writer must not be able to invent a third kind —
    a client that switch-dispatches on it would fall through to whatever its
    default branch is, which for a badge means guessing.
    """
    engine = _fresh_at_head(tmp_path, monkeypatch)
    try:
        with engine.begin() as c:
            c.execute(text(_insert_user("u1")))  # default 'human' -> ok
            c.execute(text(_insert_user("u2").replace(
                "(id, username, display_name, aiko_username, created_at)",
                "(id, username, display_name, aiko_username, created_at, kind)"
            ).replace(f"'{_TS}')", f"'{_TS}', 'agent')")))  # explicit agent -> ok
        # DISTINCT user id so a failure can only be the kind CHECK, never a PK
        # collision masking it (the same trap Carnot found in PR#24).
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(_insert_user("u3").replace(
                    "(id, username, display_name, aiko_username, created_at)",
                    "(id, username, display_name, aiko_username, created_at, kind)"
                ).replace(f"'{_TS}')", f"'{_TS}', 'daemon')")))
        assert "ck_users_kind" in str(exc.value) or "CHECK" in str(exc.value)
    finally:
        engine.dispose()


def test_users_kind_backfills_human(tmp_path, monkeypatch):
    """A row inserted WITHOUT kind gets 'human' from the server_default (#3096).

    Two things at once: the migration's backfill direction is the honest one (no
    agent account can predate 0022), and the default is retained after backfill so
    a writer that omits the column gets 'human' rather than an error — the safe
    direction for a column that gates rendering.
    """
    engine = _fresh_at_head(tmp_path, monkeypatch)
    try:
        with engine.begin() as c:
            c.execute(text(_insert_user("u1")))
            got = c.execute(text("SELECT kind FROM users WHERE id='u1'")).scalar_one()
        assert got == "human"
    finally:
        engine.dispose()


# A FULLY-POPULATED user, as a live island actually holds one. The evolution tests
# below seed with this rather than the skeleton `_insert_user` on purpose: 0022
# rebuilds `users` by copy, and a copy that keeps a column but writes NULL into it
# is invisible to a fixture that never wrote the column in the first place. Live
# rows carry password hashes, emails, ban timestamps and handle clocks; test rows
# that are husks cannot detect losing any of them.
_FAT_USER_COLS = ("id, username, display_name, password_hash, aiko_username, email, "
                  "created_at, banned_at, token_generation, handle_changed_at")
# NON-ZERO on purpose. token_generation is the session-revocation counter (0015):
# every token embeds the gen it was minted at and is honoured only while it still
# equals this column, so bumping it invalidates every outstanding token for that
# user (recovery finalize re-keys this way). A rebuild that RESET it to 0 would
# silently UN-REVOKE every previously revoked session — and a fixture that seeds
# the default 0 cannot tell "preserved" from "reset". Value-preservation and
# default-preservation are two theorems; this seeds the first, and the
# post-migration insert below proves the second. (Carnot, cage-match PR#142.)
_FAT_TOKEN_GENERATION = 7
_FAT_USER_VALUES = {
    "id": "fat1",
    "username": "fatuser",
    "display_name": "Fat User",
    "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$fakehashfortests",
    "aiko_username": "fatuser@aiko",
    "email": "fat@example.test",
    "created_at": _TS,
    "banned_at": "2026-02-02T00:00:00+00:00",
    "handle_changed_at": "2026-03-03T00:00:00+00:00",
    "token_generation": _FAT_TOKEN_GENERATION,
}


def _insert_fat_user() -> str:
    """Every column explicitly set, including a NON-DEFAULT token_generation."""
    v = _FAT_USER_VALUES
    return (
        f"INSERT INTO users ({_FAT_USER_COLS}) VALUES ("
        f"'{v['id']}', '{v['username']}', '{v['display_name']}', '{v['password_hash']}', "
        f"'{v['aiko_username']}', '{v['email']}', '{v['created_at']}', '{v['banned_at']}', "
        f"{v['token_generation']}, '{v['handle_changed_at']}')"
    )


def _assert_husk_still_empty(conn, uid: str) -> None:
    """The THIRD theorem: a cell that was NULL before the rebuild is still NULL.

    _assert_fat_user_intact can only observe cells that were written non-NULL, so a
    copy that materialises NULLs is invisible to it — and that failure is not
    cosmetic: COALESCE-ing (or CURRENT_TIMESTAMP-ing) `banned_at` mass-BANS every
    ordinary account on both live islands while the seeded banned user still
    round-trips byte-identical and the suite stays green.
    """
    row = conn.execute(text(
        "SELECT password_hash, email, banned_at, handle_changed_at "
        f"FROM users WHERE id='{uid}'")).one()
    assert row[0] is None, "password_hash materialised by the rebuild"
    assert row[1] is None, "email materialised by the rebuild"
    assert row[2] is None, (
        "banned_at materialised by the rebuild — every ordinary account is now banned")
    assert row[3] is None, "handle_changed_at materialised by the rebuild"


def _assert_fat_user_intact(conn) -> None:
    """Every seeded value survives EXACTLY.

    Exact equality, not a date prefix: SQLite hands these DATETIME columns back as
    the text they were written with, so a prefix check would pass a truncation, a
    dropped offset, or a coercion to `2026-02-02 12:00:00`.

    DEFAULT preservation is a separate theorem and is NOT checked here — it lives on
    the post-rebuild inserts that omit the defaulted columns.
    """
    row = conn.execute(text(
        "SELECT username, display_name, password_hash, aiko_username, email, "
        "created_at, banned_at, token_generation, handle_changed_at "
        "FROM users WHERE id='fat1'"
    )).one()
    v = _FAT_USER_VALUES
    assert row[0] == v["username"]
    assert row[1] == v["display_name"]
    assert row[2] == v["password_hash"], "password_hash lost or NULLed by the rebuild"
    assert row[3] == v["aiko_username"]
    assert row[4] == v["email"], "email lost or NULLed by the rebuild"
    assert str(row[5]) == v["created_at"], (
        "created_at was rewritten by the rebuild — every account's age is wrong")
    assert str(row[6]) == v["banned_at"], (
        "banned_at lost or altered — a ban would silently lift")
    assert row[7] == _FAT_TOKEN_GENERATION, (
        "token_generation was RESET by the rebuild — every session this user had "
        "revoked would silently start validating again")
    assert str(row[8]) == v["handle_changed_at"], "handle_changed_at lost or altered"


def test_upgrade_0021_to_0022_preserves_populated_users_table(tmp_path, monkeypatch):
    """The EVOLUTION path 0021 -> 0022 on a table that already has FAT rows.

    The two tests above build a FRESH database straight to head, which proves the
    end state but never exercises the path either live island will actually take:
    a populated ``users`` table rebuilt in place. 0022 is a parent-table rebuild
    (SQLite cannot ALTER TABLE ... ADD CONSTRAINT), so the rebuild has to carry
    every pre-existing structure AND every existing value across by reflection —
    and SQLite reflection is the weak instrument here.

    EACH UNIQUE IS PROBED IN ISOLATION, AND EACH ASSERTION PINS THE COLUMN NAME.
    An earlier version of this test collided on both unique columns at once and
    only asserted ``IntegrityError``, so it stayed green with UNIQUE(username)
    removed — it was firing on aiko_username the whole time. A test that cannot
    produce the failure cannot clear it, and an untyped exception assertion cannot
    tell which failure it produced. (Found by Tesla, cage-match PR#136 round 2.)
    """
    db = tmp_path / "evolve_0022.db"
    monkeypatch.setattr(settings, "db_url", f"sqlite+aiosqlite:///{db}")
    cfg = migrate._alembic_config()

    command.upgrade(cfg, "0021")  # the revision both production islands sit at
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as c:
            c.execute(text(_INSERT_CHANNEL), {"jp": "invite_only"})
            c.execute(text(_insert_fat_user()))
            c.execute(text(_insert_user("u2")))
            c.execute(text(_insert_membership("fat1")), {"role": "admin"})
    finally:
        engine.dispose()

    command.upgrade(cfg, "0022")  # THE REBUILD

    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as c:
            assert c.execute(text("SELECT count(*) FROM users")).scalar_one() == 2
            # No agent account can predate 0022, so every existing row is a human.
            assert c.execute(text(
                "SELECT kind FROM users ORDER BY id")).scalars().all() == ["human", "human"]
            # Every column value survived the copy — not just the column itself.
            _assert_fat_user_intact(c)
            # Theorem 3 on the husk seeded beside the fat row.
            _assert_husk_still_empty(c, "u2")
            # The child row still resolves across the parent swap (ADR-0002 FK-off).
            assert c.execute(text(
                "SELECT count(*) FROM memberships m JOIN users u "
                "ON u.id = m.user_id")).scalar_one() == 1

        # BOTH SERVER DEFAULTS SURVIVED ONTO THE MIGRATED TABLE — a separate
        # theorem from "existing values were preserved" above. A hand-written 0022
        # could backfill every existing row and enforce the CHECK while dropping
        # the defaults on the rebuilt table; nothing would go red until an older
        # writer omitted a column in production and hit NOT NULL. The fresh-DB
        # test cannot cover this: it never takes the rebuild path.
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO users (id, username, display_name, aiko_username, "
                f"created_at) VALUES ('d1', 'd1', 'd1', 'd1@aiko', '{_TS}')"))
            defaults = c.execute(text(
                "SELECT kind, token_generation FROM users WHERE id='d1'")).one()
        assert defaults[0] == "human", (
            "the MIGRATED table lost kind's server default — an omitting writer "
            "now fails NOT NULL instead of getting the honest 'human'")
        assert defaults[1] == 0, (
            "the MIGRATED table lost token_generation's PRE-EXISTING (0015) default")

        # UNIQUE(username) IN ISOLATION: aiko_username is distinct, so only the
        # username constraint can reject this. Pin the column so a different
        # IntegrityError (NOT NULL, PK, the other unique) cannot pass as this one.
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO users (id, username, display_name, aiko_username, "
                    f"created_at) VALUES ('x1', '{_FAT_USER_VALUES['username']}', "
                    f"'x1', 'x1@aiko', '{_TS}')"))
        assert "users.username" in str(exc.value)

        # UNIQUE(aiko_username) IN ISOLATION: username is distinct this time.
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO users (id, username, display_name, aiko_username, "
                    f"created_at) VALUES ('x2', 'x2', 'x2', "
                    f"'{_FAT_USER_VALUES['aiko_username']}', '{_TS}')"))
        assert "users.aiko_username" in str(exc.value)

        # And the NEW constraint is live on the MIGRATED (not freshly created) table.
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO users (id, username, display_name, aiko_username, "
                    f"created_at, kind) VALUES ('x3', 'x3', 'x3', 'x3@aiko', '{_TS}', "
                    "'daemon')"))
        assert "ck_users_kind" in str(exc.value)

        # kind is CHECK-closed but the CHECK CANNOT close it against NULL: SQL
        # three-valued logic makes `NULL IN ('human','agent')` UNKNOWN, and UNKNOWN
        # is not FALSE, so the row is admitted. Verified directly: with NOT NULL
        # dropped, 'daemon' is rejected by the CHECK and NULL sails through. Only
        # NOT NULL closes the set — and it is THIS column that 0022 exists to add,
        # so it needs its own probe rather than inheriting display_name's.
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO users (id, username, display_name, aiko_username, "
                    f"created_at, kind) VALUES ('k1', 'k1', 'k1', 'k1@aiko', '{_TS}', "
                    "NULL)"))
        assert "NOT NULL" in str(exc.value) and "users.kind" in str(exc.value)

        # NOT NULL survived the rebuild too. Structural weakening is as silent as
        # value loss: a rebuilt parent that dropped NOT NULL on a required column
        # accepts junk rows forever and nothing above would notice. (Carnot, PR#142.)
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO users (id, username, aiko_username, created_at) "
                    f"VALUES ('n1', 'n1', 'n1@aiko', '{_TS}')"))  # display_name omitted
        assert "NOT NULL" in str(exc.value) and "users.display_name" in str(exc.value)

        # PRIMARY KEY survived. UNIQUE(username) does NOT cover this — a duplicate
        # id under a fresh handle slips past every other assertion here and gives
        # two accounts one identity.
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO users (id, username, display_name, aiko_username, "
                    f"created_at) VALUES ('fat1', 'pk1', 'pk1', 'pk1@aiko', '{_TS}')"))
        assert "users.id" in str(exc.value)
    finally:
        engine.dispose()


def test_downgrade_0022_to_0021_round_trips_a_fat_row(tmp_path, monkeypatch):
    """0021 -> 0022 -> 0021 keeps every value, and drops the column cleanly.

    The downgrade is a SECOND parent rebuild, and it drop_constraint's a CHECK on
    the dialect this file already documents as CHECK-blind. Production only ever
    boots forward, so this is not an island-killer — it is the path someone takes
    at 3am when something has already gone wrong, which is the worst moment to
    discover it was never executed once. (Tesla's concern, cage-match PR#136.)
    """
    db = tmp_path / "roundtrip_0022.db"
    monkeypatch.setattr(settings, "db_url", f"sqlite+aiosqlite:///{db}")
    cfg = migrate._alembic_config()

    command.upgrade(cfg, "0021")
    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as c:
            c.execute(text(_INSERT_CHANNEL), {"jp": "invite_only"})
            c.execute(text(_insert_fat_user()))
            c.execute(text(_insert_user("u2")))  # a husk, so theorem 3 has something to lose
            c.execute(text(_insert_membership("fat1")), {"role": "admin"})
    finally:
        engine.dispose()

    command.upgrade(cfg, "0022")
    command.downgrade(cfg, "0021")  # the untested direction

    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as c:
            _assert_fat_user_intact(c)
            _assert_husk_still_empty(c, "u2")

        # Column absence read STRUCTURALLY, not by substring. SQLite renders DDL in
        # more than one way (quoted identifier, a different type spelling), so
        # `"kind VARCHAR" not in ddl` can pass over a leftover `"kind" TEXT`.
        cols = {c["name"] for c in inspect(engine).get_columns("users")}
        assert "kind" not in cols, f"downgrade left the column behind: {sorted(cols)}"

        # And the uniques survive the SECOND rebuild — asserted BEHAVIOURALLY, the
        # same way the upgrade path does. DDL text is not the invariant anyone
        # relies on; rejecting a duplicate is.
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO users (id, username, display_name, aiko_username, "
                f"created_at) VALUES ('dg1', 'dg1', 'dg1', 'dg1@aiko', '{_TS}')"))
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO users (id, username, display_name, aiko_username, "
                    f"created_at) VALUES ('dg2', 'dg1', 'dg2', 'dg2@aiko', '{_TS}')"))
        assert "users.username" in str(exc.value)
        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO users (id, username, display_name, aiko_username, "
                    f"created_at) VALUES ('dg3', 'dg3', 'dg3', 'dg1@aiko', '{_TS}')"))
        assert "users.aiko_username" in str(exc.value)

        # DEFAULT PRESERVATION ON THE REVERSE LEG. The downgrade is a second full
        # rebuild and owes every invariant the forward one does:
        #
        #   invariant                 upgrade   downgrade
        #   existing values           yes       yes  (_assert_fat_user_intact)
        #   server defaults           yes       THIS
        #   NOT NULL                  yes       BELOW
        #   UNIQUEs (behavioural)     yes       above
        #   CHECK present / absent    yes       column-absence above
        #
        # A downgrade that copies rows correctly while dropping DEFAULT 0 leaves
        # rollback-era writers omitting token_generation either failing NOT NULL
        # or storing the wrong revocation counter — on the exact path someone
        # takes when something has already gone wrong.
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO users (id, username, display_name, aiko_username, "
                f"created_at) VALUES ('dgd', 'dgd', 'dgd', 'dgd@aiko', '{_TS}')"))
            assert c.execute(text(
                "SELECT token_generation FROM users WHERE id='dgd'")).scalar_one() == 0, (
                "the downgraded table lost token_generation's 0015 default")

        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO users (id, username, aiko_username, created_at) "
                    f"VALUES ('dgn', 'dgn', 'dgn@aiko', '{_TS}')"))  # display_name omitted
        assert "NOT NULL" in str(exc.value) and "users.display_name" in str(exc.value)

        with pytest.raises(IntegrityError) as exc:
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO users (id, username, display_name, aiko_username, "
                    f"created_at) VALUES ('fat1', 'pk2', 'pk2', 'pk2@aiko', '{_TS}')"))
        assert "users.id" in str(exc.value)

        # The child still resolves after the SECOND rebuild. The upgrade leg
        # asserted this; the downgrade leg did not, and it drops+recreates the
        # same parent under the same FK-off premise (ADR-0002).
        with engine.begin() as c:
            assert c.execute(text(
                "SELECT count(*) FROM memberships m JOIN users u "
                "ON u.id = m.user_id")).scalar_one() == 1
    finally:
        engine.dispose()
