"""Unify discord_threads/discord_task_threads/discord_foreman_threads into discord_thread_bindings.

Revision ID: 20260711_000001_unify_discord_thread_bindings
Revises: 20260709_000001_fold_issue_root_threads_into_discord_threads
Create Date: 2026-07-11

Collapses the three independent Discord thread-mapping tables into one
``(subject_type, subject_key) -> thread_id`` table (#885). Each old table had
its own near-identical lookup/create/save code path, and inbound message
routing only ever consulted two of the three — a reply inside a task's
live-stream thread (``discord_task_threads``) was silently dropped since
neither ``discord/gateway.py``'s wired-channel check nor
``discord/router.py``'s session resolver looked at that table. Folding all
three into one table with one lookup makes every bound thread routable by
construction.

Subject key formats:
    "issue"          "<issue_repo>#<issue_number>"   from discord_threads
    "task_stream"    "<task_id>"                     from discord_task_threads
    "foreman_daily"  "<guild_id>:<session_key>"       from discord_foreman_threads
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260711_000001_unify_discord_thread_bindings"
down_revision: str | Sequence[str] | None = (
    "20260709_000001_fold_issue_root_threads_into_discord_threads"
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discord_thread_bindings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_key", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_discord_thread_bindings_subject",
        "discord_thread_bindings",
        ["subject_type", "subject_key"],
        unique=True,
    )
    op.create_index(
        "uq_discord_thread_bindings_thread_id",
        "discord_thread_bindings",
        ["thread_id"],
        unique=True,
    )

    op.execute(
        sa.text(
            """
            INSERT INTO discord_thread_bindings
                (subject_type, subject_key, thread_id, created_at)
            SELECT 'issue', issue_repo || '#' || issue_number, thread_id, created_at
            FROM discord_threads
            ON CONFLICT (subject_type, subject_key) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO discord_thread_bindings
                (subject_type, subject_key, thread_id, created_at)
            SELECT 'task_stream', task_id, thread_id, created_at
            FROM discord_task_threads
            ON CONFLICT (subject_type, subject_key) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO discord_thread_bindings
                (subject_type, subject_key, thread_id, created_at)
            SELECT 'foreman_daily', guild_id || ':' || session_key, thread_id, created_at
            FROM discord_foreman_threads
            ON CONFLICT (subject_type, subject_key) DO NOTHING
            """
        )
    )

    op.drop_table("discord_threads")
    op.drop_table("discord_task_threads")
    op.drop_table("discord_foreman_threads")


def downgrade() -> None:
    op.create_table(
        "discord_threads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("issue_repo", sa.Text(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_discord_threads_repo_number",
        "discord_threads",
        ["issue_repo", "issue_number"],
        unique=True,
    )
    op.create_table(
        "discord_task_threads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_discord_task_threads_task_id",
        "discord_task_threads",
        ["task_id"],
        unique=True,
    )
    op.create_table(
        "discord_foreman_threads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("session_key", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_discord_foreman_threads_guild_session",
        "discord_foreman_threads",
        ["guild_id", "session_key"],
        unique=True,
    )

    op.execute(
        sa.text(
            """
            INSERT INTO discord_threads (issue_repo, issue_number, thread_id, created_at)
            SELECT split_part(subject_key, '#', 1),
                   split_part(subject_key, '#', 2)::integer,
                   thread_id,
                   created_at
            FROM discord_thread_bindings
            WHERE subject_type = 'issue'
            ON CONFLICT (issue_repo, issue_number) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO discord_task_threads (task_id, thread_id, created_at)
            SELECT subject_key, thread_id, created_at
            FROM discord_thread_bindings
            WHERE subject_type = 'task_stream'
            ON CONFLICT (task_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO discord_foreman_threads (guild_id, session_key, thread_id, created_at)
            SELECT split_part(subject_key, ':', 1),
                   split_part(subject_key, ':', 2),
                   thread_id,
                   created_at
            FROM discord_thread_bindings
            WHERE subject_type = 'foreman_daily'
            ON CONFLICT (guild_id, session_key) DO NOTHING
            """
        )
    )

    op.drop_index("uq_discord_thread_bindings_thread_id", table_name="discord_thread_bindings")
    op.drop_index("uq_discord_thread_bindings_subject", table_name="discord_thread_bindings")
    op.drop_table("discord_thread_bindings")
