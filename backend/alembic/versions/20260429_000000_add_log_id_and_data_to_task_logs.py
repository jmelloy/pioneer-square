"""add_log_id_and_data_to_task_logs

Revision ID: c3f8e2a91b45
Revises: da1a6b1632f1
Create Date: 2026-04-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3f8e2a91b45'
down_revision: Union[str, Sequence[str], None] = 'da1a6b1632f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('task_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('log_id', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('data', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('task_logs', schema=None) as batch_op:
        batch_op.drop_column('data')
        batch_op.drop_column('log_id')
