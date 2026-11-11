"""Add artifacts table for S3-stored screenshots/images."""

import sqlalchemy as sa
from alembic import op

revision = "20261110_000001"
down_revision = "20261110_000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("guild_id", sa.Integer(), sa.ForeignKey("guilds.id"), nullable=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("uploaded_by_worker_id", sa.String(), sa.ForeignKey("workers.id"), nullable=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_artifacts_guild_id", "artifacts", ["guild_id"])
    op.create_index("ix_artifacts_task_id", "artifacts", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_task_id", table_name="artifacts")
    op.drop_index("ix_artifacts_guild_id", table_name="artifacts")
    op.drop_table("artifacts")
