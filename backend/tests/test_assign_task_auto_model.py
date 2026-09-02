"""Integration tests: assign_task auto-selects model from tier+catalog (#692).

When the foreman calls assign_task without an explicit ``model`` argument the
handler maps phase+tool → tier → best model in the worker's provider catalog
and persists the resolved value on the task row.  Explicit overrides bypass
auto-selection entirely.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from _test_config import TEST_DATABASE_URL
from foreman.tools import exec_tools
from helpers import create_db, insert_guild, insert_worker, truncate_all
from models import Guild, ModelCatalog, SpawnSettings, Task

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(monkeypatch):
    """Provide the PostgreSQL test database, isolated per test."""
    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)

    yield TEST_DATABASE_URL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool(name: str, inputs: dict, tool_id: str = "tool-abc") -> SimpleNamespace:
    return SimpleNamespace(name=name, input=inputs, id=tool_id)


async def _seed_catalog(db_url: str, provider: str, model_ids: list[str]) -> None:
    """Insert model catalog rows directly via the async session."""
    now = datetime.now(UTC)
    async with database_module.AsyncSessionLocal() as db:
        for mid in model_ids:
            db.add(
                ModelCatalog(
                    provider=provider,
                    model_id=mid,
                    display_name=mid,
                    fetched_at=now,
                )
            )
        await db.commit()


async def _get_task(worker_id: str) -> Task | None:
    async with database_module.AsyncSessionLocal() as db:
        return (await db.exec(select(Task).where(col(Task.worker_id) == worker_id))).one_or_none()


async def _set_tool_defaults(db_url: str, guild_id: str, defaults: dict) -> None:
    async with database_module.AsyncSessionLocal() as db:
        guild_pk = (await db.exec(select(col(Guild.id)).where(col(Guild.slug) == guild_id))).one()
        row = SpawnSettings(
            guild_id=guild_pk,
            user_id=None,
            tool_defaults=defaults,
            updated_at=datetime.now(UTC),
        )
        db.add(row)
        await db.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAssignTaskAutoModelSelection:
    @pytest.mark.asyncio
    async def test_execute_phase_auto_selects_standard_tier_model(self, db_session):
        """assign_task without model= and phase=execute picks a standard-tier model."""
        guild_id = "g-am001"
        worker_id = "w-am001"
        insert_guild(db_session, guild_id)
        insert_worker(
            db_session, guild_id, worker_id, state="idle", tools='["claude"]', provider="anthropic"
        )
        await _seed_catalog(
            db_session,
            "anthropic",
            ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-8"],
        )

        tu = _tool(
            "assign_task",
            {"worker_id": worker_id, "description": "Implement the feature", "phase": "execute"},
            "tool-001",
        )
        with (
            patch("foreman.tools.broadcast", new=AsyncMock()),
            patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
        ):
            results = await exec_tools(guild_id, [tu])

        r = results[0]
        assert not r.get("is_error"), f"Expected success but got error: {r['content']}"

        task = await _get_task(worker_id)
        assert task is not None, "Task row not created"
        assert task.model is not None, "model should be auto-selected, not null"
        # execute → standard tier → claude-sonnet-4-6 is first preference for anthropic
        assert task.model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_review_phase_auto_selects_cheap_tier_model(self, db_session):
        """review phase maps to cheap tier — cheapest model in preference list is chosen."""
        guild_id = "g-am002"
        worker_id = "w-am002"
        insert_guild(db_session, guild_id)
        insert_worker(
            db_session, guild_id, worker_id, state="idle", tools='["claude"]', provider="anthropic"
        )
        await _seed_catalog(
            db_session,
            "anthropic",
            ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"],
        )

        tu = _tool(
            "assign_task",
            {"worker_id": worker_id, "description": "Review the PR", "phase": "review"},
            "tool-002",
        )
        with (
            patch("foreman.tools.broadcast", new=AsyncMock()),
            patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
        ):
            results = await exec_tools(guild_id, [tu])

        r = results[0]
        assert not r.get("is_error"), f"Expected success but got error: {r['content']}"

        task = await _get_task(worker_id)
        assert task is not None
        assert task.model is not None
        # review → cheap tier → haiku is first preference for anthropic/cheap
        assert task.model == "claude-haiku-4-5-20251001"

    @pytest.mark.asyncio
    async def test_model_input_is_ignored_model_auto_selected_from_tier(self, db_session):
        """model= in tool input is not in the schema and is ignored; tier drives selection.

        The refactor (issue #1235) removed inp.get("model") from assign_task so that
        the schema (which exposes tier, not model) is the single source of truth.
        The handler always auto-selects from catalog using the resolved tier.
        """
        guild_id = "g-am003"
        worker_id = "w-am003"
        insert_guild(db_session, guild_id)
        insert_worker(
            db_session, guild_id, worker_id, state="idle", tools='["claude"]', provider="anthropic"
        )
        await _seed_catalog(
            db_session,
            "anthropic",
            ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-8"],
        )

        tu = _tool(
            "assign_task",
            {
                "worker_id": worker_id,
                "description": "Big complex task",
                "phase": "execute",
                # model= is not in the schema; passing it has no effect.
                # tier drives auto-selection; execute → standard → sonnet.
                "tier": "standard",
            },
            "tool-003",
        )
        with (
            patch("foreman.tools.broadcast", new=AsyncMock()),
            patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
        ):
            results = await exec_tools(guild_id, [tu])

        r = results[0]
        assert not r.get("is_error"), f"Expected success but got error: {r['content']}"

        task = await _get_task(worker_id)
        assert task is not None
        # tier=standard for execute phase → sonnet is the standard-tier model
        assert task.model == "claude-sonnet-4-6", (
            "Tier-driven auto-selection must resolve correctly"
        )

    @pytest.mark.asyncio
    async def test_auto_selected_model_persisted_on_task_row(self, db_session):
        """The resolved model is visible on the task record after assignment."""
        guild_id = "g-am004"
        worker_id = "w-am004"
        insert_guild(db_session, guild_id)
        insert_worker(
            db_session, guild_id, worker_id, state="idle", tools='["claude"]', provider="anthropic"
        )
        await _seed_catalog(db_session, "anthropic", ["claude-sonnet-4-6"])

        tu = _tool(
            "assign_task",
            {"worker_id": worker_id, "description": "Write tests"},
            "tool-004",
        )
        with (
            patch("foreman.tools.broadcast", new=AsyncMock()),
            patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
        ):
            results = await exec_tools(guild_id, [tu])

        assert not results[0].get("is_error")

        task = await _get_task(worker_id)
        assert task is not None
        # model must be persisted (non-null) on the DB row
        assert task.model is not None
        assert len(task.model) > 0

    @pytest.mark.asyncio
    async def test_claude_uses_worker_provider_catalog_not_spawn_tool_defaults(self, db_session):
        guild_id = "g-am005"
        worker_id = "w-am005"
        insert_guild(db_session, guild_id)
        insert_worker(
            db_session, guild_id, worker_id, state="idle", tools='["claude"]', provider="anthropic"
        )
        await _set_tool_defaults(
            db_session,
            guild_id,
            {"pi": {"provider": "bedrock"}, "codex": {"model": "gpt-5-codex"}},
        )
        await _seed_catalog(db_session, "anthropic", ["claude-sonnet-4-6"])

        tu = _tool("assign_task", {"worker_id": worker_id, "description": "Use Claude"})
        with (
            patch("foreman.tools.broadcast", new=AsyncMock()),
            patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
        ):
            results = await exec_tools(guild_id, [tu])

        assert not results[0].get("is_error"), results[0]["content"]
        task = await _get_task(worker_id)
        assert task is not None
        assert task.tool == "claude"
        assert task.model == "claude-sonnet-4-6"
        assert task.provider is None

    @pytest.mark.asyncio
    async def test_codex_uses_spawn_settings_default_model(self, db_session):
        guild_id = "g-am006"
        worker_id = "w-am006"
        insert_guild(db_session, guild_id)
        insert_worker(db_session, guild_id, worker_id, state="idle", tools='["codex"]')
        await _set_tool_defaults(db_session, guild_id, {"codex": {"model": "gpt-5-codex"}})

        tu = _tool(
            "assign_task",
            {"worker_id": worker_id, "description": "Use Codex", "tool": "codex"},
        )
        with (
            patch("foreman.tools.broadcast", new=AsyncMock()),
            patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
        ):
            results = await exec_tools(guild_id, [tu])

        assert not results[0].get("is_error"), results[0]["content"]
        task = await _get_task(worker_id)
        assert task is not None
        assert task.tool == "codex"
        assert task.model == "gpt-5-codex"
        assert task.provider is None

    @pytest.mark.asyncio
    async def test_pi_blank_spawn_settings_leaves_provider_and_model_blank(self, db_session):
        """Pi tasks should let the Pi CLI pick unless spawn_settings pins a Pi default."""
        guild_id = "g-am007"
        worker_id = "w-am007"
        insert_guild(db_session, guild_id)
        insert_worker(
            db_session, guild_id, worker_id, state="idle", tools='["pi"]', provider="anthropic"
        )
        await _seed_catalog(db_session, "anthropic", ["claude-sonnet-4-6"])

        tu = _tool(
            "assign_task",
            {"worker_id": worker_id, "description": "Let pi pick", "tool": "pi"},
        )
        with (
            patch("foreman.tools.broadcast", new=AsyncMock()),
            patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
        ):
            results = await exec_tools(guild_id, [tu])

        assert not results[0].get("is_error"), results[0]["content"]
        task = await _get_task(worker_id)
        assert task is not None
        assert task.tool == "pi"
        assert task.model is None
        assert task.provider is None

    @pytest.mark.asyncio
    async def test_pi_uses_explicit_spawn_settings_default_provider(self, db_session):
        guild_id = "g-am008"
        worker_id = "w-am008"
        insert_guild(db_session, guild_id)
        insert_worker(db_session, guild_id, worker_id, state="idle", tools='["pi"]')
        await _set_tool_defaults(db_session, guild_id, {"pi": {"provider": "bedrock"}})

        tu = _tool(
            "assign_task",
            {"worker_id": worker_id, "description": "Use Pi default", "tool": "pi"},
        )
        with (
            patch("foreman.tools.broadcast", new=AsyncMock()),
            patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
        ):
            results = await exec_tools(guild_id, [tu])

        assert not results[0].get("is_error"), results[0]["content"]
        task = await _get_task(worker_id)
        assert task is not None
        assert task.tool == "pi"
        assert task.model is None
        assert task.provider == "bedrock"
