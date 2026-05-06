"""add_user_id_to_tasks

Records who initiated each task so worker-driven foreman events
(task-complete, task-followup-done, needs-input, claude-auth-required) can
route the resulting Claude conversation to the right user thread instead of
defaulting to the guild owner. Without this column, multi-user guilds leak
worker-event context across users.

NULL on legacy rows; the foreman falls back to the guild owner for those.

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-05-03 00:00:01.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, Sequence[str], None] = "b6c7d8e9f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("user_id")
