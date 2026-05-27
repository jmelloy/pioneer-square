"""add_api_keys_to_users

Adds ``claude_api_key`` and ``openai_api_key`` nullable columns to the
``users`` table to support per-user API key storage for the user-settings UI.

Revision ID: 20260527_000000_add_api_keys_to_users
Revises: 20260526_000000_convert_datetime_columns
Create Date: 2026-05-27

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260527_000000_add_api_keys_to_users"
down_revision: str | Sequence[str] | None = "20260526_000000_convert_datetime_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("claude_api_key", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("openai_api_key", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("openai_api_key")
        batch_op.drop_column("claude_api_key")
