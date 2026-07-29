"""Add unified spawn_settings table and backfill from the three legacy sources.

Revision ID: 20260728_000002_add_spawn_settings
Revises: 20260728_000000_add_last_refreshed_at_to_github_cache
Create Date: 2026-07-28

Additive step of the spawn-settings unification. Creates spawn_settings (guild
baseline row = user_id NULL; user override = user_id set) and backfills it from:
  - guild_spawn_defaults      -> baseline repos/tools/agent_count
  - guilds.foreman_config     -> baseline env_vars (forward=true only),
                                 tool_env_vars, provider/model (pi_default_*)
  - user_spawn_settings       -> per-user override repos/tools/env_vars

The legacy tables and the foreman_config worker-slice are left in place; a later
migration drops them once all readers are cut over (issue: unify-spawn-settings).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "20260728_000002_add_spawn_settings"
down_revision: str | Sequence[str] | None = "20260728_000000_add_last_refreshed_at_to_github_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _spawn_table() -> sa.Table:
    md = sa.MetaData()
    return sa.Table(
        "spawn_settings",
        md,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.Integer, nullable=False),
        sa.Column("user_id", sa.Text, nullable=True),
        sa.Column("repos", sa.Text, nullable=False),
        sa.Column("tools", sa.Text, nullable=False),
        sa.Column("agent_count", sa.Integer, nullable=True),
        sa.Column("env_vars", sa.JSON, nullable=False),
        sa.Column("tool_env_vars", sa.JSON, nullable=False),
        sa.Column("provider", sa.Text, nullable=True),
        sa.Column("model", sa.Text, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "spawn_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.Integer(), sa.ForeignKey("guilds.id"), nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("repos", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("tools", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("agent_count", sa.Integer(), nullable=True),
        sa.Column("env_vars", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("tool_env_vars", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_spawn_settings_guild_baseline",
        "spawn_settings",
        ["guild_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.create_index(
        "uq_spawn_settings_guild_user",
        "spawn_settings",
        ["guild_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )

    conn = op.get_bind()
    now = datetime.now(UTC)
    spawn = _spawn_table()
    rows: list[dict] = []

    # --- guild baseline rows: guild_spawn_defaults + foreman_config worker-slice ---
    gsd = {
        r.guild_id: r
        for r in conn.execute(
            sa.text("SELECT guild_id, repos, tools, agent_count FROM guild_spawn_defaults")
        )
    }
    guilds = conn.execute(sa.text("SELECT id, foreman_config FROM guilds")).fetchall()
    for g in guilds:
        cfg = g.foreman_config or {}
        if isinstance(cfg, str):
            cfg = json.loads(cfg or "{}")
        env_vars = {
            e["key"]: e["value"]
            for e in (cfg.get("env_vars") or [])
            if e.get("forward") and e.get("key") and e.get("value") is not None
        }
        tool_env_vars = {
            tool: {i["key"]: i["value"] for i in (items or []) if i.get("key") and i.get("value") is not None}
            for tool, items in (cfg.get("tool_env_vars") or {}).items()
        }
        tool_env_vars = {t: kv for t, kv in tool_env_vars.items() if kv}
        d = gsd.get(g.id)
        # Skip guilds with nothing to migrate — resolve() treats a missing
        # baseline the same as an empty one.
        if d is None and not env_vars and not tool_env_vars and not cfg.get("pi_default_provider"):
            continue
        rows.append(
            {
                "guild_id": g.id,
                "user_id": None,
                "repos": d.repos if d else "[]",
                "tools": d.tools if d else "[]",
                "agent_count": d.agent_count if d else None,
                "env_vars": env_vars,
                "tool_env_vars": tool_env_vars,
                "provider": cfg.get("pi_default_provider"),
                "model": cfg.get("pi_default_model"),
                "updated_at": now,
            }
        )

    # --- user override rows: user_spawn_settings ---
    uss = conn.execute(
        sa.text("SELECT guild_id, user_id, settings_json FROM user_spawn_settings")
    ).fetchall()
    for u in uss:
        try:
            s = json.loads(u.settings_json or "{}")
        except (ValueError, TypeError):
            s = {}
        env_vars = {
            e["key"]: e["value"]
            for e in (s.get("envVars") or [])
            if e.get("key") and e.get("value") is not None
        }
        rows.append(
            {
                "guild_id": u.guild_id,
                "user_id": u.user_id,
                "repos": json.dumps(s.get("repos") or []),
                "tools": json.dumps(s.get("tools") or []),
                "agent_count": s.get("agent_count"),
                "env_vars": env_vars,
                "tool_env_vars": {},
                "provider": None,
                "model": None,
                "updated_at": now,
            }
        )

    if rows:
        op.bulk_insert(spawn, rows)


def downgrade() -> None:
    op.drop_index("uq_spawn_settings_guild_user", table_name="spawn_settings")
    op.drop_index("uq_spawn_settings_guild_baseline", table_name="spawn_settings")
    op.drop_table("spawn_settings")
