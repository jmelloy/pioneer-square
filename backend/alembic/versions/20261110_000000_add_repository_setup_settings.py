"""Add repository_setup_settings for UI-managed per-repo setup commands."""

import sqlalchemy as sa
from alembic import op

revision = "20261110_000000"
down_revision = "20261028_000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repository_setup_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("guild_id", sa.Integer(), sa.ForeignKey("guilds.id"), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("setup_commands", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), server_default="600", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_repository_setup_settings_guild_repo",
        "repository_setup_settings",
        ["guild_id", "repo"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_repository_setup_settings_guild_repo", table_name="repository_setup_settings")
    op.drop_table("repository_setup_settings")
