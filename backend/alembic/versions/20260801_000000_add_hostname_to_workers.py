"""add_hostname_to_workers

Adds a ``hostname`` column to the ``workers`` table, capturing the machine
or container hostname where each worker process runs. Populated at HTTP
self-registration and refreshed on every worker-register WS message; NULL
on legacy rows.

Revision ID: 20260801_000000_add_hostname_to_workers
Revises: 20260801_000000_add_excluded_env_keys_to_workers
Create Date: 2026-08-01 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260801_000000_add_hostname_to_workers"
down_revision: str | Sequence[str] | None = "20260801_000000_add_excluded_env_keys_to_workers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.add_column(sa.Column("hostname", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.drop_column("hostname")
