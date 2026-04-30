"""add_user_id_to_messages

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-04-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e3f4a5b6c7d8'
down_revision: Union[str, Sequence[str], None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('messages', sa.Column('user_id', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('messages', 'user_id')
