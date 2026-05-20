"""merge_nullable_worker_and_locks_fix

Merge the two independent heads:
  - 20260520_000004_nullable_task_worker_id  (tasks.worker_id → nullable)
  - 20260520_000005_fix_locks_timestamp_types_and_index_predicate  (locks table fix)

These migrations touch completely separate tables so the merge has no ops.

Revision ID: 20260520_000006_merge_nullable_worker_and_locks_fix
Revises: 20260520_000004_nullable_task_worker_id, 20260520_000005_fix_locks_timestamp_types_and_index_predicate
Create Date: 2026-05-20 00:00:06.000000

"""

from collections.abc import Sequence

revision: str = "20260520_000006_merge_nullable_worker_and_locks_fix"
down_revision: str | Sequence[str] | None = (
    "20260520_000004_nullable_task_worker_id",
    "20260520_000005_fix_locks_timestamp_types_and_index_predicate",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
