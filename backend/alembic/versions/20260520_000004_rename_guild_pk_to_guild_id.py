"""rename_guild_pk_to_guild_id

Rename the ``guild_pk`` FK column to ``guild_id`` in all child tables.

``guild_pk`` was a transitional name introduced in migration
20260512_000001 to distinguish the new integer FK column from the legacy
TEXT ``guild_id`` column that was subsequently dropped in 20260512_000002.
Now that the schema is fully migrated, the conventional FK name
``guild_id`` (pointing at ``guilds.id``) is used consistently everywhere.

Revision ID: 20260520_000004_rename_guild_pk_to_guild_id
Revises: 20260520_000003_merge_locking_and_messages
Create Date: 2026-05-20 00:00:04.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_000004_rename_guild_pk_to_guild_id"
down_revision: str | Sequence[str] | None = "20260520_000003_merge_locking_and_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = [
    "agents",
    "messages",
    "workers",
    "tasks",
    "guild_members",
    "claude_credentials",
    "guild_keys",
    "foreman_turns",
    "github_events",
]


def upgrade() -> None:
    for table in _TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} RENAME COLUMN guild_pk TO guild_id"))


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(sa.text(f"ALTER TABLE {table} RENAME COLUMN guild_id TO guild_pk"))
