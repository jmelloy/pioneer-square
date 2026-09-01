"""Data migration 20260831_000000: the worker-facing slice of foreman_config
moves into spawn_settings, once, for every guild (issue #1240).

Runs the real `upgrade()` against the test database with a seeded legacy guild,
so the assertions cover the migration itself rather than a reimplementation.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(__file__))
from alembic.migration import MigrationContext
from alembic.operations import Operations
from helpers import _sync_session, insert_guild
from models import Guild, SpawnSettings
from sqlalchemy import select, update
from sqlmodel import col

_MIGRATION = os.path.join(
    os.path.dirname(__file__),
    "..",
    "alembic",
    "versions",
    "20260831_000000_finish_spawn_settings_migration.py",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_spawn_migration_1240", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_upgrade(session) -> None:
    """Invoke the migration's upgrade() bound to this session's connection."""
    conn = session.connection()
    ctx = MigrationContext.configure(conn)
    with Operations.context(Operations(ctx)):
        _load_migration().upgrade()


LEGACY_CONFIG = {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "max_rounds": 40,
    "env_vars": [
        {"key": "FOREMAN_ONLY", "value": "f"},
        {"key": "GITHUB_TOKEN", "value": "ghp_x", "forward": True},
    ],
    "tool_env_vars": {"claude": [{"key": "CLAUDE_CODE_OAUTH_TOKEN", "value": "tok"}]},
    "pi_default_provider": "bedrock",
    "pi_default_model": "arn:pi",
    "codex_default_model": "gpt-5",
}


def _seed(db_url: str, slug: str, config: dict) -> None:
    insert_guild(db_url, slug)
    with _sync_session(db_url) as session:
        session.execute(update(Guild).where(col(Guild.slug) == slug).values(foreman_config=config))
        session.commit()


def _read(session, slug: str):
    guild = session.execute(select(Guild).where(col(Guild.slug) == slug)).scalar_one()
    baseline = session.execute(
        select(SpawnSettings).where(
            col(SpawnSettings.guild_id) == guild.id, col(SpawnSettings.user_id).is_(None)
        )
    ).scalar_one_or_none()
    return guild, baseline


def test_worker_slice_moves_to_spawn_settings(client):
    _, db_url = client
    _seed(db_url, "g-mig1", LEGACY_CONFIG)

    with _sync_session(db_url) as session:
        _run_upgrade(session)
        session.commit()
        guild, baseline = _read(session, "g-mig1")

        # foreman_config keeps only the orchestrator-LLM fields.
        assert guild.foreman_config == {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "max_rounds": 40,
            "env_vars": [{"key": "FOREMAN_ONLY", "value": "f"}],
        }

        # ...and every worker-facing value landed in the one store.
        assert baseline is not None
        assert baseline.env_vars == {"GITHUB_TOKEN": "ghp_x"}
        assert baseline.tool_env_vars == {"claude": {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}}
        assert baseline.tool_defaults == {
            "pi": {"provider": "bedrock", "model": "arn:pi"},
            "codex": {"model": "gpt-5"},
        }


def test_migration_is_idempotent(client):
    """Re-running must not resurrect keys or double-merge — the migration is the
    only remaining writer of this move, so a retried deploy has to be safe."""
    _, db_url = client
    _seed(db_url, "g-mig2", LEGACY_CONFIG)

    with _sync_session(db_url) as session:
        _run_upgrade(session)
        session.commit()
        _, first = _read(session, "g-mig2")
        snapshot = (first.env_vars, first.tool_env_vars, first.tool_defaults)

        _run_upgrade(session)
        session.commit()
        guild, second = _read(session, "g-mig2")

    assert (second.env_vars, second.tool_env_vars, second.tool_defaults) == snapshot
    assert "tool_env_vars" not in guild.foreman_config


def test_existing_baseline_row_is_merged_not_replaced(client):
    """A guild that already has spawn settings keeps them; the legacy values
    overlay on top rather than clobbering the row."""
    _, db_url = client
    _seed(db_url, "g-mig3", {"tool_env_vars": {"pi": [{"key": "PI_KEY", "value": "p"}]}})
    with _sync_session(db_url) as session:
        guild = session.execute(select(Guild).where(col(Guild.slug) == "g-mig3")).scalar_one()
        session.add(
            SpawnSettings(
                guild_id=guild.id,
                user_id=None,
                repos='["owner/repo"]',
                tools='["claude"]',
                agent_count=3,
                env_vars={"KEPT": "yes"},
                tool_env_vars={"claude": {"CLAUDE_KEY": "c"}},
                tool_defaults={},
                updated_at=datetime.now(UTC),
            )
        )
        session.commit()

        _run_upgrade(session)
        session.commit()
        _, baseline = _read(session, "g-mig3")

    assert baseline.repos == '["owner/repo"]'
    assert baseline.agent_count == 3
    assert baseline.env_vars == {"KEPT": "yes"}
    assert baseline.tool_env_vars == {"claude": {"CLAUDE_KEY": "c"}, "pi": {"PI_KEY": "p"}}


def test_legacy_top_level_provider_model_becomes_pi_tool_default(client):
    """20260728_000002 parked the Pi defaults in the row's provider/model columns,
    which the tool-default resolver deliberately ignores for pi. Fold them in."""
    _, db_url = client
    _seed(db_url, "g-mig4", {"provider": "anthropic"})
    with _sync_session(db_url) as session:
        guild = session.execute(select(Guild).where(col(Guild.slug) == "g-mig4")).scalar_one()
        session.add(
            SpawnSettings(
                guild_id=guild.id,
                user_id=None,
                env_vars={},
                tool_env_vars={},
                tool_defaults={},
                provider="bedrock",
                model="arn:legacy-pi",
                updated_at=datetime.now(UTC),
            )
        )
        session.commit()

        _run_upgrade(session)
        session.commit()
        _, baseline = _read(session, "g-mig4")

    assert baseline.tool_defaults == {"pi": {"provider": "bedrock", "model": "arn:legacy-pi"}}


def test_guild_with_nothing_to_migrate_is_untouched(client):
    _, db_url = client
    clean = {"provider": "anthropic", "max_rounds": 10}
    _seed(db_url, "g-mig5", clean)

    with _sync_session(db_url) as session:
        _run_upgrade(session)
        session.commit()
        guild, baseline = _read(session, "g-mig5")

    assert guild.foreman_config == clean
    assert baseline is None
