"""merge_rename_with_prior_head

Merge the two independent heads that both descend from
20260520_000003_merge_locking_and_messages:

  - 20260520_000006_merge_nullable_worker_and_locks_fix
    (nullable worker_id + locks-table fixes)
  - 20260520_000004_rename_guild_pk_to_guild_id
    (FK column rename across all child tables)

These migrations touch completely separate columns/tables so the merge
has no ops.

Revision ID: 20260520_000007_merge_rename_with_prior_head
Revises: 20260520_000006_merge_nullable_worker_and_locks_fix,
         20260520_000004_rename_guild_pk_to_guild_id
Create Date: 2026-05-20 00:00:07.000000

"""

from collections.abc import Sequence

revision: str = "20260520_000007_merge_rename_with_prior_head"
down_revision: str | Sequence[str] | None = (
    "20260520_000006_merge_nullable_worker_and_locks_fix",
    "20260520_000004_rename_guild_pk_to_guild_id",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
