"""Replace tasks.deleted_at (soft delete) with tasks.finished_at.

Same instant, new meaning: when the task left the active list. Existing values
are copied over, so history is kept.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261116_000000"
down_revision = "20261111_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE tasks SET finished_at = deleted_at")
    op.drop_index("ix_tasks_guild_id_deleted_at_created_at", table_name="tasks")
    op.drop_column("tasks", "deleted_at")
    op.create_index(
        "ix_tasks_guild_id_finished_at_created_at",
        "tasks",
        ["guild_id", "finished_at", "created_at"],
    )


def downgrade() -> None:
    op.add_column("tasks", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE tasks SET deleted_at = finished_at")
    op.drop_index("ix_tasks_guild_id_finished_at_created_at", table_name="tasks")
    op.drop_column("tasks", "finished_at")
    op.create_index(
        "ix_tasks_guild_id_deleted_at_created_at",
        "tasks",
        ["guild_id", "deleted_at", "created_at"],
    )
