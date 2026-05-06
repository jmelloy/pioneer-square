"""add_primary_repo_to_guilds

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('guilds', sa.Column('primary_repo', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('guilds', schema=None) as batch_op:
        batch_op.drop_column('primary_repo')
