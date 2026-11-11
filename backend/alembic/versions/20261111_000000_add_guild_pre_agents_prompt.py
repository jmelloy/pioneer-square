"""Add guilds.pre_agents_prompt and pre_agents_prompt_updated_at."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_000000"
down_revision = "20261110_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("guilds", sa.Column("pre_agents_prompt", sa.Text(), nullable=True))
    op.add_column(
        "guilds",
        sa.Column("pre_agents_prompt_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("guilds", "pre_agents_prompt_updated_at")
    op.drop_column("guilds", "pre_agents_prompt")
