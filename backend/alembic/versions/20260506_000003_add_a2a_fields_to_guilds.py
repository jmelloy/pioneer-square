"""add_a2a_fields_to_guilds

Adds ``description``, ``url``, and ``version`` columns to the ``guilds`` table
so each guild can populate its A2A AgentCard (served at
``/.well-known/agent.json``). All columns are nullable; existing rows keep NULL
(the endpoint falls back to sensible defaults).

Revision ID: 20260506_000003_add_a2a_fields_to_guilds
Revises: 20260506_000002_add_org_to_workers
Create Date: 2026-05-06 00:03:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260506_000003_add_a2a_fields_to_guilds"
down_revision: str | Sequence[str] | None = "20260506_000002_add_org_to_workers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("guilds") as batch_op:
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("url", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("version", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("guilds") as batch_op:
        batch_op.drop_column("version")
        batch_op.drop_column("url")
        batch_op.drop_column("description")
