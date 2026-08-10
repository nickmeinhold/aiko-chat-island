"""memberships.user_id index for user-centric DM listing (#2633, cage-match PR#124)

GET /v1/dm ("my DM channels", channels_service.list_dms) filters memberships on
``user_id`` alone. The composite PK is ``(channel_id, user_id)`` — leading with
channel_id — so a user-first query cannot use it and table-scans as memberships grow
(Carnot). This adds the covering index. Purely additive (no data change, no rebuild);
safe on any dataset.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-10
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_memberships_user_id", table_name="memberships")
