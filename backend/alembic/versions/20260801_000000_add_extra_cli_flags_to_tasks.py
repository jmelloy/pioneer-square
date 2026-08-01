"""Add extra_cli_flags column to tasks.

Revision ID: 20260801_000000_add_extra_cli_flags_to_tasks
Revises: 20260729_000001_drop_foreman_turns_usage_columns
Create Date: 2026-08-01

Part of per-task foreman-chosen extra CLI flags for worker tools (#1036).
Stores a JSON-encoded list of extra argv flags (e.g. ["--thinking", "high"]
for pi, ["-c", "key=value"] for codex) the foreman attaches to a task via
assign_task/send_followup, forwarded verbatim to the worker-tool invocation.
NULL on legacy tasks and on any task the foreman didn't attach flags to.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260801_000000_add_extra_cli_flags_to_tasks"
down_revision: str | Sequence[str] | None = "20260729_000001_drop_foreman_turns_usage_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("extra_cli_flags", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "extra_cli_flags")
