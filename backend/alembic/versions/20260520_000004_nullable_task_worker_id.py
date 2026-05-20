"""nullable_task_worker_id

Make tasks.worker_id nullable (FK constraint retained).
Foreman-owned tasks previously used the sentinel string 'foreman' (which
has no matching row in the workers table and therefore violates the FK
constraint on PostgreSQL).  NULL is the correct representation for
"no real worker assigned" — PostgreSQL allows NULL on FK columns.

Backfills existing 'foreman' rows to NULL on upgrade.

Revision ID: 20260520_000004_nullable_task_worker_id
Revises: 20260520_000003_merge_locking_and_messages
Create Date: 2026-05-20 00:00:04.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_000004_nullable_task_worker_id"
down_revision: str | Sequence[str] | None = "20260520_000003_merge_locking_and_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop NOT NULL — foreman tasks have no real worker, FK constraint stays.
    op.alter_column("tasks", "worker_id", existing_type=sa.Text(), nullable=True)

    # Backfill: the 'foreman' sentinel → NULL (satisfies the FK; NULL is always valid).
    op.execute(sa.text("UPDATE tasks SET worker_id = NULL WHERE worker_id = 'foreman'"))


def downgrade() -> None:
    # Restore NOT NULL.  NULL rows become 'foreman' (best-effort; the old
    # 'foreman' string still violates the FK, but that was the pre-migration state).
    op.execute(sa.text("UPDATE tasks SET worker_id = 'foreman' WHERE worker_id IS NULL"))
    op.alter_column("tasks", "worker_id", existing_type=sa.Text(), nullable=False)
