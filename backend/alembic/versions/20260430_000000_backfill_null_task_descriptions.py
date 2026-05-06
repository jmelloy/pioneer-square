"""backfill_null_task_descriptions

Revision ID: f3a9c1e7b250
Revises: c3f8e2a91b45
Create Date: 2026-04-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'f3a9c1e7b250'
down_revision: Union[str, Sequence[str], None] = 'c3f8e2a91b45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE tasks SET description = '' WHERE description IS NULL")


def downgrade() -> None:
    pass
