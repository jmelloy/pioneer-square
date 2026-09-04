"""Add conversation_id columns to github_issues, github_pull_requests.

Revision ID: 20260904_010000_add_conversation_id_to_github_cache
Revises: 20260904_000000_add_conversation_id_columns
Create Date: 2026-09-04

Second step of issue #1271 for the GitHub entity models (#1277): adds a
nullable ``conversation_id`` FK to ``conversations.id`` on the two remaining
GitHub cache tables, mirroring what
``20260904_000000_add_conversation_id_columns`` already did for
``github_events``.

``github_issues``/``github_pull_requests`` have no direct FK to a task —
they're keyed on (repo, number) and correlate to ``tasks`` via
``tasks.issue_repo``/``issue_number`` (issues) or ``tasks.pr_repo``/
``pr_number`` (PRs). Multiple tasks can reference the same issue/PR over
time (e.g. a redone review), so the backfill picks each (repo, number)'s
most-recently-created task with a resolved conversation_id. Additive only:
this doesn't change how ``github_issues``/``github_pull_requests`` rows are
looked up anywhere.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_010000_add_conversation_id_to_github_cache"
down_revision: str | Sequence[str] | None = "20260904_000000_add_conversation_id_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "github_issues",
        sa.Column(
            "conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=True
        ),
    )
    op.create_index("ix_github_issues_conversation_id", "github_issues", ["conversation_id"])

    op.add_column(
        "github_pull_requests",
        sa.Column(
            "conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=True
        ),
    )
    op.create_index(
        "ix_github_pull_requests_conversation_id", "github_pull_requests", ["conversation_id"]
    )

    conn = op.get_bind()

    conn.execute(
        sa.text(
            "UPDATE github_issues gi "
            "SET conversation_id = t.conversation_id "
            "FROM ("
            "  SELECT DISTINCT ON (issue_repo, issue_number) issue_repo, issue_number, conversation_id "
            "  FROM tasks "
            "  WHERE conversation_id IS NOT NULL "
            "    AND issue_repo IS NOT NULL AND issue_number IS NOT NULL "
            "  ORDER BY issue_repo, issue_number, created_at DESC"
            ") t "
            "WHERE gi.repo = t.issue_repo AND gi.number = t.issue_number "
            "AND gi.conversation_id IS NULL"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE github_pull_requests gp "
            "SET conversation_id = t.conversation_id "
            "FROM ("
            "  SELECT DISTINCT ON (pr_repo, pr_number) pr_repo, pr_number, conversation_id "
            "  FROM tasks "
            "  WHERE conversation_id IS NOT NULL "
            "    AND pr_repo IS NOT NULL AND pr_number IS NOT NULL "
            "  ORDER BY pr_repo, pr_number, created_at DESC"
            ") t "
            "WHERE gp.repo = t.pr_repo AND gp.number = t.pr_number "
            "AND gp.conversation_id IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_github_pull_requests_conversation_id", table_name="github_pull_requests")
    op.drop_column("github_pull_requests", "conversation_id")

    op.drop_index("ix_github_issues_conversation_id", table_name="github_issues")
    op.drop_column("github_issues", "conversation_id")
