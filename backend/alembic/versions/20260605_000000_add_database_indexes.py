"""Add database indexes for query performance.

Revision ID: 20260605_000000_add_database_indexes
Revises: 20260604_000000_add_foreman_config_to_guilds
Create Date: 2026-06-05

Adds indexes on columns used in WHERE, ORDER BY, JOIN, and FK lookups that had
no explicit index. Covers the following high-frequency query patterns:

  tasks       – list by guild (filtered on deleted_at, ordered by created_at),
                orphaned-task sweeper (state == 'working'), FK from workers
  agents      – sweeper + listing by guild+state, FK from workers
  messages    – history fetch by guild ordered by created_at
  task_logs   – log retrieval by task_id (FK present but no index in SQLite/PG)
  workers     – listing by guild ordered by created_at
  guild_members – owner lookups by guild+role
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260605_000000_add_database_indexes"
down_revision: str | Sequence[str] | None = "20260604_000000_add_foreman_config_to_guilds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # tasks: most common list query filters on guild_id + live_tasks_filter()
    # (deleted_at IS NULL or > now) and orders by created_at DESC
    op.create_index(
        "ix_tasks_guild_id_deleted_at_created_at", "tasks", ["guild_id", "deleted_at", "created_at"]
    )

    # tasks: orphaned-task sweeper filters on state == 'working'
    op.create_index("ix_tasks_state", "tasks", ["state"])

    # tasks: FK lookup from workers (task list for a specific worker)
    op.create_index("ix_tasks_worker_id", "tasks", ["worker_id"])

    # agents: sweeper and guild listing filter on guild_id + state
    op.create_index("ix_agents_guild_id_state", "agents", ["guild_id", "state"])

    # agents: FK lookup from workers (agents belonging to a worker)
    op.create_index("ix_agents_worker_id", "agents", ["worker_id"])

    # messages: history fetch filters on guild_id and orders by created_at
    op.create_index("ix_messages_guild_id_created_at", "messages", ["guild_id", "created_at"])

    # task_logs: log retrieval filters on task_id; FK constraints do not
    # create an index automatically in SQLite or PostgreSQL
    op.create_index("ix_task_logs_task_id", "task_logs", ["task_id"])

    # workers: listing endpoint filters on guild_id and orders by created_at DESC
    op.create_index("ix_workers_guild_id_created_at", "workers", ["guild_id", "created_at"])

    # guild_members: owner-role lookup filters on guild_id + role
    op.create_index("ix_guild_members_guild_id_role", "guild_members", ["guild_id", "role"])


def downgrade() -> None:
    op.drop_index("ix_guild_members_guild_id_role", table_name="guild_members")
    op.drop_index("ix_workers_guild_id_created_at", table_name="workers")
    op.drop_index("ix_task_logs_task_id", table_name="task_logs")
    op.drop_index("ix_messages_guild_id_created_at", table_name="messages")
    op.drop_index("ix_agents_worker_id", table_name="agents")
    op.drop_index("ix_agents_guild_id_state", table_name="agents")
    op.drop_index("ix_tasks_worker_id", table_name="tasks")
    op.drop_index("ix_tasks_state", table_name="tasks")
    op.drop_index("ix_tasks_guild_id_deleted_at_created_at", table_name="tasks")
