"""Add issue_state and issue_title columns to tasks table.

Revision ID: 20260701_000003_add_issue_state_to_tasks
Revises: 20260701_000002_add_bedrock_model_id_to_catalog
Create Date: 2026-07-01

Caches the GitHub issue state ("open" or "closed") and title on each task row
so the tasks/tree endpoint can return the real state/title without a GitHub API
call when the guild owner's token is unavailable or the API is unreachable.
NULL means the value has not been fetched yet; the tree endpoint writes them
back after a successful GitHub API response, and the webhook handler keeps them
current when issue opened/closed/reopened/edited events arrive.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260701_000003_add_issue_state_to_tasks"
down_revision: str | Sequence[str] | None = "20260701_000002_add_bedrock_model_id_to_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("issue_state", sa.Text(), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("issue_title", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "issue_title")
    op.drop_column("tasks", "issue_state")
