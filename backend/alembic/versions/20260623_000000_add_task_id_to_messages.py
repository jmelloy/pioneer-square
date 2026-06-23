"""Add task_id column to messages table.

Revision ID: 20260623_000000_add_task_id_to_messages
Revises: 20260621_000000_add_guild_invites
Create Date: 2026-06-23

Adds a nullable ``task_id`` FK column to ``messages`` so that chat messages
created in response to task events (tool calls, webhook events, CI results,
foreman responses) can be linked to the task that caused them. General user
chat and system-level messages (worker-online, periodic-check, etc.) remain
NULL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260623_000000_add_task_id_to_messages"
down_revision: str | Sequence[str] | None = "20260621_000000_add_guild_invites"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.id"), nullable=True),
    )
    op.create_index("ix_messages_task_id", "messages", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_task_id", table_name="messages")
    op.drop_column("messages", "task_id")
