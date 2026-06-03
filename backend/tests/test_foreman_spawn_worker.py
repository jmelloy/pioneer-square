"""Tests for the foreman spawn_worker tool and related helpers."""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from _test_config import TEST_DATABASE_URL
from foreman.tools import _exec_one_tool
from helpers import _sync_session, create_db, insert_guild, truncate_all
from models import Worker
from sqlalchemy import select
from sqlmodel import col
from utils import build_spawn_worker_env

from foreman import tools as foreman_tools

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(monkeypatch):
    """Provide the PostgreSQL test database, truncated before each test."""
    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)
    db_url = TEST_DATABASE_URL

    monkeypatch.setenv("DATABASE_URL", db_url)

    engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)

    yield db_url


def _fake_tool_use(name: str, inputs: dict, tool_id: str = "tool-spawn01") -> SimpleNamespace:
    return SimpleNamespace(name=name, input=inputs, id=tool_id)


# ---------------------------------------------------------------------------
# build_spawn_worker_env tests (pure, no DB)
# ---------------------------------------------------------------------------


def test_build_spawn_worker_env_basic():
    env = build_spawn_worker_env(
        guild_id="abc123",
        repos=["owner/repo"],
        worker_name=None,
        source_env={},
    )
    assert env["PIONEER_GUILD_ID"] == "abc123"
    assert env["PIONEER_REPOS"] == "owner/repo"
    assert env["PIONEER_BACKEND_URL"] == "http://backend:8000"


def test_build_spawn_worker_env_worker_id_and_auth_token():
    env = build_spawn_worker_env(
        guild_id="abc123",
        repos=["owner/repo"],
        worker_name=None,
        source_env={},
        worker_id="w-abc123",
        auth_token="tok-secret",
    )
    assert env["PIONEER_WORKER_ID"] == "w-abc123"
    assert env["PIONEER_AUTH_TOKEN"] == "tok-secret"


def test_build_spawn_worker_env_agent_count_and_tools():
    env = build_spawn_worker_env(
        guild_id="abc123",
        repos=["owner/repo"],
        worker_name=None,
        source_env={},
        agent_count=2,
        tools=["claude", "codex"],
    )
    assert env["PIONEER_MAX_AGENTS"] == "2"
    assert env["PIONEER_TOOLS"] == "claude,codex"


def test_build_spawn_worker_env_no_worker_id_keys_absent():
    env = build_spawn_worker_env(
        guild_id="abc123",
        repos=["owner/repo"],
        worker_name=None,
        source_env={},
    )
    assert "PIONEER_WORKER_ID" not in env
    assert "PIONEER_AUTH_TOKEN" not in env
    assert "PIONEER_MAX_AGENTS" not in env
    assert "PIONEER_TOOLS" not in env


def test_build_spawn_worker_env_multiple_repos():
    env = build_spawn_worker_env(
        guild_id="abc123",
        repos=["owner/repo-a", "owner/repo-b"],
        worker_name="my-worker",
        source_env={"GITHUB_TOKEN": "ghp_test"},
    )
    assert env["PIONEER_REPOS"] == "owner/repo-a,owner/repo-b"
    assert env["PIONEER_WORKER_NAME"] == "my-worker"
    assert env["GITHUB_TOKEN"] == "ghp_test"
    assert env["PIONEER_GITHUB_TOKEN"] == "ghp_test"


def test_build_spawn_worker_env_extra_env_passthrough():
    env = build_spawn_worker_env(
        guild_id="abc123",
        repos=["owner/repo"],
        worker_name=None,
        source_env={},
        extra_env={"MY_SECRET": "hunter2", "DEBUG": "1"},
    )
    assert env["MY_SECRET"] == "hunter2"
    assert env["DEBUG"] == "1"
    # Pioneer's required vars must not be clobbered by extra_env
    assert env["PIONEER_GUILD_ID"] == "abc123"


def test_build_spawn_worker_env_extra_env_pioneer_vars_win():
    # User tries to override a required Pioneer var — Pioneer wins.
    env = build_spawn_worker_env(
        guild_id="real-guild",
        repos=["owner/repo"],
        worker_name=None,
        source_env={},
        extra_env={"PIONEER_GUILD_ID": "injected", "PIONEER_REPOS": "evil/repo"},
    )
    assert env["PIONEER_GUILD_ID"] == "real-guild"
    assert env["PIONEER_REPOS"] == "owner/repo"


# ---------------------------------------------------------------------------
# spawn_worker tool — happy path (Docker mocked)
# ---------------------------------------------------------------------------


