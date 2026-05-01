"""add_activity_to_agents

Revision ID: a1b2c3d4e5f6
Revises: f3a9c1e7b250
Create Date: 2026-05-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'f3a9c1e7b250'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('agents') as batch_op:
        batch_op.add_column(sa.Column('activity', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('agents') as batch_op:
        batch_op.drop_column('activity')
