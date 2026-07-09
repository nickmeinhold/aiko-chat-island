"""Account-deletion cascade COMPLETENESS guards (the verify-the-neighbor class).

`delete_user_account` must tear down every child of `users` — message tombstone,
moderation rows, device tokens, passkey credentials, social identities,
memberships. That completeness was enforced for a long time only by a MANUAL
"verify-the-neighbor" checklist in code comments, and a manual checklist is a
prose gate: it failed silently TWICE (device_tokens, then passkey_credentials,
each orphaning rows that pointed at a now-deleted user). These two tests convert
that discipline into a runtime valve.

Two complementary guards, each covering the other's blind spot:

  * `test_users_fk_set_is_the_expected_set` — a STRUCTURAL tripwire. It introspects
    `Base.metadata` for every column with a foreign key to `users.id` and asserts
    the set is exactly the one the cascade is known to handle. Add a new
    users-referencing table and THIS test fails first — pointing you at
    `delete_user_account` before the orphan can ever reach prod. Seeding-independent:
    catches the table's mere EXISTENCE.

  * `test_deletion_leaves_no_row_referencing_the_user` — a BEHAVIORAL proof. It
    seeds a row in every FK-to-`users` column, asserts each references the user
    BEFORE deletion (so the test can't pass vacuously), runs `delete_user_account`,
    then GENERICALLY asserts no FK-to-`users` column anywhere still references the
    deleted user. Proves the cascade actually clears the tables that exist today.

The behavioral test alone would pass vacuously for a future table nobody seeded
(the count loop finds 0 rows because nothing inserted any); the tripwire is what
forces that future table to be noticed. The tripwire alone proves nothing about
behavior. Keep both.
"""
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine)
from sqlalchemy.pool import StaticPool

from aiko_gateway.db import Base
from aiko_gateway.domain import (
    accounts_service, devices_service, moderation_service,
    signing_keys_service, users_service)
from aiko_gateway.domain.models import (
    DEFAULT_COMMUNITY_ID, Channel, Community, CommunityMembership, Membership,
    Message, PasskeyCredential, SigningKey, SocialIdentity, User)
from aiko_gateway.domain.ids import new_ulid


# This app uses a SINGLE database schema (SQLite today, default-schema Postgres on
# the planned migration), so a bare table name is a unique key and `table.key ==
# table.name`. The guard intentionally relies on that: it compares bare names here,
# in EXPECTED_USERS_FK_COLUMNS, and in the Base.metadata.tables[...] lookups, so all
# three stay consistent. `test_schema_is_single_namespace` below fails loudly if a
# table ever declares an explicit schema — the signal to revisit this assumption
# (and the deletion cascade) rather than let same-named tables collapse silently.
def _references_users_id(col) -> bool:
    """True iff `col` has a foreign key targeting `users.id` specifically — not
    merely some other `users` column (e.g. a future FK to `users.email`). Inspect
    the referenced column, not the box label: match the target table name AND that
    the target column is `id`."""
    return any(
        fk.column.table.name == "users" and fk.column.name == "id"
        for fk in col.foreign_keys
    )


def _users_fk_columns() -> set[tuple[str, str]]:
    """Every (table, column) whose FK targets `users.id` specifically."""
    found: set[tuple[str, str]] = set()
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if _references_users_id(col):
                found.add((table.name, col.name))
    return found


# The columns the account-deletion cascade is KNOWN to handle. Keep this in lockstep
# with `accounts_service.delete_user_account`: if you add a row here you must also add
# the teardown there (and vice versa). The tripwire test fails if reality drifts.
EXPECTED_USERS_FK_COLUMNS: set[tuple[str, str]] = {
    ("messages", "sender_user_id"),          # tombstone (ref -> NULL)
    ("message_reports", "reporter_user_id"),  # anonymize (ref -> NULL)
    ("user_blocks", "blocker_user_id"),       # delete (either direction)
    ("user_blocks", "blocked_user_id"),       # delete (either direction)
    ("device_tokens", "user_id"),             # delete
    ("passkey_credentials", "user_id"),       # delete
    ("signing_keys", "user_id"),              # delete (#1816 PR B)
    ("social_identities", "user_id"),         # delete
    ("memberships", "user_id"),               # delete
    ("community_memberships", "user_id"),     # delete (#32)
    ("communities", "owner_id"),              # anonymize (ref -> NULL) (#32)
}


