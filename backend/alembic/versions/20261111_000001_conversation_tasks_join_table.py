"""Make Conversation <-> Task many-to-many via conversation_tasks (#1431).

Backfills one link per task from tasks.conversation_id, then drops that column.
Downgrade restores tasks.conversation_id from each task's earliest link.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261111_000001"
down_revision = "20261111_000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_tasks",
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id", sa.String(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("conversation_id", "task_id", name="pk_conversation_tasks"),
    )
    op.create_index("ix_conversation_tasks_task_id", "conversation_tasks", ["task_id"])
    op.execute(
        "INSERT INTO conversation_tasks (conversation_id, task_id, created_at) "
        "SELECT conversation_id, id, created_at FROM tasks WHERE conversation_id IS NOT NULL"
    )
    op.drop_index("ix_tasks_conversation_id", table_name="tasks")
    op.drop_column("tasks", "conversation_id")


def downgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=True
        ),
    )
    op.create_index("ix_tasks_conversation_id", "tasks", ["conversation_id"])
    op.execute(
        "UPDATE tasks SET conversation_id = ct.conversation_id FROM ("
        " SELECT DISTINCT ON (task_id) task_id, conversation_id FROM conversation_tasks"
        " ORDER BY task_id, created_at, conversation_id"
        ") ct WHERE ct.task_id = tasks.id"
    )
    op.drop_index("ix_conversation_tasks_task_id", table_name="conversation_tasks")
    op.drop_table("conversation_tasks")
