"""Rename claude_usage to llm_usage and add tool column.

Revision ID: 20260619_000000_rename_claude_usage_to_llm_usage
Revises: 20260614_000000_add_worker_disabled_column
Create Date: 2026-06-19

Renames the ``claude_usage`` table to ``llm_usage`` to reflect that usage
tracking now covers multiple tool runners (claude, pi, codex, …). Adds a
``tool`` column (TEXT, default ``'claude'``) so each row records which runner
produced the usage data.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260619_000000_rename_claude_usage_to_llm_usage"
down_revision: str | Sequence[str] | None = "20260614_000000_add_worker_disabled_column"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.rename_table("claude_usage", "llm_usage")
    op.drop_index("ix_claude_usage_guild_id", table_name="llm_usage")
    op.drop_index("ix_claude_usage_task_id", table_name="llm_usage")
    op.create_index("ix_llm_usage_guild_id", "llm_usage", ["guild_id"])
    op.create_index("ix_llm_usage_task_id", "llm_usage", ["task_id"])
    op.add_column(
        "llm_usage",
        sa.Column("tool", sa.Text(), nullable=True, server_default="'claude'"),
    )


def downgrade() -> None:
    op.drop_column("llm_usage", "tool")
    op.drop_index("ix_llm_usage_task_id", table_name="llm_usage")
    op.drop_index("ix_llm_usage_guild_id", table_name="llm_usage")
    op.create_index("ix_claude_usage_task_id", "llm_usage", ["task_id"])
    op.create_index("ix_claude_usage_guild_id", "llm_usage", ["guild_id"])
    op.rename_table("llm_usage", "claude_usage")
