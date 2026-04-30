"""add_foreman_turns

Revision ID: d2e3f4a5b6c7
Revises: b1e2f3a4c5d6
Create Date: 2026-04-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2e3f4a5b6c7'
down_revision: Union[str, Sequence[str], None] = 'b1e2f3a4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'foreman_turns',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('guild_id', sa.Text(), nullable=False),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('role', sa.Text(), nullable=False),
        sa.Column('content_json', sa.Text(), nullable=False),
        sa.Column('is_tool_response', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('parent_id', sa.Integer(), sa.ForeignKey('foreman_turns.id'), nullable=True),
        sa.Column('created_at', sa.Text(), nullable=False),
    )
    op.create_index('ix_foreman_turns_guild_user', 'foreman_turns', ['guild_id', 'user_id'])


def downgrade() -> None:
    op.drop_index('ix_foreman_turns_guild_user', table_name='foreman_turns')
    op.drop_table('foreman_turns')
