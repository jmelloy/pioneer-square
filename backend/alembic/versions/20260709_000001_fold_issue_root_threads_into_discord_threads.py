"""Fold issue-root Discord threads into discord_threads; drop tasks.discord_thread_id.

Revision ID: 20260709_000001_fold_issue_root_threads_into_discord_threads
Revises: 20260709_000000_add_github_issues_and_pull_requests
Create Date: 2026-07-09

The issue-rooted task tree (phase='issue' root tasks, #832) is retired:
Discord threads are now keyed canonically by (issue_repo, issue_number) in
``discord_threads`` (see discord_notifier._canonical_coords, #866). Copy each
legacy root's thread into that table under its issue coordinates so in-flight
issues keep routing to their existing thread, then drop the per-task column.

Roots without issue linkage (created before create_task accepted
issue_number/issue_repo and not yet backfilled by
scripts/backfill_task_linkage.py) can't be migrated — their threads are
orphaned and future notifications for those issues start a fresh thread. Run
the backfill before deploying this migration to minimise that set.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260709_000001_fold_issue_root_threads_into_discord_threads"
down_revision: str | Sequence[str] | None = "20260709_000000_add_github_issues_and_pull_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO discord_threads (issue_repo, issue_number, thread_id, created_at)
            SELECT issue_repo, issue_number, discord_thread_id, now()
            FROM tasks
            WHERE discord_thread_id IS NOT NULL
              AND issue_repo IS NOT NULL
              AND issue_number IS NOT NULL
            ON CONFLICT (issue_repo, issue_number) DO NOTHING
            """
        )
    )
    op.drop_column("tasks", "discord_thread_id")


def downgrade() -> None:
    # The thread-id copies in discord_threads are left in place — they are
    # valid rows under the old model too.
    op.add_column("tasks", sa.Column("discord_thread_id", sa.Text(), nullable=True))
