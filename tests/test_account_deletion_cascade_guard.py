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

from sqlalchemy import func, select

from aiko_gateway.db import Base
from aiko_gateway.domain import (
    accounts_service, devices_service, moderation_service, users_service)
from aiko_gateway.domain.models import (
    Channel, Community, CommunityMembership, Membership, Message,
    PasskeyCredential)


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


async def test_deletion_leaves_no_row_referencing_the_user(session):
    """BEHAVIORAL proof — seed every FK-to-users column, delete, assert no orphan.

    The generic post-condition loop covers EVERY current FK-to-users column (and
    any future one, once seeded) without naming them one by one — so the cascade's
    correctness is proven by the schema, not by a hand-maintained list."""
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