def test_schema_is_single_namespace():
    """The guard's bare-table-name keying assumes a single schema (so `table.key ==
    table.name`). If a table ever declares an explicit schema, that assumption — and
    the deletion cascade's table lookups — must be revisited, so fail loudly here
    rather than let two same-named tables collapse silently in the FK set."""
    schemad = sorted(t.name for t in Base.metadata.tables.values() if t.schema is not None)
    assert not schemad, (
        f"Tables now declare an explicit schema: {schemad}. The cascade guard keys "
        "FK columns on bare table names assuming a single namespace — revisit "
        "_users_fk_columns / EXPECTED_USERS_FK_COLUMNS (and delete_user_account) "
        "before this can be trusted under multiple schemas.")


def test_users_fk_set_is_the_expected_set():
    """STRUCTURAL tripwire — if this fails, a table now references `users.id` and
    the cascade may not know about it. Wire the new column into
    `accounts_service.delete_user_account`, then add it to EXPECTED_USERS_FK_COLUMNS.
    Do NOT just update the set to make the test green — that re-buries the bug."""
    actual = _users_fk_columns()
    missing_from_expected = actual - EXPECTED_USERS_FK_COLUMNS
    gone_from_schema = EXPECTED_USERS_FK_COLUMNS - actual
    assert not missing_from_expected, (
        "New FK(s) to users.id are NOT yet declared as cascade-handled: "
        f"{sorted(missing_from_expected)}. Wire each into delete_user_account "
        "(children-before-parent), then add it to EXPECTED_USERS_FK_COLUMNS.")
    assert not gone_from_schema, (
        "Expected FK(s) to users.id vanished from the schema: "
        f"{sorted(gone_from_schema)}. Update EXPECTED_USERS_FK_COLUMNS and check "
        "delete_user_account no longer references a dropped table.")


