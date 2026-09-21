"""messages.sender_kind — rename 'actor' to 'unknown', then close the set (#3144)

TWO CHANGES IN ONE REVISION because they are one decision: the set being closed is
the set being corrected, and landing the CHECK first would pin a name we had
already decided was wrong.

=== WHY THE RENAME (Nick's call, 2026-09-20) ===

In aiko_services an `Actor` is a precise and nearly OPPOSITE thing.
`aiko_services/main/actor.py:198` defines `class Actor(Service)` on the Hewitt
actor model: a registered name, a protocol string, an ordered priority mailbox at
`{topic_path}/in`, leases, a lifecycle, an EC producer publishing its state — and
an entire `ActorDiscovery` class exists to find them. An Actor is the most
strongly IDENTIFIED participant on the bus.

This column used `actor` for a sender the island could NOT identify. A reader who
knows the framework reads that as a stronger identity claim than 'human' rather
than the absence of one.

The name also looked right for the wrong reason, which is what made it survive.
`aiko-chat-bridge/examples/reference_robot.py:60` is `class ReferenceRobot(Actor)`
— @@armbot, which produced 58 of the 76 rows holding the old value, genuinely IS
an aiko Actor. But the island never checked that: `_kind_for` failed a
`users.aiko_username` lookup and fell back to a word that happened to fit 76% of
the time. For the other 18 rows — messages a PERSON typed, which returned from the
bus with no username (#4665) — it asserted that a registered bus service had
spoken.

'unknown' says what is actually true: no island account matched this sender. "A
registered aiko Actor sent this" is a claim nothing here can make, and if it is
ever wanted it needs its own member derived from a real Actor lookup.

=== WHY THE CHECK ===

sender_kind was the LAST closed-set-shaped column with no closure. Every other one
drives a CHECK from its enum via _in_check — ck_users_kind, ck_channels_kind,
ck_memberships_role and ten more; sender_kind was absent from all thirteen. Its
vocabulary was EMERGENT from _kind_for's control flow rather than declared: two
members borrowed from UserKind, two from ChannelKind, and the fallback declared in
no enum at all. The #2633 cage-match ruled that a rendering-relevant kind must not
rest on an unenforced open string; this is that column.

The absence was not theoretical. Adding this CHECK immediately failed 42 tests,
because fixtures had been writing `sender_kind="user"` — a SIXTH value, in no enum
and no docstring, which survived only because nothing could object to it.

=== THREE MEMBERS, NOT FIVE ===

'llm' and 'robot' were reachable only through _kind_for's channel fallback, and
nothing can create a channel of those kinds: the three writers of channels.kind are
dm_service (hardcoded DM), channels_service (hardcoded 'standard') and
memberships_service (a 'standard' default whose only caller never passes it). Dead
by construction, so that arm is removed in this change rather than marked — it is
not inert but CONTRADICTORY, since a branch writing a value this CHECK rejects
turns a silent nothing into a 500 on send.

Nick's ruling 2026-09-20: an absent writer here means NOT BUILT, not declined. So
the work that makes an llm/robot channel creatable restores the arm AND adds the
members in ONE migration — the paired step ChannelKind's docstring argues for about
'group'.

=== SAFE AGAINST LIVE DATA — MEASURED, NOT ASSUMED ===

Read-only against both production DBs on 2026-09-20, before this file was written:

    chat.enspyr.co        sender_kind: human 171, actor 76
    chat.imagineering.cc  sender_kind: human 44,  actor 2

293 rows, two distinct values. The UPDATE below rewrites the 78 'actor' rows; no
other value exists on either island, so nothing falls outside the new set and no
repair arm is needed. The same probe found channels.kind holding only 'dm' and
'standard' on both boxes — the live half of the dead-by-construction argument.
Re-run before deploying if more than a few days pass:

    SELECT sender_kind, count(*) FROM messages GROUP BY 1;

ORDER MATTERS: the UPDATE runs BEFORE create_check_constraint. batch_alter_table
rebuilds the table and copies rows through; a surviving 'actor' row would fail the
new constraint mid-rebuild and abort the migration on a live box.

NO server_default, deliberately — unlike 0022. That column was being ADDED and
needed every existing row to acquire an honest value; this one already exists and
is already NOT NULL. A default would also invent a silent answer for a writer that
forgot to say who sent a message, and "unattributed" is not something this column
should be able to mean by omission.

=== THE READ PATH STAYS OPEN, AND THE WIRE ORDER MATTERS ===

The client (aiko_chat_app, message.dart) degrades an unrecognised sender_kind to
its generic badge through a documented `default:` arm. So the column stays
Mapped[str] with no load-time coercion: closing the read too would 500 a history
fetch on a value the consumer has already said it handles.

That tolerance is also what makes this rename safe to ship island-first, which the
silent-desync rule requires. An app that has not yet added an `unknown` case sees
it hit `default:` and renders the same generic badge it rendered for 'actor' — the
display does not change for the 78 affected rows. Deploying the APP first would be
the unsafe order: it would wait for a value no island yet sends.

=== MECHANICS ===

HAND-WRITTEN, not autogenerated: alembic's compare_metadata is CHECK-blind on
SQLite (the deploy dialect), so --autogenerate emits no constraint. The literal
below must stay in sync with _in_check("sender_kind", SenderKind) in
domain/models.py; tests/test_migrations asserts ck_messages_sender_kind
structurally, reads the clause out of the migrated DDL, and makes the database
refuse a bad value.

SQLite cannot ALTER TABLE ... ADD CONSTRAINT, so batch_alter_table rebuilds
``messages`` (create new + copy + swap). ``messages`` is the largest table on both
islands and a PARENT of reactions.message_id, message_reports.message_id,
retractions.message_id and its own reply_to self-reference. The swap is safe
because the gateway does NOT enable SQLite PRAGMA foreign_keys — it relies on
application-level explicit cascades (db.py / accounts_service, ISL-0002) — so
dropping and recreating the parent cannot trip a child FK violation mid-rebuild.
Same note as 0002, 0020 and 0022.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-20
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Must match _in_check("sender_kind", SenderKind) in domain/models.py exactly
# (parity gate). Member ORDER follows the enum's declaration order, not alphabetical.
_SENDER_KIND_CHECK = "sender_kind IN ('human', 'agent', 'unknown')"

_OLD_UNKNOWN = "actor"
_NEW_UNKNOWN = "unknown"


def upgrade() -> None:
    # BEFORE the constraint: a surviving old-name row would fail the new CHECK
    # during batch_alter_table's copy and abort the migration on a live box.
    op.execute(
        sa.text("UPDATE messages SET sender_kind = :new WHERE sender_kind = :old")
        .bindparams(new=_NEW_UNKNOWN, old=_OLD_UNKNOWN))
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.create_check_constraint("ck_messages_sender_kind", _SENDER_KIND_CHECK)


def downgrade() -> None:
    # Drop the constraint FIRST, or restoring the old name writes a value the
    # still-present CHECK rejects — the mirror of the upgrade's ordering argument.
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_constraint("ck_messages_sender_kind", type_="check")
    op.execute(
        sa.text("UPDATE messages SET sender_kind = :old WHERE sender_kind = :new")
        .bindparams(new=_NEW_UNKNOWN, old=_OLD_UNKNOWN))
    # NOTE: this restores the DATA but not _kind_for's removed channel-kind arm,
    # which is application code travelling with the image. A downgraded DB simply
    # accepts values the running code no longer produces.
