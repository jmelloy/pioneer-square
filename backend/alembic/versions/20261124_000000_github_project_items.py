"""Add github_project_items: cached issue membership of linked GitHub Projects (#1513)."""

import sqlalchemy as sa
from alembic import op

revision = "20261124_000000"
down_revision = "20261118_000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "github_project_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "uq_github_project_items_project_repo_number",
        "github_project_items",
        ["project_id", "repo", "number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_github_project_items_project_repo_number", "github_project_items")
    op.drop_table("github_project_items")
