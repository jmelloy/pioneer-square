"""Create claude_usage table.

Revision ID: 20260530_000000_create_claude_usage
Revises: 20260527_000000_create_push_tokens
Create Date: 2026-05-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260530_000000_create_claude_usage"
down_revision: str | Sequence[str] | None = "20260527_000000_create_push_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "claude_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "guild_id",
            sa.Integer(),
            sa.ForeignKey("guilds.id"),
            nullable=False,
        ),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("worker_id", sa.Text(), sa.ForeignKey("workers.id"), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("call_index", sa.Integer(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_creation_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("num_turns", sa.Integer(), nullable=True),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("reporter", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_claude_usage_guild_id", "claude_usage", ["guild_id"])
    op.create_index("ix_claude_usage_task_id", "claude_usage", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_claude_usage_task_id", table_name="claude_usage")
    op.drop_index("ix_claude_usage_guild_id", table_name="claude_usage")
    op.drop_table("claude_usage")
