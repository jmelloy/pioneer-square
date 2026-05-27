"""Create push_tokens table.

Revision ID: 20260527_000000_create_push_tokens
Revises: 20260526_000000_convert_datetime_columns
Create Date: 2026-05-27

Stores APNs (and, later, FCM) device tokens per user. The token is the
primary key — Apple gives every install of the app a unique token, and
the same token coming back from a different user means the device was
re-provisioned, so we just overwrite the user_id.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260527_000000_create_push_tokens"
down_revision: str | Sequence[str] | None = "20260526_000000_convert_datetime_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_tokens",
        sa.Column("token", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False, server_default="ios"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_push_tokens_user_id", "push_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_push_tokens_user_id", table_name="push_tokens")
    op.drop_table("push_tokens")
