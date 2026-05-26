"""Convert all datetime columns from Text to DateTime(timezone=True).

Revision ID: 20260526_000000_convert_datetime_columns
Revises: 20260520_000007_merge_rename_with_prior_head
Create Date: 2026-05-26

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260526_000000_convert_datetime_columns"
down_revision: str | Sequence[str] | None = "20260520_000007_merge_rename_with_prior_head"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column, nullable)
_COLUMNS = [
    ("guilds", "created_at", False),
    ("guilds", "deleted_at", True),
    ("agents", "joined_at", False),
    ("agents", "last_seen", True),
    ("messages", "created_at", False),
    ("workers", "created_at", False),
    ("workers", "last_seen", True),
    ("tasks", "created_at", False),
    ("tasks", "finished_at", True),
    ("tasks", "deleted_at", True),
    ("github_tokens", "created_at", False),
    ("github_tokens", "updated_at", False),
    ("user_sessions", "created_at", False),
    ("users", "created_at", False),
    ("users", "updated_at", False),
    ("guild_members", "created_at", False),
    ("task_logs", "timestamp", False),
    ("claude_credentials", "updated_at", False),
    ("guild_keys", "created_at", False),
    ("foreman_turns", "created_at", False),
    ("github_events", "created_at", False),
    ("task_events", "created_at", False),
]


def upgrade() -> None:
    # IMPORTANT: postgresql_using casts existing text values directly to
    # timestamptz. This succeeds only when every stored value is a valid
    # ISO-8601 timestamp string. If any row contains a non-parseable value
    # the ALTER TABLE will fail mid-migration. Verify clean data beforehand:
    #   SELECT table, column, value FROM <table> WHERE <column> !~ '^\d{4}-\d{2}-\d{2}'
    for table, column, nullable in _COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.Text(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=nullable,
            postgresql_using=f"{column}::timestamptz",  # safe for ISO-8601 text strings
        )


def downgrade() -> None:
    for table, column, nullable in reversed(_COLUMNS):
        op.alter_column(
            table,
            column,
            existing_type=sa.DateTime(timezone=True),
            type_=sa.Text(),
            existing_nullable=nullable,
            postgresql_using=f"{column}::text",
        )
