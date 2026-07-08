"""Add 'issue' phase value and index tasks(issue_repo, issue_number).

Revision ID: 20260708_000000_add_issue_phase_and_index
Revises: 20260706_000000_add_id_pk_everywhere
Create Date: 2026-07-08

Part of the issue-rooted task tree feature (#815). tasks.phase is a plain
Text column (no DB-level enum type/CHECK constraint exists), so "issue" is
simply a new value the application may write — no DDL is required for that
part. The composite index supports looking up the task tree for a given
GitHub issue by (issue_repo, issue_number) without a full table scan.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260708_000000_add_issue_phase_and_index"
down_revision: str | Sequence[str] | None = "20260706_000000_add_id_pk_everywhere"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_tasks_issue_repo_issue_number",
        "tasks",
        ["issue_repo", "issue_number"],
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_issue_repo_issue_number", table_name="tasks")
