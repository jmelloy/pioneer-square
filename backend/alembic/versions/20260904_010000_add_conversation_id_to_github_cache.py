"""Add conversation_id columns to github_issues, github_pull_requests.

Revision ID: 20260904_010000_add_conversation_id_to_github_cache
Revises: 20260904_000000_add_conversation_id_columns
Create Date: 2026-09-04

Second half of issue #1277 ("link GitHub events, issues, and PRs to
Conversation") — the first half (20260904_000000) added
``github_events.conversation_id``. This adds the same nullable FK to the
``github_issues`` / ``github_pull_requests`` local caches (see
``db/github_cache.py``) and backfills it from any task that already links to
that issue/PR:

  - ``github_issues.conversation_id``        <- ``tasks.conversation_id`` via
    (``tasks.issue_repo``, ``tasks.issue_number``) == (``github_issues.repo``, ``github_issues.number``)
  - ``github_pull_requests.conversation_id`` <- ``tasks.conversation_id`` via
    (``tasks.pr_repo``, ``tasks.pr_number``) == (``github_pull_requests.repo``, ``github_pull_requests.number``)

Same as the prior migration, this is additive only and best-effort: rows with
no resolvable task/conversation stay NULL.
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
            "UPDATE github_issues SET conversation_id = tasks.conversation_id "
            "FROM tasks WHERE tasks.issue_repo = github_issues.repo "
            "AND tasks.issue_number = github_issues.number "
            "AND tasks.conversation_id IS NOT NULL "
            "AND github_issues.conversation_id IS NULL"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE github_pull_requests SET conversation_id = tasks.conversation_id "
            "FROM tasks WHERE tasks.pr_repo = github_pull_requests.repo "
            "AND tasks.pr_number = github_pull_requests.number "
            "AND tasks.conversation_id IS NOT NULL "
            "AND github_pull_requests.conversation_id IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_github_pull_requests_conversation_id", table_name="github_pull_requests")
    op.drop_column("github_pull_requests", "conversation_id")

    op.drop_index("ix_github_issues_conversation_id", table_name="github_issues")
    op.drop_column("github_issues", "conversation_id")
