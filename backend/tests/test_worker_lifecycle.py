"""Tests for backend/worker_lifecycle.py."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make backend importable from tests/
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from worker_lifecycle import WORKER_DRAIN_TIMEOUT, drain_stale_workers_on_startup, get_current_version


# ---------------------------------------------------------------------------
# get_current_version
# ---------------------------------------------------------------------------


def test_get_current_version_prefers_env_var(monkeypatch):
    monkeypatch.setenv("PIONEER_VERSION", "v1.2.3")
    assert get_current_version() == "v1.2.3"


def test_get_current_version_strips_whitespace(monkeypatch):
    monkeypatch.setenv("PIONEER_VERSION", "  abc123  ")
    assert get_current_version() == "abc123"


def test_get_current_version_falls_back_to_git(monkeypatch):
    monkeypatch.delenv("PIONEER_VERSION", raising=False)
    with patch("subprocess.check_output", return_value="deadbeef\n") as mock_git:
        result = get_current_version()
    assert result == "deadbeef"
    mock_git.assert_called_once()


def test_get_current_version_returns_none_when_git_fails(monkeypatch):
    monkeypatch.delenv("PIONEER_VERSION", raising=False)
    with patch("subprocess.check_output", side_effect=Exception("no git")):
        result = get_current_version()
    assert result is None


def test_get_current_version_returns_none_when_git_returns_empty(monkeypatch):
    monkeypatch.delenv("PIONEER_VERSION", raising=False)
    with patch("subprocess.check_output", return_value=""):
        result = get_current_version()
    assert result is None


# ---------------------------------------------------------------------------
# drain_stale_workers_on_startup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_skipped_when_no_version(monkeypatch):
    """No version → drain is skipped with a warning, no DB calls."""
    monkeypatch.delenv("PIONEER_VERSION", raising=False)
    with (
        patch("worker_lifecycle.get_current_version", return_value=None),
        patch("worker_lifecycle.AsyncSessionLocal") as mock_session,
    ):
        await drain_stale_workers_on_startup()
    mock_session.assert_not_called()


@pytest.mark.asyncio
async def test_drain_skipped_when_no_stale_workers(monkeypatch):
    """When all workers match the current version, drain is a no-op."""
    mock_exec = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    mock_db = AsyncMock()
    mock_db.exec = mock_exec
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock(return_value=mock_db)

    with (
        patch("worker_lifecycle.get_current_version", return_value="abc123"),
        patch("worker_lifecycle.AsyncSessionLocal", mock_session),
    ):
        await drain_stale_workers_on_startup()

    # Only one DB session opened (the initial stale-worker query).
    assert mock_session.call_count == 1


@pytest.mark.asyncio
async def test_drain_sends_shutdown_and_records_timestamp(monkeypatch):
    """Stale workers receive a WS shutdown signal and drain_requested_at is written."""
    # Build a fake worker row (worker, guild_slug) pair.
    fake_worker = MagicMock()
    fake_worker.id = "w-stale1"
    fake_worker.container_id = None

    # Session 1: stale detection → returns one stale row.
    # Session 2: write drain_requested_at.
    # Session 3: check still-alive after drain timeout → none alive.
    call_count = 0
    session_results = [
        [(fake_worker, "g-myguild")],  # session 1: stale rows
        None,  # session 2: update + commit
        [],  # session 3: still alive
    ]

    class FakeDB:
        def __init__(self, rows):
            self._rows = rows
            self.exec = AsyncMock(
                return_value=MagicMock(all=MagicMock(return_value=self._rows or []))
            )
            self.commit = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    sessions = [FakeDB(r) for r in session_results]
    session_iter = iter(sessions)

    def make_session():
        return next(session_iter)

    broadcast_calls = []

    async def fake_broadcast(guild_slug, msg, *a, **kw):
        broadcast_calls.append((guild_slug, msg))

    with (
        patch("worker_lifecycle.get_current_version", return_value="new-ver"),
        patch("worker_lifecycle.AsyncSessionLocal", make_session),
        patch("worker_lifecycle.broadcast_msg", fake_broadcast),
        patch("worker_lifecycle.asyncio.sleep", AsyncMock()),
    ):
        await drain_stale_workers_on_startup()

    # Shutdown signal was broadcast to the correct guild.
    assert len(broadcast_calls) == 1
    guild_slug, msg = broadcast_calls[0]
    assert guild_slug == "g-myguild"
    assert msg.workerId == "w-stale1"
    assert "version mismatch" in (msg.reason or "")

    # drain_requested_at update was issued.
    session_2 = sessions[1]
    session_2.exec.assert_called()
    session_2.commit.assert_called()


@pytest.mark.asyncio
async def test_drain_force_kills_surviving_container(monkeypatch):
    """After the drain timeout, surviving containers are killed via Docker SDK."""
    fake_worker = MagicMock()
    fake_worker.id = "w-stale2"
    fake_worker.container_id = "abc123deadbeef"

    class FakeDB:
        def __init__(self, rows):
            self._rows = rows
            self.exec = AsyncMock(
                return_value=MagicMock(all=MagicMock(return_value=self._rows or []))
            )
            self.commit = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    sessions = [
        FakeDB([(fake_worker, "g-guild2")]),  # stale detection
        FakeDB(None),  # drain_requested_at update
        FakeDB([fake_worker]),  # still alive after timeout
    ]
    session_iter = iter(sessions)

    fake_container = MagicMock()
    fake_docker_client = MagicMock()
    fake_docker_client.containers.get.return_value = fake_container
    fake_docker_module = MagicMock()
    fake_docker_module.from_env.return_value = fake_docker_client

    with (
        patch("worker_lifecycle.get_current_version", return_value="new-ver"),
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle.broadcast_msg", AsyncMock()),
        patch("worker_lifecycle.asyncio.sleep", AsyncMock()),
        patch.dict("sys.modules", {"docker": fake_docker_module}),
    ):
        await drain_stale_workers_on_startup()

    fake_docker_client.containers.get.assert_called_once_with("abc123deadbeef")
    fake_container.kill.assert_called_once()


@pytest.mark.asyncio
async def test_drain_skips_kill_when_no_container_id(monkeypatch):
    """Workers without a container_id (non-Docker) are not force-killed."""
    fake_worker = MagicMock()
    fake_worker.id = "w-nocontainer"
    fake_worker.container_id = None

    class FakeDB:
        def __init__(self, rows):
            self._rows = rows
            self.exec = AsyncMock(
                return_value=MagicMock(all=MagicMock(return_value=self._rows or []))
            )
            self.commit = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    sessions = [
        FakeDB([(fake_worker, "g-guild3")]),
        FakeDB(None),
        FakeDB([fake_worker]),  # still alive → but no container_id
    ]
    session_iter = iter(sessions)

    fake_docker_module = MagicMock()

    with (
        patch("worker_lifecycle.get_current_version", return_value="new-ver"),
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle.broadcast_msg", AsyncMock()),
        patch("worker_lifecycle.asyncio.sleep", AsyncMock()),
        patch.dict("sys.modules", {"docker": fake_docker_module}),
    ):
        await drain_stale_workers_on_startup()

    # Docker was never asked to kill anything.
    fake_docker_module.from_env.assert_not_called()


def test_drain_timeout_default():
    assert WORKER_DRAIN_TIMEOUT == 60.0


def test_drain_timeout_env_override(monkeypatch):
    monkeypatch.setenv("PIONEER_WORKER_DRAIN_TIMEOUT", "120")
    import importlib
    import worker_lifecycle as wl

    importlib.reload(wl)
    assert wl.WORKER_DRAIN_TIMEOUT == 120.0
    # Restore.
    monkeypatch.delenv("PIONEER_WORKER_DRAIN_TIMEOUT", raising=False)
    importlib.reload(wl)
