"""Add guilds.browse_repos: shared Projects-page browsing repository list."""

import sqlalchemy as sa
from alembic import op

revision = "20261118_000000"
down_revision = "20261116_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("guilds", sa.Column("browse_repos", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("guilds", "browse_repos")