async def _seed_full_user_graph(session):
    """Seed a row in EVERY FK-to-`users` column (the full deletion blast radius),
    returning (primary_user, other_user). Shared by the FK-off behavioral proof and
    the FK-enforced probe so both exercise the identical graph. Rows are added
    parent-before-child so the seed itself is valid even under
    `PRAGMA foreign_keys=ON`."""
    # Primary user (create_social_user also makes the social_identities row).
    user = await users_service.create_social_user(
        session, provider="google", provider_sub="g-primary",
        handle="primary", display_name="Primary", email="primary@example.com")
    # A second user so the block pair + a foreign-authored message exist.
    other = await users_service.create_social_user(
        session, provider="google", provider_sub="g-other",
        handle="other", display_name="Other", email="other@example.com")

    ch = Channel(id="C".ljust(26, "0"), name="general", kind="standard",
                 aiko_channel="general")
    session.add(ch)
    session.add(Membership(channel_id=ch.id, user_id=user.id, role="member"))
    # messages.sender_user_id — primary authored M1; other authored M2.
    session.add(Message(
        id="M1".ljust(26, "0"), channel_id=ch.id, sender_user_id=user.id,
        sender_kind="human", sender_label="Primary", body="hi",
        created_at=dt.datetime(2026, 6, 28, tzinfo=dt.timezone.utc)))
    session.add(Message(
        id="M2".ljust(26, "0"), channel_id=ch.id, sender_user_id=other.id,
        sender_kind="human", sender_label="Other", body="yo",
        created_at=dt.datetime(2026, 6, 28, tzinfo=dt.timezone.utc)))
    # passkey_credentials.user_id (create_social_user already covered identities).
    session.add(PasskeyCredential(
        credential_id="cred-primary", user_id=user.id,
        public_key="cHVibGlj", sign_count=0))
    # communities.owner_id (anonymized on delete) + community_memberships.user_id
    # (deleted on delete) — the two FK-to-users columns added in #32. The owned
    # community must survive deletion with owner_id NULLed, not vanish.
    owned = Community(id="K".ljust(26, "0"), name="Owned", visibility="public",
                      category="general", owner_id=user.id, member_count=1)
    session.add(owned)
    await session.flush()
    session.add(CommunityMembership(
        community_id=owned.id, user_id=user.id, role="member"))
    await session.commit()

    # device_tokens.user_id
    await devices_service.register_device(
        session, user_id=user.id, platform="apns", token="tok-primary")
    # user_blocks: primary blocks other (blocker_user_id) AND other blocks primary
    # (blocked_user_id) — cover BOTH FK columns on user_blocks.
    await moderation_service.block_user(session, user.id, other.id)
    await moderation_service.block_user(session, other.id, user.id)
    # message_reports.reporter_user_id — primary reports other's message.
    await moderation_service.report_message(
        session, reporter_id=user.id, message_id="M2".ljust(26, "0"), reason="spam")
    # signing_keys.user_id (#1816 PR B) — primary has an observed signing key.
    # record_signing_key does not commit (caller owns the txn), so commit here.
    await signing_keys_service.record_signing_key(
        session, user_id=user.id, pubkey="z-signing-primary", key_version=1)
    await session.commit()
    return user, other


async def test_deletion_leaves_no_row_referencing_the_user(session):
    """BEHAVIORAL proof — seed every FK-to-users column, delete, assert no orphan.

    The generic post-condition loop covers EVERY current FK-to-users column (and
    any future one, once seeded) without naming them one by one — so the cascade's
    correctness is proven by the schema, not by a hand-maintained list."""
    user, other = await _seed_full_user_graph(session)

    # Precondition: EVERY expected FK-to-users column actually references the user
    # now — otherwise a green post-condition would prove nothing for that column.
    for table_name, col_name in EXPECTED_USERS_FK_COLUMNS:
        table = Base.metadata.tables[table_name]
        col = table.c[col_name]
        cnt = (await session.execute(
            select(func.count()).select_from(table).where(col == user.id))).scalar_one()
        assert cnt >= 1, (
            f"seed gap: {table_name}.{col_name} references no row for the user "
            "before deletion — the post-condition would be vacuous for it.")

    await accounts_service.delete_user_account(session, user.id)

    # Post-condition (GENERIC): no FK-to-users column anywhere still points at the
    # deleted user. Drives off live metadata, so a new column is covered the moment
    # it's seeded — no per-column edit here.
    for table_name, col_name in sorted(_users_fk_columns()):
        table = Base.metadata.tables[table_name]
        col = table.c[col_name]
        cnt = (await session.execute(
            select(func.count()).select_from(table).where(col == user.id))).scalar_one()
        assert cnt == 0, (
            f"ORPHAN: {table_name}.{col_name} still references the deleted user — "
            "delete_user_account does not tear this down.")

    # And the user row itself is gone.
    assert await users_service.get_by_id(session, user.id) is None

    # The owned community SURVIVES as a tombstone — anonymized, NOT shredded. The
    # generic post-condition above only proves no row still *references* the user;
    # this proves the community row itself endures with owner_id NULLed (a community
    # is shared infrastructure, like a channel — see accounts_service / #32).
    surviving = (await session.execute(
        select(Community).where(Community.id == "K".ljust(26, "0")))).scalar_one()
    assert surviving.owner_id is None
    # member_count was decremented (the deleted user was its sole member: 1 -> 0),
    # so the denormalized count doesn't drift on account deletion.
    assert surviving.member_count == 0


