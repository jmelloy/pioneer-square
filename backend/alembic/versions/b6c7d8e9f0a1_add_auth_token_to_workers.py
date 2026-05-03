"""add_auth_token_to_workers

Adds an ``auth_token`` column to the ``workers`` table. Workers receive a
fresh token at registration time and present it as a Bearer credential when
fetching guild secrets (Claude credentials, GitHub OAuth tokens) so those
endpoints stop being unauthenticated.

Existing rows are left with NULL — they predate the auth requirement, and
workers re-register on every process startup, so a NULL token simply means
that worker hasn't reconnected yet under the new scheme.

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-05-03 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, Sequence[str], None] = "a5b6c7d8e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.add_column(sa.Column("auth_token", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.drop_column("auth_token")
