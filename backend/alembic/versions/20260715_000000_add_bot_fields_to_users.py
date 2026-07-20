"""add_bot_fields_to_users

Adds ``is_bot`` and ``parent_user_id`` to ``users`` so a Discord bot account
can be auto-provisioned its own row (for audit trails / message attribution)
while its guild permissions and ownership are traced back to a real human via
``parent_user_id`` (self-referential FK). See ``discord/bot_users.py``.

Revision ID: 20260715_000000_add_bot_fields_to_users
Revises: 20260714_000000_add_task_logs_worker_agent_indexes
Create Date: 2026-07-15

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260715_000000_add_bot_fields_to_users"
down_revision: str | Sequence[str] | None = "20260714_000000_add_task_logs_worker_agent_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_bot", sa.Boolean(), nullable=False, server_default=sa.text("false")
            )
        )
        batch_op.add_column(sa.Column("parent_user_id", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "fk_users_parent_user_id_users", "users", ["parent_user_id"], ["id"]
        )

    op.create_index("ix_users_parent_user_id", "users", ["parent_user_id"])


def downgrade() -> None:
    op.drop_index("ix_users_parent_user_id", table_name="users")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("fk_users_parent_user_id_users", type_="foreignkey")
        batch_op.drop_column("parent_user_id")
        batch_op.drop_column("is_bot")
