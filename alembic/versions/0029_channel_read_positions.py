"""channel_read_positions — the island half of the same-view-across-devices ruling (#4834a)

WHY A TABLE AND NOT A COLUMN. A read position is per (user, channel), and there
is no existing row with that grain that a user owns: `memberships` is the closest,
but a position must survive LEAVING a channel (otherwise rejoining silently
resumes from a stale watermark, or from nothing) and must exist for channels a
membership row was never written for. Hanging it off memberships would tie a
private note's lifetime to an authorization fact.

THE SHAPE IS `{last_read, seq}`, where `seq` is the ISLAND'S version of the row,
advanced by compare-and-swap (a write names the version it edits; PR #429's v2
contract). It is a version, not a timestamp or a ULID comparison, so mark-as-unread
(an earlier ULID) applies like any other edit.

NO BACKFILL, AND NOTHING TO MIGRATE. Read positions have never been stored
server-side: `rest/dm.py`'s `list_dms` docstring records the old contract
verbatim ("NO ``unread`` (client-side per the contract — the island has no
read-position store)"), and the app holds them in SharedPreferences where they
"never leave the device" (`channel_read_store.dart`). Every existing user starts
with zero rows, and their device's local watermarks are uploaded by the client on
its next sync — which is the app half, and is NOT gated on this migration.

THE PK LEADS WITH user_id, the inverse of `memberships`. Every access path here
is user-first ("my read positions"); #2633 had to add `ix_memberships_user_id`
precisely because that table's composite PK leads with `channel_id` and a
user-first query could not use it. Leading correctly now costs nothing and avoids
adding the same index later.

FK TO users.id — SO THE ACCOUNT-DELETION CASCADE MUST HANDLE IT. Prod runs SQLite
with FK OFF and application-level cascades (ISL-0002), so nothing in the database
will clean these rows up. `accounts_service.delete_user_account` deletes them
explicitly, and `test_account_deletion_cascade_guard.py`'s structural tripwire is
what forces that to have happened: it introspects every FK-to-users.id column and
fails on a new one. This table was added WITH that test red, then green — the
tripwire worked as designed rather than being remembered.

CHECK ON seq >= 1: versions start at 1 and only go up. 0 is the wire's name for
"no row", so a stored row at 0 could never be moved; the column refuses it. Derived from the model via a literal that
`test_migrations`' generic parity gate compares against `Base.metadata`.

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-26
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "channel_read_positions",
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("channel_id", sa.String(length=26), nullable=False),
        # '' is the app's first-sight floor, not a missing value — so NOT NULL,
        # and an absence means exactly one thing: no row.
        sa.Column("last_read", sa.String(length=26), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.PrimaryKeyConstraint("user_id", "channel_id"),
        sa.CheckConstraint("seq >= 1", name="ck_channel_read_positions_seq"),
    )
    op.create_index("ix_channel_read_positions_channel_id",
                    "channel_read_positions", ["channel_id"])


def downgrade() -> None:
    op.drop_index("ix_channel_read_positions_channel_id",
                  table_name="channel_read_positions")
    op.drop_table("channel_read_positions")
