"""Add thread_id column to messages table.

Revision ID: 20260815_000000_add_thread_id_to_messages
Revises: 20260813_000001_add_threads_and_conversations
Create Date: 2026-08-15

Adds a nullable ``thread_id`` FK column to ``messages`` so chat messages can be
linked to the Foreman-owned conversation :class:`~models.Thread` they belong
to (#1167), letting the frontend show each thread's own conversation history
instead of only the guild-wide comms pane. Mirrors the ``task_id`` column
added in ``20260623_000000_add_task_id_to_messages``. Existing rows and
messages not scoped to a conversation (webhook notices, etc.) remain NULL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260815_000000_add_thread_id_to_messages"
down_revision: str | Sequence[str] | None = "20260813_000001_add_threads_and_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("thread_id", sa.Text(), sa.ForeignKey("threads.id"), nullable=True),
    )
    op.create_index("ix_messages_thread_id", "messages", ["thread_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_thread_id", table_name="messages")
    op.drop_column("messages", "thread_id")
