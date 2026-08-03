"""Named, reusable spawn-worker configurations ("spawn profiles").

A profile bundles the ``tool``/``provider``/``credentials_source`` a worker
should run with plus convenience defaults (``default_agent_count``,
``default_repos``), so a caller can say "spawn a claude-aws worker" instead of
restating that bundle on every ``spawn_worker`` call. Layered on top of, not a
replacement for, ``spawn_config``'s guild-baseline/user-override
``spawn_settings`` rows: ``resolve_profile`` looks a name up (user-scoped row >
guild-scoped row > a built-in), and ``as_layer`` turns the result into a
``SpawnLayer`` that ``spawn_config.resolve_spawn`` folds in between the
spawn_settings layers and the call's own explicit args (see resolve_spawn's
``profile_layer`` parameter) — so a profile sets the defaults for a tool+
provider pairing while per-spawn overrides still win.

See models.SpawnProfile for the DB-backed (guild- or user-scoped) custom
profile storage, and docs/spawn-profiles.md for the on-disk format.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class SpawnProfileDef:
    """A built-in profile: same shape as a SpawnProfile row, minus id/scope."""

    name: str
    description: str
    tool: str
    provider: str
    credentials_source: str
    default_agent_count: int | None = None
    default_repos: tuple[str, ...] | None = None
    # Env vars this profile's tool+provider pairing needs on the worker beyond
    # what the guild/user's forwarded spawn_settings env_vars already supply
    # (e.g. flipping on Bedrock mode for claude). Not part of the DB-backed
    # SpawnProfile schema — custom profiles express this via the guild/user
    # env_vars they already have, not per-profile.
    env_vars: dict[str, str] = field(default_factory=dict)


# The initial profile set (see docs/spawn-profiles.md). Credential values
# themselves are never stored here or anywhere in a profile — the worker still
# resolves actual secrets from the guild/user's forwarded env vars
# (`GET /guilds/{id}/foreman/env-vars`) at startup; `credentials_source` only
# names which env var group a given profile expects to be present.
BUILTIN_PROFILES: dict[str, SpawnProfileDef] = {
    p.name: p
    for p in (
        SpawnProfileDef(
            name="claude-default",
            description="Claude Code via the Anthropic API",
            tool="claude",
            provider="api-key",
            credentials_source="anthropic_api_key",
        ),
        SpawnProfileDef(
            name="claude-aws",
            description="Claude Code via AWS Bedrock",
            tool="claude",
            provider="aws-bedrock",
            credentials_source="aws_bedrock",
            env_vars={"CLAUDE_CODE_USE_BEDROCK": "1"},
        ),
        SpawnProfileDef(
            name="codex-api",
            description="Codex via the OpenAI API",
            tool="codex",
            provider="api-key",
            credentials_source="openai_api_key",
        ),
        SpawnProfileDef(
            name="codex-aws",
            description="Codex via AWS",
            tool="codex",
            provider="aws-bedrock",
            credentials_source="aws_bedrock",
        ),
        SpawnProfileDef(
            name="pi-aws",
            description="Pi via AWS Bedrock",
            tool="pi",
            provider="aws-bedrock",
            credentials_source="aws_bedrock",
        ),
    )
}


@dataclass
class ResolvedProfile:
    """A profile after resolution, regardless of which layer it came from."""

    id: str
    source: str  # "builtin" | "guild" | "user"
    name: str
    description: str | None
    tool: str
    provider: str | None
    credentials_source: str | None
    default_agent_count: int | None
    default_repos: list[str] = field(default_factory=list)
    env_vars: dict[str, str] = field(default_factory=dict)


def _row_to_resolved(row, source: str) -> ResolvedProfile:
    try:
        repos = json.loads(row.default_repos) if row.default_repos else []
    except (ValueError, TypeError):
        repos = []
    return ResolvedProfile(
        id=row.id,
        source=source,
        name=row.name,
        description=row.description,
        tool=row.tool,
        provider=row.provider,
        credentials_source=row.credentials_source,
        default_agent_count=row.default_agent_count,
        default_repos=repos,
    )


def _builtin_to_resolved(p: SpawnProfileDef) -> ResolvedProfile:
    return ResolvedProfile(
        id=p.name,
        source="builtin",
        name=p.name,
        description=p.description,
        tool=p.tool,
        provider=p.provider,
        credentials_source=p.credentials_source,
        default_agent_count=p.default_agent_count,
        default_repos=list(p.default_repos or ()),
        env_vars=dict(p.env_vars),
    )


async def resolve_profile(
    db, guild_pk: int, user_id: str | None, name: str
) -> ResolvedProfile | None:
    """Resolve *name* to a profile: user row > guild row > built-in > None."""
    from models import SpawnProfile  # noqa: PLC0415 — avoid circular import at module load
    from sqlmodel import col, select  # noqa: PLC0415

    if user_id is not None:
        row = (
            await db.exec(
                select(SpawnProfile).where(
                    col(SpawnProfile.guild_id) == guild_pk,
                    col(SpawnProfile.user_id) == user_id,
                    col(SpawnProfile.name) == name,
                )
            )
        ).one_or_none()
        if row is not None:
            return _row_to_resolved(row, "user")

    row = (
        await db.exec(
            select(SpawnProfile).where(
                col(SpawnProfile.guild_id) == guild_pk,
                col(SpawnProfile.user_id).is_(None),
                col(SpawnProfile.name) == name,
            )
        )
    ).one_or_none()
    if row is not None:
        return _row_to_resolved(row, "guild")

    builtin = BUILTIN_PROFILES.get(name)
    return _builtin_to_resolved(builtin) if builtin is not None else None


def as_layer(profile: ResolvedProfile):
    """Turn a resolved profile into a spawn_config.SpawnLayer."""
    from spawn_config import SpawnLayer  # noqa: PLC0415

    return SpawnLayer(
        repos=profile.default_repos or None,
        tools=[profile.tool] if profile.tool else None,
        agent_count=profile.default_agent_count,
        provider=profile.provider,
        env_vars=profile.env_vars or None,
    )


async def generate_profile_id(db) -> str:
    """Return a fresh ``sp-``-prefixed id guaranteed not to collide with an existing row."""
    from models import SpawnProfile  # noqa: PLC0415
    from sqlmodel import col, select  # noqa: PLC0415

    for _ in range(5):
        candidate = "sp-" + secrets.token_hex(3)
        existing = (
            await db.exec(select(col(SpawnProfile.id)).where(col(SpawnProfile.id) == candidate))
        ).one_or_none()
        if existing is None:
            return candidate
    raise RuntimeError("could not generate a unique spawn_profile id after 5 attempts")


async def list_profiles(db, guild_pk: int, user_id: str | None):
    """Guild-wide custom profiles plus *user_id*'s own, if given (DB rows only)."""
    from models import SpawnProfile  # noqa: PLC0415
    from sqlmodel import col, or_, select  # noqa: PLC0415

    clause = col(SpawnProfile.user_id).is_(None)
    if user_id is not None:
        clause = or_(clause, col(SpawnProfile.user_id) == user_id)
    rows = (
        await db.exec(
            select(SpawnProfile)
            .where(col(SpawnProfile.guild_id) == guild_pk, clause)
            .order_by(col(SpawnProfile.name))
        )
    ).all()
    return list(rows)


