"""add_claude_credentials

Revision ID: b1e2f3a4c5d6
Revises: da1a6b1632f1
Create Date: 2026-04-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1e2f3a4c5d6'
down_revision: Union[str, Sequence[str], None] = 'da1a6b1632f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'claude_credentials',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('guild_id', sa.Text(), sa.ForeignKey('guilds.id'), nullable=False, unique=True),
        sa.Column('credentials_blob', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('claude_credentials')
