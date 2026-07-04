"""merge_discord_threads_and_model_tier

Merge the two independent heads that both descend from
20260701_000003_add_issue_state_to_tasks:

  - 20260703_000000_add_discord_threads
    (discord_threads table for per-issue/PR thread persistence)
  - 20260703_000000_add_model_tier_to_tasks
    (model_tier column on tasks table)

These migrations touch completely separate tables/columns so the merge
has no ops.

Revision ID: 20260704_000000_merge_discord_threads_and_model_tier
Revises: 20260703_000000_add_discord_threads,
         20260703_000000_add_model_tier_to_tasks
Create Date: 2026-07-04 00:00:00.000000

"""

from collections.abc import Sequence

revision: str = "20260704_000000_merge_discord_threads_and_model_tier"
down_revision: str | Sequence[str] | None = (
    "20260703_000000_add_discord_threads",
    "20260703_000000_add_model_tier_to_tasks",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
