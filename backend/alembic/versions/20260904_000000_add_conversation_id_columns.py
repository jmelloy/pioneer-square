"""Add conversation_id columns to messages, tasks, foreman_turns, github_events.

Revision ID: 20260904_000000_add_conversation_id_columns
Revises: 20260831_000000
Create Date: 2026-09-04

First step of issue #1271 ("make Conversation the core Foreman thread
model"): adds a nullable ``conversation_id`` FK to ``conversations.id`` on
every table that currently correlates via ``thread_id``/``task_id``, and
backfills it for existing rows:

  - ``messages.conversation_id``      <- ``threads.conversation_id`` via ``messages.thread_id``
  - ``tasks.conversation_id``         <- ``threads.conversation_id`` via ``tasks.thread_id``
  - ``foreman_turns.conversation_id`` <- ``conversations`` matched on (guild_id, user_id)
    (``foreman_turns`` has no ``thread_id`` column to join through; ``Conversation``
    is already keyed 1:1 on (guild_id, user_id) — see ``models.Conversation``)
  - ``github_events.conversation_id`` <- ``tasks.conversation_id`` via ``github_events.task_id``
    (run after the ``tasks`` backfill above so it has something to copy)

This is deliberately additive only: ``thread_id`` keeps being written and read
everywhere it already was (see the "Suggested first PR" section of #1271).
Nothing is backfilled for rows with no resolvable thread/task/conversation —
those stay NULL, matching every other best-effort correlation column in this
schema (e.g. ``tasks.thread_id`` itself).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_000000_add_conversation_id_columns"
down_revision: str | Sequence[str] | None = "20260831_000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=True
        ),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])

    op.add_column(
        "tasks",
        sa.Column(
            "conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=True
        ),
    )
    op.create_index("ix_tasks_conversation_id", "tasks", ["conversation_id"])

    op.add_column(
        "foreman_turns",
        sa.Column(
            "conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=True
        ),
    )
    op.create_index("ix_foreman_turns_conversation_id", "foreman_turns", ["conversation_id"])

    op.add_column(
        "github_events",
        sa.Column(
            "conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=True
        ),
    )
    op.create_index("ix_github_events_conversation_id", "github_events", ["conversation_id"])

    conn = op.get_bind()

    conn.execute(
        sa.text(
            "UPDATE messages SET conversation_id = threads.conversation_id "
            "FROM threads WHERE messages.thread_id = threads.id "
            "AND messages.conversation_id IS NULL"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE tasks SET conversation_id = threads.conversation_id "
            "FROM threads WHERE tasks.thread_id = threads.id "
            "AND tasks.conversation_id IS NULL"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE foreman_turns SET conversation_id = conversations.id "
            "FROM conversations "
            "WHERE conversations.guild_id = foreman_turns.guild_id "
            "AND conversations.user_id = foreman_turns.user_id "
            "AND foreman_turns.conversation_id IS NULL"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE github_events SET conversation_id = tasks.conversation_id "
            "FROM tasks WHERE github_events.task_id = tasks.id "
            "AND tasks.conversation_id IS NOT NULL "
            "AND github_events.conversation_id IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_github_events_conversation_id", table_name="github_events")
    op.drop_column("github_events", "conversation_id")

    op.drop_index("ix_foreman_turns_conversation_id", table_name="foreman_turns")
    op.drop_column("foreman_turns", "conversation_id")

    op.drop_index("ix_tasks_conversation_id", table_name="tasks")
    op.drop_column("tasks", "conversation_id")

    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_column("messages", "conversation_id")