async def test_spawn_worker_pre_registers_and_spawns(db_session):
    insert_guild(db_session, "guild01")

    fake_container = MagicMock()
    fake_container.id = "abc123def456789"

    fake_docker_client = MagicMock()
    fake_docker_client.containers.get.side_effect = Exception("no hostname")
    fake_docker_client.containers.run.return_value = fake_container

    with patch.object(foreman_tools, "_get_docker_client", return_value=fake_docker_client):
        result = await _exec_one_tool(
            "guild01",
            _fake_tool_use("spawn_worker", {"repos": ["acme/backend"]}),
        )

    assert result["type"] == "tool_result"
    assert not result.get("is_error"), result["content"]

    payload = json.loads(result["content"])
    assert payload["worker_id"].startswith("w-")
    assert payload["container_id"] == "abc123def456"  # first 12 chars
    assert payload["repos"] == ["acme/backend"]

    # Worker must be persisted in DB
    with _sync_session(db_session) as session:
        workers = (
            session.execute(select(Worker).where(col(Worker.id) == payload["worker_id"]))
            .scalars()
            .all()
        )
    assert len(workers) == 1
    assert json.loads(workers[0].repos) == ["acme/backend"]
    assert workers[0].auth_token is not None


async def test_spawn_worker_with_agent_count_and_tools(db_session):
    insert_guild(db_session, "guild02")

    fake_container = MagicMock()
    fake_container.id = "deadbeef000000"

    fake_docker_client = MagicMock()
    fake_docker_client.containers.get.side_effect = Exception("no hostname")
    fake_docker_client.containers.run.return_value = fake_container

    captured_env: dict = {}

    def _capture_run(**kwargs):
        captured_env.update(kwargs.get("environment", {}))
        return fake_container

    fake_docker_client.containers.run.side_effect = _capture_run

    with patch.object(foreman_tools, "_get_docker_client", return_value=fake_docker_client):
        result = await _exec_one_tool(
            "guild02",
            _fake_tool_use(
                "spawn_worker",
                {
                    "repos": ["acme/backend"],
                    "agent_count": 2,
                    "tools": ["claude"],
                    "name": "test-worker",
                },
            ),
        )

    assert not result.get("is_error"), result["content"]
    assert captured_env["PIONEER_MAX_AGENTS"] == "2"
    assert captured_env["PIONEER_TOOLS"] == "claude"
    assert captured_env["PIONEER_WORKER_NAME"] == "test-worker"


# ---------------------------------------------------------------------------
# spawn_worker tool — error cases
# ---------------------------------------------------------------------------


async def test_spawn_worker_no_repos_returns_error(db_session):
    insert_guild(db_session, "guild03")

    result = await _exec_one_tool(
        "guild03",
        _fake_tool_use("spawn_worker", {"repos": []}),
    )
    assert result.get("is_error")
    assert "repos" in result["content"].lower()


async def test_spawn_worker_docker_unavailable(db_session):
    """When Docker SDK is not installed, worker is still pre-registered and error is returned."""
    insert_guild(db_session, "guild04")

    with patch.object(
        foreman_tools, "_get_docker_client", side_effect=ImportError("No module named 'docker'")
    ):
        result = await _exec_one_tool(
            "guild04",
            _fake_tool_use("spawn_worker", {"repos": ["acme/api"]}),
        )

    assert result.get("is_error")
    assert "docker" in result["content"].lower()
    # The worker_id should be mentioned in the error so foreman can act on it
    assert "w-" in result["content"]

    # Worker must still be persisted even on Docker failure
    with _sync_session(db_session) as session:
        count = session.execute(select(Worker)).scalars().all()
    assert len(count) == 1


async def test_spawn_worker_docker_run_fails(db_session):
    """When Docker.run raises, worker pre-registration is committed and error reported."""
    insert_guild(db_session, "guild05")

    fake_docker_client = MagicMock()
    fake_docker_client.containers.get.side_effect = Exception("no hostname")
    fake_docker_client.containers.run.side_effect = Exception("image not found")

    with patch.object(foreman_tools, "_get_docker_client", return_value=fake_docker_client):
        result = await _exec_one_tool(
            "guild05",
            _fake_tool_use("spawn_worker", {"repos": ["acme/api"]}),
        )

    assert result.get("is_error")
    assert "image not found" in result["content"]

    with _sync_session(db_session) as session:
        count = session.execute(select(Worker)).scalars().all()
    assert len(count) == 1
