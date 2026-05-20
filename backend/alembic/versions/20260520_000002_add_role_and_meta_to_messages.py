"""add_role_and_meta_to_messages

Persist tool_use / tool_result chat blocks so they survive a page refresh.
The `role` column mirrors the WebSocket message role field; `meta` stores
JSON with extra fields (toolId, toolName, toolInput, isError).

Revision ID: 20260520_000002_add_role_and_meta_to_messages
Revises: 20260520_000001_add_task_locking
Create Date: 2026-05-20 00:02:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_000002_add_role_and_meta_to_messages"
down_revision: str | Sequence[str] | None = "20260520_000001_add_task_locking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.add_column(sa.Column("role", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("meta", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_column("meta")
        batch_op.drop_column("role")
