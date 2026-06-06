"""Add index on tasks.parent_task_id for hierarchy lookups.

Revision ID: 20260606_000000_index_tasks_parent_task_id
Revises: 20260605_000003_refactor_foreman_turns
Create Date: 2026-06-06

tasks.parent_task_id is used to link review/sub-tasks back to their parent
work item. Querying children by parent (e.g. to show the sidebar hierarchy)
is an unindexed scan without this index.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260606_000000_index_tasks_parent_task_id"
down_revision: str | Sequence[str] | None = "20260605_000003_refactor_foreman_turns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_parent_task_id", table_name="tasks")
