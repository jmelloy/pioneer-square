"""merge_locking_and_messages

Merge the two branches that both revise 20260520_000001_add_task_locking:
  - 20260520_000002_add_locks_table
  - 20260520_000002_add_role_and_meta_to_messages

Revision ID: 20260520_000003_merge_locking_and_messages
Revises: 20260520_000002_add_locks_table, 20260520_000002_add_role_and_meta_to_messages
Create Date: 2026-05-20 00:00:03.000000

"""

from collections.abc import Sequence

revision: str = "20260520_000003_merge_locking_and_messages"
down_revision: str | Sequence[str] | None = (
    "20260520_000002_add_locks_table",
    "20260520_000002_add_role_and_meta_to_messages",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
