"""add_name_to_workers

Adds a ``name`` column to the ``workers`` table that stores the human-readable
label ``hostname[:3]/worker_id`` (e.g. ``tok/w-g2otus``).  Computed at
registration time from the hostname the worker process reports; NULL for
pre-existing rows (the API falls back to ``worker_id`` for those).

Revision ID: 20260515_000000_add_name_to_workers
Revises: 20260512_000002_drop_guild_id_child_tables
Create Date: 2026-05-15 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260515_000000_add_name_to_workers"
down_revision: str | Sequence[str] | None = "20260512_000002_drop_guild_id_child_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.add_column(sa.Column("name", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.drop_column("name")
