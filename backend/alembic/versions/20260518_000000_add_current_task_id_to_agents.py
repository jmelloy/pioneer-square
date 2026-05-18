"""add_current_task_id_to_agents

Adds a ``current_task_id`` column to the ``agents`` table. Workers report
the task each slot is executing via ``agent-state`` messages; persisting
it lets the frontend map a task row to its agent immediately on initial
REST fetch instead of having to wait for the next WS update.

Revision ID: 20260518_000000_add_current_task_id_to_agents
Revises: 20260515_000001_add_tokens_to_foreman_turns
Create Date: 2026-05-18 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260518_000000_add_current_task_id_to_agents"
down_revision: str | Sequence[str] | None = "20260515_000001_add_tokens_to_foreman_turns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(sa.Column("current_task_id", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_column("current_task_id")
