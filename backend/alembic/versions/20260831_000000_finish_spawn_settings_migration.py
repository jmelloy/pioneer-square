"""Finish the spawn-settings migration: one store for worker-facing config.

Revision ID: 20260831_000000
Revises: 20260821_000000
Create Date: 2026-08-31

20260728_000002 created spawn_settings and backfilled it, but left the
worker-facing slice of ``guilds.foreman_config`` in place; a lazy migration in
the foreman-config GET/PATCH handlers finished the move per-guild, on first
read. This migration does that move for every guild at once so the lazy path
can be deleted (issue #1240).

Moves out of ``foreman_config`` and into the guild's baseline spawn_settings row:
  - ``env_vars`` entries with ``forward: true``  -> ``env_vars``
  - ``tool_env_vars``                            -> ``tool_env_vars``
  - ``pi_default_provider`` / ``pi_default_model``  -> ``tool_defaults['pi']``
  - ``codex_default_model``                      -> ``tool_defaults['codex']``

Also folds each row's legacy top-level ``provider``/``model`` (where
20260728_000002 parked the Pi defaults) into ``tool_defaults['pi']``, which is
the only place the tool-default resolver reads them from.

``foreman_config`` keeps only the foreman's own orchestrator-LLM fields.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_000000"
down_revision: str | Sequence[str] | None = "20260821_000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Worker-facing keys that must not survive in foreman_config.
_WORKER_KEYS = ("tool_env_vars", "pi_default_provider", "pi_default_model", "codex_default_model")


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
        sa.Column("tool_defaults", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def _guilds_table() -> sa.Table:
    md = sa.MetaData()
    return sa.Table(
        "guilds",
        md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("foreman_config", sa.JSON, nullable=True),
    )


def _as_dict(value) -> dict:
    """foreman_config comes back as a dict on Postgres and a str on SQLite."""
    if isinstance(value, str):
        try:
            return json.loads(value or "{}")
        except (ValueError, TypeError):
            return {}
    return dict(value or {})


def _as_json(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value or "null") or default
        except (ValueError, TypeError):
            return default
    return value or default


def _pairs_to_map(pairs) -> dict[str, str]:
    """Accept both wire shapes: a [{key, value}] list or an already-flat map."""
    if isinstance(pairs, dict):
        return {k: v or "" for k, v in pairs.items() if k}
    return {
        p["key"]: p.get("value") or ""
        for p in (pairs or [])
        if isinstance(p, dict) and p.get("key")
    }


def upgrade() -> None:
    conn = op.get_bind()
    now = datetime.now(UTC)
    spawn, guilds = _spawn_table(), _guilds_table()

    baselines = {
        r.guild_id: r for r in conn.execute(sa.select(spawn).where(spawn.c.user_id.is_(None)))
    }

    for g in conn.execute(sa.select(guilds)).fetchall():
        cfg = _as_dict(g.foreman_config)
        row = baselines.get(g.id)

        worker_env: dict[str, str] = {}
        foreman_env: list[dict] = []
        for item in cfg.get("env_vars") or []:
            if not isinstance(item, dict) or not item.get("key"):
                continue
            if item.get("forward"):
                worker_env[item["key"]] = item.get("value") or ""
            else:
                foreman_env.append({"key": item["key"], "value": item.get("value") or ""})

        tool_env = {
            tool: _pairs_to_map(pairs) for tool, pairs in (cfg.get("tool_env_vars") or {}).items()
        }
        tool_env = {t: kv for t, kv in tool_env.items() if kv}

        tool_defaults: dict[str, dict[str, str]] = {}
        # The row's own top-level provider/model held the Pi defaults after
        # 20260728_000002; tool_defaults['pi'] is where the resolver looks now.
        pi = {
            k: v
            for k, v in (
                ("provider", cfg.get("pi_default_provider") or (row.provider if row else None)),
                ("model", cfg.get("pi_default_model") or (row.model if row else None)),
            )
            if v
        }
        if pi:
            tool_defaults["pi"] = pi
        if cfg.get("codex_default_model"):
            tool_defaults["codex"] = {"model": cfg["codex_default_model"]}

        cleaned = {k: v for k, v in cfg.items() if k not in _WORKER_KEYS}
        if foreman_env:
            cleaned["env_vars"] = foreman_env
        else:
            cleaned.pop("env_vars", None)

        has_spawn_data = bool(worker_env or tool_env or tool_defaults)
        if not has_spawn_data and cleaned == cfg:
            continue

        if has_spawn_data:
            merged_env = dict(_as_json(row.env_vars, {}) if row else {})
            merged_env.update(worker_env)
            merged_tool_env = dict(_as_json(row.tool_env_vars, {}) if row else {})
            merged_tool_env.update(tool_env)
            merged_defaults = dict(_as_json(row.tool_defaults, {}) if row else {})
            for tool, defaults in tool_defaults.items():
                merged_defaults[tool] = {**(merged_defaults.get(tool) or {}), **defaults}
            values = {
                "env_vars": merged_env,
                "tool_env_vars": merged_tool_env,
                "tool_defaults": merged_defaults,
                "updated_at": now,
            }
            if row is None:
                conn.execute(
                    spawn.insert().values(
                        guild_id=g.id,
                        user_id=None,
                        repos="[]",
                        tools="[]",
                        agent_count=None,
                        provider=None,
                        model=None,
                        **values,
                    )
                )
            else:
                conn.execute(spawn.update().where(spawn.c.id == row.id).values(**values))

        conn.execute(guilds.update().where(guilds.c.id == g.id).values(foreman_config=cleaned))

    # Every remaining row's Pi defaults now live in tool_defaults; the top-level
    # provider/model columns are the *worker's* provider/model, not Pi's.
    for row in conn.execute(sa.select(spawn).where(spawn.c.user_id.is_not(None))).fetchall():
        defaults = dict(_as_json(row.tool_defaults, {}))
        if defaults.get("pi") or not (row.provider or row.model):
            continue
        defaults["pi"] = {k: v for k, v in (("provider", row.provider), ("model", row.model)) if v}
        conn.execute(
            spawn.update()
            .where(spawn.c.id == row.id)
            .values(tool_defaults=defaults, updated_at=now)
        )


def downgrade() -> None:
    """No-op: the worker-facing values live in spawn_settings and are readable
    there. Copying them back into foreman_config would re-create the dual store
    this migration exists to remove."""
