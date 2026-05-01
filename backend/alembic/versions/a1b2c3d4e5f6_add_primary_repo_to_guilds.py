"""add_primary_repo_to_guilds

Revision ID: a1b2c3d4e5f6
Revises: b1e2f3a4c5d6
Create Date: 2026-04-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'e3f4a5b6c7d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('guilds', sa.Column('primary_repo', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('guilds') as batch_op:
        batch_op.drop_column('primary_repo')
