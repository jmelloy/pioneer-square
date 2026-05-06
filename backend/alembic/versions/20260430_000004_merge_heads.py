"""merge_heads

Revision ID: a1b2c3d4e5f6
Revises: e3f4a5b6c7d8, f3a9c1e7b250
Create Date: 2026-04-30 00:00:00.000000

"""
from typing import Sequence, Union

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = ('e3f4a5b6c7d8', 'f3a9c1e7b250')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
