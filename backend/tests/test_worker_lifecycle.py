"""Tests for backend/worker_lifecycle.py."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make backend importable from tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from worker_lifecycle import (  # noqa: E402
    WORKER_DRAIN_TIMEOUT,
    drain_stale_workers_on_startup,
    get_current_version,
    record_worker_spawn,
    spawn_replacement_workers,
)

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
async def test_drain_treats_all_workers_as_stale_when_no_version(monkeypatch):
    """No version → conservatively drain all online workers rather than silently skipping."""
    monkeypatch.delenv("PIONEER_VERSION", raising=False)

    fake_worker = MagicMock()
    fake_worker.id = "w-unknown-ver"
    fake_worker.container_id = None

    # Session 1: returns the online worker.
    # Session 2: write drain_requested_at.
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

    sessions = [FakeDB([(fake_worker, "g-guild")]), FakeDB(None)]
    session_iter = iter(sessions)
    broadcast_calls = []

    async def fake_broadcast(guild_slug, msg, *a, **kw):
        broadcast_calls.append((guild_slug, msg))

    with (
        patch("worker_lifecycle.get_current_version", return_value=None),
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle.broadcast_msg", fake_broadcast),
    ):
        result = await drain_stale_workers_on_startup()

    # All online workers are treated as stale and get a shutdown signal.
    assert result == ["w-unknown-ver"]
    assert len(broadcast_calls) == 1
    assert broadcast_calls[0][0] == "g-guild"


@pytest.mark.asyncio
async def test_drain_treats_null_version_workers_as_stale():
    """Workers with NULL spawned_version are drained even when current version is known.

    Pre-migration rows have no spawned_version stamp and cannot be confirmed to match
    the current backend version, so they are conservatively treated as stale.
    """
    fake_worker = MagicMock()
    fake_worker.id = "w-null-ver"
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

    # Session 1: returns the NULL-version worker as stale.
    # Session 2: write drain_requested_at.
    sessions = [FakeDB([(fake_worker, "g-guild")]), FakeDB(None)]
    session_iter = iter(sessions)
    broadcast_calls = []

    async def fake_broadcast(guild_slug, msg, *a, **kw):
        broadcast_calls.append((guild_slug, msg))

    with (
        patch("worker_lifecycle.get_current_version", return_value="v-current"),
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle.broadcast_msg", fake_broadcast),
    ):
        result = await drain_stale_workers_on_startup()

    # NULL-version worker is treated as stale and receives a shutdown signal.
    assert result == ["w-null-ver"]
    assert len(broadcast_calls) == 1
    assert broadcast_calls[0][0] == "g-guild"


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
        result = await drain_stale_workers_on_startup()

    # Only one DB session opened (the initial stale-worker query).
    assert mock_session.call_count == 1
    assert result == []


@pytest.mark.asyncio
async def test_drain_sends_shutdown_and_records_timestamp(monkeypatch):
    """Stale workers receive a WS shutdown signal and drain_requested_at is written."""
    # Build a fake worker row (worker, guild_slug) pair.
    fake_worker = MagicMock()
    fake_worker.id = "w-stale1"
    fake_worker.container_id = None

    # Session 1: stale detection → returns one stale row.
    # Session 2: write drain_requested_at.
    session_results = [
        [(fake_worker, "g-myguild")],  # session 1: stale rows
        None,  # session 2: update + commit
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
    ):
        stale_ids = await drain_stale_workers_on_startup()

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

    # Stale IDs returned for the force-kill background task.
    assert stale_ids == ["w-stale1"]


def test_drain_timeout_default():
    assert WORKER_DRAIN_TIMEOUT == 60.0


def test_drain_timeout_env_override(monkeypatch):
    monkeypatch.setenv("PIONEER_WORKER_DRAIN_TIMEOUT", "120")
    import importlib

    import worker_lifecycle as wl

    try:
        importlib.reload(wl)
        assert wl.WORKER_DRAIN_TIMEOUT == 120.0
    finally:
        # Always restore module state so later tests see the default value.
        monkeypatch.delenv("PIONEER_WORKER_DRAIN_TIMEOUT", raising=False)
        importlib.reload(wl)


# ---------------------------------------------------------------------------
# record_worker_spawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_worker_spawn_writes_fields():
    """record_worker_spawn updates container_id, spawned_version, and started_at."""
    mock_db = AsyncMock()
    mock_db.exec = AsyncMock()
    mock_db.commit = AsyncMock()

    with patch("worker_lifecycle.get_current_version", return_value="v-test"):
        await record_worker_spawn(mock_db, "w-abc123", "container-deadbeef")

    mock_db.exec.assert_called_once()
    mock_db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# _spawn_replacement_workers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_replacement_workers_no_op_on_empty():
    """Empty stale_ids list → no DB or spawn calls."""
    with patch("worker_lifecycle.AsyncSessionLocal") as mock_session:
        await spawn_replacement_workers([])
    mock_session.assert_not_called()


@pytest.mark.asyncio
async def test_spawn_replacement_workers_calls_spawn_for_each():
    """One replacement worker is spawned per stale worker."""
    fake_worker = MagicMock()
    fake_worker.id = "w-old1"
    fake_worker.guild_id = 42
    fake_worker.repos = '["owner/repo"]'
    fake_worker.name = "old-worker"

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

    # Session 1: query workers + guild slugs.
    # Session 2: passed to spawn_worker call.
    sessions = [FakeDB([(fake_worker, "g-myguild")]), FakeDB([])]
    session_iter = iter(sessions)
    spawn_calls: list[dict] = []

    async def fake_spawn(inp, guild_id, guild_pk, db):
        spawn_calls.append({"inp": inp, "guild_id": guild_id, "guild_pk": guild_pk})
        return ("ok", False)

    fake_tools = MagicMock()
    fake_tools.spawn_worker = fake_spawn
    with (
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch.dict("sys.modules", {"foreman": MagicMock(), "foreman.tools": fake_tools}),
    ):
        await spawn_replacement_workers(["w-old1"])

    assert len(spawn_calls) == 1
    assert spawn_calls[0]["guild_id"] == "g-myguild"
    assert spawn_calls[0]["guild_pk"] == 42
    assert spawn_calls[0]["inp"]["repos"] == ["owner/repo"]
