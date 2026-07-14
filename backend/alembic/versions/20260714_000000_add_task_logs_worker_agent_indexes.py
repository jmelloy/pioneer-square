"""Add task_logs indexes on worker_id and agent_id.

Revision ID: 20260714_000000_add_task_logs_worker_agent_indexes
Revises: 20260712_000000_add_claude_session_id_to_tasks
Create Date: 2026-07-14

The worker- and agent-scoped log-fetch queries in routes/tasks.py filter on
task_logs.worker_id (WHERE worker_id = ? AND task_id IS NULL) and
task_logs.agent_id (WHERE agent_id = ?). Neither column was indexed, so both
did a full sequential scan of task_logs (~500k rows and growing). Only the
task_id lookup had an index. These add the two missing indexes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260714_000000_add_task_logs_worker_agent_indexes"
down_revision: str | Sequence[str] | None = "20260712_000000_add_claude_session_id_to_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # task_logs: worker-scoped log fetch (worker_id = ? AND task_id IS NULL)
    op.create_index("ix_task_logs_worker_id", "task_logs", ["worker_id"])

    # task_logs: agent-scoped log fetch (agent_id = ?)
    op.create_index("ix_task_logs_agent_id", "task_logs", ["agent_id"])

    # foreman_turns: history fetch filters on (guild_id, user_id) and orders by
    # id DESC. Without this it seq-scans the whole table (~90k rows) then sorts;
    # the trailing id column lets it use a backward index scan and skip the sort.
    op.create_index(
        "ix_foreman_turns_guild_id_user_id_id", "foreman_turns", ["guild_id", "user_id", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_foreman_turns_guild_id_user_id_id", table_name="foreman_turns")
    op.drop_index("ix_task_logs_agent_id", table_name="task_logs")
    op.drop_index("ix_task_logs_worker_id", table_name="task_logs")
