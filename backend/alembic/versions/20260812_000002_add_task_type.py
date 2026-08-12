"""add task_type to tasks

Revision ID: 20260812_000002
Revises: 20260812_000001_repair_api_request_log_guild_user
Create Date: 2026-08-12 00:00:02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260812_000002"
down_revision = "20260812_000001_repair_api_request_log_guild_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("tasks")}
    if "task_type" not in cols:
        op.add_column(
            "tasks",
            sa.Column("task_type", sa.String(), nullable=False, server_default="standard"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("tasks")}
    if "task_type" in cols:
        op.drop_column("tasks", "task_type")
