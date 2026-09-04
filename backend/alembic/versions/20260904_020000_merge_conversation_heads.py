"""Merge conversation_id migration heads.

Revision ID: 20260904_020000_merge_conversation_heads
Revises: 20260904_000001_add_foreman_turns_conversation_id_index, 20260904_010000_add_conversation_id_to_github_cache
Create Date: 2026-09-04

Issues #1279 and #1277 each branched off
``20260904_000000_add_conversation_id_columns`` independently, leaving two
heads. No-op merge to bring the chain back to one head.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "20260904_020000_merge_conversation_heads"
down_revision: str | Sequence[str] | None = (
    "20260904_000001_add_foreman_turns_conversation_id_index",
    "20260904_010000_add_conversation_id_to_github_cache",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
