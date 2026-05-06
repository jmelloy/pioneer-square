"""add_org_to_workers

Adds an ``org`` column to the ``workers`` table so a worker can be configured
with a GitHub org name instead of (or alongside) an explicit ``repos`` list.
When set, the worker is eligible for any task targeting ``<org>/*`` and clones
repos lazily on first use.

Existing rows are left with NULL (behaves as before: explicit repos list only).

Revision ID: 20260506_000002_add_org_to_workers
Revises: 20260506_000001_add_custom_jwks_to_guild_keys
Create Date: 2026-05-06 00:02:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260506_000002_add_org_to_workers"
down_revision: str | Sequence[str] | None = "20260506_000001_add_custom_jwks_to_guild_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.add_column(sa.Column("org", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.drop_column("org")
