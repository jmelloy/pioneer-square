"""add_index_worker_last_seen

Add a composite index on workers(state, last_seen) to avoid a full table
scan on every zombie-worker sweep (which filters WHERE state != 'offline'
AND last_seen IS NOT NULL AND last_seen < cutoff).

Revision ID: 20260520_000000_add_index_worker_last_seen
Revises: 20260518_000000_add_current_task_id_to_agents
Create Date: 2026-05-20 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260520_000000_add_index_worker_last_seen"
down_revision: str | Sequence[str] | None = "20260518_000000_add_current_task_id_to_agents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.create_index("ix_workers_state_last_seen", ["state", "last_seen"])


def downgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.drop_index("ix_workers_state_last_seen")