@pytest_asyncio.fixture
async def fk_enforced_session() -> AsyncSession:
    """A fresh in-memory DB per test with SQLite FK enforcement turned ON.

    The default `session` fixture (conftest) leaves `PRAGMA foreign_keys=OFF` to
    match prod — the gateway enforces no DB-level FKs and relies on application-
    level cascades. This fixture deliberately turns enforcement ON so a test can
    use SQLite's own referential engine as an INDEPENDENT auditor of the manual
    cascade: a teardown that either misses a child or deletes the parent before
    its children raises an IntegrityError here that the FK-off path swallows."""
    # StaticPool → ONE shared connection for the whole engine, so the in-memory DB
    # (and the per-connection foreign_keys pragma) persists across create_all, the
    # seed, and the test session. With the default pool a :memory: engine can hand
    # out a fresh empty DB per connection, and the FK pragma would only arm some of
    # them — both silent footguns this avoids.
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Tests use create_all, not migrations; under FK enforcement the default
        # "Aiko" community row (seeded by migration 0009 in prod) must exist BEFORE
        # any channel insert, because Channel.community_id defaults to
        # DEFAULT_COMMUNITY_ID and FK-references communities.id. System-owned
        # (owner_id NULL), mirroring the migration. Without this, the very first
        # channel insert would FK-fail — itself the finding that an FK-off prod
        # silently tolerates a channel pointing at a non-existent community.
        # created_at is NOT NULL with a Python-side default that raw SQL bypasses,
        # so supply it explicitly.
        await conn.exec_driver_sql(
            "INSERT INTO communities (id, name, visibility, category, owner_id, "
            "member_count, created_at) VALUES "
            "(?, 'Aiko', 'public', 'general', NULL, 0, '2026-06-30 00:00:00+00:00')",
            (DEFAULT_COMMUNITY_ID,))
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def test_fixture_actually_enforces_foreign_keys(fk_enforced_session):
    """Guard the guard: prove the fixture's `PRAGMA foreign_keys=ON` is in force, so
    the probe below can't pass vacuously (an FK-off engine would accept the orphan
    insert and the probe would prove nothing). Insert a membership pointing at a
    non-existent user and assert SQLite REJECTS it."""
    import sqlalchemy.exc

    fk_enforced_session.add(Channel(
        id="C".ljust(26, "0"), name="general", kind="standard",
        aiko_channel="general"))
    await fk_enforced_session.flush()
    fk_enforced_session.add(Membership(
        channel_id="C".ljust(26, "0"), user_id="nonexistent-user", role="member"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await fk_enforced_session.flush()


async def _seed_fk_safe(session):
    """Seed the SAME full FK-to-`users` graph as `_seed_full_user_graph`, but in
    explicit dependency LAYERS (flush after each) so the seed itself is valid under
    `PRAGMA foreign_keys=ON`. Returns (primary_user, other_user).

    Why not reuse `_seed_full_user_graph`? It calls `create_social_user`, which adds
    a `User` and its `SocialIdentity` in ONE flush. The models define no
    `relationship()` between them, so SQLAlchemy's unit-of-work has no per-object
    dependency edge and inserts `social_identities` BEFORE `users` — which RESTRICT
    rejects under enforcement. That non-FK-safe ordering is harmless on FK-off prod
    but means the real create path can't run here; we seed the users directly,
    then reuse the genuinely FK-safe single-row services (register_device /
    block_user / report_message) for the rest. (Tracked: the create paths assume
    FK-off — see the session findings / follow-up task.)"""
    # Layer 1 — users (every other table references these).
    user = User(id=new_ulid(), username="primary", display_name="Primary",
                password_hash=None, aiko_username="primary",
                email="primary@example.com")
    other = User(id=new_ulid(), username="other", display_name="Other",
                 password_hash=None, aiko_username="other",
                 email="other@example.com")
    session.add_all([user, other])
    await session.flush()
    # Layer 2 — direct children of users / communities.
    session.add_all([
        SocialIdentity(provider="google", provider_sub="g-primary", user_id=user.id),
        SocialIdentity(provider="google", provider_sub="g-other", user_id=other.id),
    ])
    ch = Channel(id="C".ljust(26, "0"), name="general", kind="standard",
                 aiko_channel="general")  # community_id defaults to the seeded Aiko
    owned = Community(id="K".ljust(26, "0"), name="Owned", visibility="public",
                      category="general", owner_id=user.id, member_count=1)
    session.add_all([ch, owned])
    await session.flush()
    # Layer 3 — rows referencing channel / community / messages' parents.
    session.add_all([
        Membership(channel_id=ch.id, user_id=user.id, role="member"),
        Message(id="M1".ljust(26, "0"), channel_id=ch.id, sender_user_id=user.id,
                sender_kind="human", sender_label="Primary", body="hi",
                created_at=dt.datetime(2026, 6, 28, tzinfo=dt.timezone.utc)),
        Message(id="M2".ljust(26, "0"), channel_id=ch.id, sender_user_id=other.id,
                sender_kind="human", sender_label="Other", body="yo",
                created_at=dt.datetime(2026, 6, 28, tzinfo=dt.timezone.utc)),
        PasskeyCredential(credential_id="cred-primary", user_id=user.id,
                          public_key="cHVibGlj", sign_count=0),
        CommunityMembership(community_id=owned.id, user_id=user.id, role="member"),
    ])
    await session.commit()
    # The remaining FK columns via real services — each is a single-row insert
    # against already-committed parents, so it is FK-safe under enforcement.
    await devices_service.register_device(
        session, user_id=user.id, platform="apns", token="tok-primary")
    await moderation_service.block_user(session, user.id, other.id)
    await moderation_service.block_user(session, other.id, user.id)
    await moderation_service.report_message(
        session, reporter_id=user.id, message_id="M2".ljust(26, "0"), reason="spam")
    # signing_keys.user_id (#1816 PR B) — FK-safe (user committed above); commit
    # since record_signing_key leaves the txn to the caller.
    await signing_keys_service.record_signing_key(
        session, user_id=user.id, pubkey="z-signing-primary", key_version=1)
    await session.commit()
    return user, other


async def test_deletion_satisfies_referential_integrity(fk_enforced_session):
    """PROBE — the manual cascade must also satisfy SQLite's referential engine.

    Same full graph as the behavioral proof, but under `PRAGMA foreign_keys=ON`.
    This catches a class the FK-off behavioral test cannot: an ORDERING bug. None
    of the FKs declare `ondelete=CASCADE`, so with enforcement on every FK is
    RESTRICT — `delete_user_account` must tear down (or NULL) each child BEFORE the
    parent `users` row, or the delete raises IntegrityError. The FK-off path
    swallows a wrong order silently (it just leaves an orphan the other test then
    flags); here a wrong order is a hard failure. Passing here proves the cascade
    is both COMPLETE and correctly ORDERED — the property prod's app-level cascade
    actually depends on."""
    user, _other = await _seed_fk_safe(fk_enforced_session)

    # The operation under test. Under FK enforcement this RAISES if the cascade
    # touches the parent before a child, or misses a child a NOT-NULL FK guards.
    await accounts_service.delete_user_account(fk_enforced_session, user.id)

    # Belt-and-suspenders: no orphan survived (mirrors the FK-off post-condition).
    for table_name, col_name in sorted(_users_fk_columns()):
        table = Base.metadata.tables[table_name]
        col = table.c[col_name]
        cnt = (await fk_enforced_session.execute(
            select(func.count()).select_from(table).where(col == user.id))).scalar_one()
        assert cnt == 0, (
            f"ORPHAN under FK enforcement: {table_name}.{col_name} still references "
            "the deleted user.")
    assert await users_service.get_by_id(fk_enforced_session, user.id) is None
