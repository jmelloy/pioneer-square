"""Single resolution path for worker spawn settings.

Both spawn callers (the foreman spawn_worker tool and the REST/UI spawn
endpoint) resolve through here so the override rules live in exactly one place.

Precedence, low -> high: guild baseline row < user override row < explicit
call args. Merge rules per field type:
  - lists (repos, tools): the highest layer that provides a NON-EMPTY list wins
    (replace, not concatenate).
  - scalars (agent_count, provider, model): highest layer with a non-None value.
  - maps (env_vars, tool_env_vars): key-by-key overlay, higher layer wins — but
    an empty-string value never blanks a real value set by a lower layer (the
    one place that guard lives).

See models.SpawnSettings for the storage shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class SpawnLayer:
    """One layer of spawn settings. A field left None/empty means 'unset here'."""

    repos: list[str] | None = None
    tools: list[str] | None = None
    agent_count: int | None = None
    env_vars: dict[str, str] | None = None
    tool_env_vars: dict[str, dict[str, str]] | None = None
    provider: str | None = None
    model: str | None = None


@dataclass
class ResolvedSpawn:
    repos: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    agent_count: int | None = None
    env_vars: dict[str, str] = field(default_factory=dict)
    tool_env_vars: dict[str, dict[str, str]] = field(default_factory=dict)
    provider: str | None = None
    model: str | None = None


def _merge_env(base: dict[str, str], over: dict[str, str]) -> dict[str, str]:
    """Overlay *over* onto *base*; an empty value never blanks a real one."""
    out = dict(base)
    for k, v in over.items():
        if v == "" and out.get(k):
            continue
        out[k] = v
    return out


def merge_layers(layers: list[SpawnLayer | None]) -> ResolvedSpawn:
    """Fold ordered layers (lowest priority first) into a ResolvedSpawn."""
    r = ResolvedSpawn()
    for layer in layers:
        if layer is None:
            continue
        if layer.repos:  # non-empty list wins
            r.repos = list(layer.repos)
        if layer.tools:
            r.tools = list(layer.tools)
        if layer.agent_count is not None:
            r.agent_count = layer.agent_count
        if layer.provider is not None:
            r.provider = layer.provider
        if layer.model is not None:
            r.model = layer.model
        if layer.env_vars:
            r.env_vars = _merge_env(r.env_vars, layer.env_vars)
        if layer.tool_env_vars:
            for tool, kv in layer.tool_env_vars.items():
                r.tool_env_vars[tool] = _merge_env(r.tool_env_vars.get(tool, {}), kv or {})
    return r


def row_to_layer(row) -> SpawnLayer | None:
    """Convert a SpawnSettings row to a SpawnLayer (repos/tools are JSON strings)."""
    if row is None:
        return None

    def _list(s: str | None) -> list[str]:
        try:
            return json.loads(s or "[]")
        except (ValueError, TypeError):
            return []

    return SpawnLayer(
        repos=_list(row.repos),
        tools=_list(row.tools),
        agent_count=row.agent_count,
        env_vars=dict(row.env_vars or {}),
        tool_env_vars={t: dict(kv or {}) for t, kv in (row.tool_env_vars or {}).items()},
        provider=row.provider,
        model=row.model,
    )


async def get_spawn_row(db, guild_pk: int, user_id: str | None = None):
    """Return the guild baseline row (user_id=None) or a user override row, or None."""
    from models import SpawnSettings  # noqa: PLC0415
    from sqlmodel import col, select  # noqa: PLC0415

    clause = (
        col(SpawnSettings.user_id).is_(None)
        if user_id is None
        else col(SpawnSettings.user_id) == user_id
    )
    return (
        await db.exec(select(SpawnSettings).where(col(SpawnSettings.guild_id) == guild_pk, clause))
    ).one_or_none()


async def upsert_spawn_row(db, guild_pk: int, user_id: str | None = None, **fields):
    """Create or update a spawn_settings row, setting only the provided fields.

    Partial by design: e.g. the spawn-defaults endpoint sets repos/tools/agent_count
    on the guild baseline without touching its env_vars/tool_env_vars/provider.
    """
    from models import SpawnSettings  # noqa: PLC0415

    row = await get_spawn_row(db, guild_pk, user_id)
    if row is None:
        row = SpawnSettings(guild_id=guild_pk, user_id=user_id, updated_at=datetime.now(UTC))
        db.add(row)
    for k, v in fields.items():
        setattr(row, k, v)
    row.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)
    return row


async def resolve_spawn(
    db,
    guild_pk: int,
    user_id: str | None,
    call: SpawnLayer | None = None,
    profile_layer: SpawnLayer | None = None,
) -> ResolvedSpawn:
    """Load the guild baseline + user override rows and merge with optional layers.

    Precedence, low -> high: guild baseline row < user override row <
    *profile_layer* < *call*. A profile (see spawn_profiles.py) is a named
    preset the caller opted into for this spawn, so it outranks the standing
    guild/user spawn_settings — but the call's own explicit repos/tools/
    agent_count/etc. are the most specific ask and still win over the profile.
    """
    from models import SpawnSettings  # noqa: PLC0415 — avoid circular import at module load
    from sqlmodel import col, select  # noqa: PLC0415

    guild_row = (
        await db.exec(
            select(SpawnSettings).where(
                col(SpawnSettings.guild_id) == guild_pk,
                col(SpawnSettings.user_id).is_(None),
            )
        )
    ).one_or_none()
    user_row = None
    if user_id is not None:
        user_row = (
            await db.exec(
                select(SpawnSettings).where(
                    col(SpawnSettings.guild_id) == guild_pk,
                    col(SpawnSettings.user_id) == user_id,
                )
            )
        ).one_or_none()
    return merge_layers(
        [row_to_layer(guild_row), row_to_layer(user_row), profile_layer, call]
    )