async def get_profile_row(db, guild_pk: int, profile_id: str):
    """Look up one custom (DB-backed) profile row by id, scoped to the guild."""
    from models import SpawnProfile  # noqa: PLC0415
    from sqlmodel import col, select  # noqa: PLC0415

    return (
        await db.exec(
            select(SpawnProfile).where(
                col(SpawnProfile.guild_id) == guild_pk,
                col(SpawnProfile.id) == profile_id,
            )
        )
    ).one_or_none()


async def create_profile(
    db,
    guild_pk: int,
    user_id: str | None,
    *,
    name: str,
    description: str | None = None,
    tool: str = "claude",
    provider: str | None = None,
    credentials_source: str | None = None,
    default_agent_count: int | None = None,
    default_repos: list[str] | None = None,
):
    from models import SpawnProfile  # noqa: PLC0415

    profile_id = await generate_profile_id(db)
    row = SpawnProfile(
        id=profile_id,
        guild_id=guild_pk,
        user_id=user_id,
        name=name,
        description=description,
        tool=tool,
        provider=provider,
        credentials_source=credentials_source,
        default_agent_count=default_agent_count,
        default_repos=json.dumps(default_repos) if default_repos else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_profile(db, row, **fields):
    """Apply the given fields to an existing profile row. ``default_repos`` is a list."""
    for k, v in fields.items():
        if k == "default_repos":
            v = json.dumps(v) if v else None
        setattr(row, k, v)
    row.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)
    return row


async def delete_profile(db, row) -> None:
    await db.delete(row)
    await db.commit()
