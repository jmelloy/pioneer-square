"""Add artifacts.type (image | html | text | pdf)."""

import sqlalchemy as sa
from alembic import op

revision = "20261118_000002"
down_revision = "20261118_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "artifacts", sa.Column("type", sa.Text(), nullable=False, server_default="image")
    )


def downgrade() -> None:
    op.drop_column("artifacts", "type")
