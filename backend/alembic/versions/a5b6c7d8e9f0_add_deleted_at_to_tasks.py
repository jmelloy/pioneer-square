"""add_deleted_at_to_tasks

Adds a ``deleted_at`` column to the ``tasks`` table for soft-delete semantics.
The column stores an ISO-8601 UTC timestamp (matching every other timestamp in
the schema). When NULL the task is "live"; when set, the task is considered
soft-deleted as soon as the wall clock passes that instant.

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-05-02 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a5b6c7d8e9f0"
down_revision: Union[str, Sequence[str], None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("deleted_at", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("deleted_at")
