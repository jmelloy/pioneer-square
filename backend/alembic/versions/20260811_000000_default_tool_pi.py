"""Default new tasks/llm_usage/spawn_profiles rows to tool='pi' instead of 'claude'.

Pi is now the primary coding agent; ORM-level defaults in models.py were
updated to match (Task.tool, LlmUsage.tool, SpawnProfile.tool). This
migration brings the DB-level ``server_default`` in line so raw-SQL inserts
that omit ``tool`` get the same behavior. Existing rows are left untouched.

Revision ID: 20260811_000000_default_tool_pi
Revises: 20260803_000000_add_spawn_profiles
Create Date: 2026-08-11 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_000000_default_tool_pi"
down_revision: str | Sequence[str] | None = "20260803_000000_add_spawn_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.alter_column("tool", server_default=sa.text("'pi'"))
    with op.batch_alter_table("llm_usage") as batch_op:
        batch_op.alter_column("tool", server_default=sa.text("'pi'"))
    with op.batch_alter_table("spawn_profiles") as batch_op:
        batch_op.alter_column("tool", server_default=sa.text("'pi'"))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.alter_column("tool", server_default=sa.text("'claude'"))
    with op.batch_alter_table("llm_usage") as batch_op:
        batch_op.alter_column("tool", server_default=sa.text("'claude'"))
    with op.batch_alter_table("spawn_profiles") as batch_op:
        batch_op.alter_column("tool", server_default=sa.text("'claude'"))
