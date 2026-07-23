"""Tests for backend/worker_lifecycle.py."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make backend importable from tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from worker_lifecycle import (  # noqa: E402
    SHUTDOWN_FORCE_KILL_TIMEOUT,
    WORKER_SPAWN_COOLDOWN,
    check_worker_spawn_cooldown,
    drain_stale_workers_on_startup,
    force_kill_worker_if_unresponsive,
    generate_worker_id,
    get_current_version,
    record_worker_spawn,
    signal_stale_worker_on_join,
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


@pytest.mark.asyncio
async def test_record_worker_spawn_upserts_guild_defaults():
    """With guild_pk and repos, the guild's spawn defaults are upserted in the same commit."""
    mock_db = AsyncMock()
    mock_db.exec = AsyncMock()
    mock_db.commit = AsyncMock()

    with patch("worker_lifecycle.get_current_version", return_value="v-test"):
        await record_worker_spawn(
            mock_db,
            "w-abc123",
            "container-deadbeef",
            guild_pk=42,
            repos=["acme/widgets"],
            tools=["claude"],
            agent_count=2,
        )

    # Worker update + guild_spawn_defaults upsert, one commit.
    assert mock_db.exec.call_count == 2
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_record_worker_spawn_skips_defaults_without_repos():
    """No repos recorded (or no guild_pk) → the defaults row is left untouched."""
    mock_db = AsyncMock()
    mock_db.exec = AsyncMock()
    mock_db.commit = AsyncMock()

    with patch("worker_lifecycle.get_current_version", return_value="v-test"):
        await record_worker_spawn(mock_db, "w-abc123", "container-deadbeef", guild_pk=42, repos=[])

    mock_db.exec.assert_called_once()
    mock_db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# check_worker_spawn_cooldown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_worker_spawn_cooldown_no_prior_spawn():
    """A guild with no recorded spawn defaults is never in cooldown."""
    mock_db = AsyncMock()
    mock_db.exec = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=None)))

    remaining = await check_worker_spawn_cooldown(mock_db, 42)

    assert remaining is None


@pytest.mark.asyncio
async def test_check_worker_spawn_cooldown_recent_spawn_still_active():
    """A spawn 1 minute ago is still within the 5-minute cooldown."""
    defaults = MagicMock(updated_at=datetime.now(UTC) - timedelta(minutes=1))
    mock_db = AsyncMock()
    mock_db.exec = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=defaults)))

    remaining = await check_worker_spawn_cooldown(mock_db, 42)

    assert remaining is not None
    assert timedelta(minutes=3) < remaining <= timedelta(minutes=4)


@pytest.mark.asyncio
async def test_check_worker_spawn_cooldown_expired():
    """A spawn from longer ago than the cooldown window is not in cooldown."""
    defaults = MagicMock(
        updated_at=datetime.now(UTC) - WORKER_SPAWN_COOLDOWN - timedelta(seconds=1)
    )
    mock_db = AsyncMock()
    mock_db.exec = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=defaults)))

    remaining = await check_worker_spawn_cooldown(mock_db, 42)

    assert remaining is None


# ---------------------------------------------------------------------------
# signal_stale_worker_on_join
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signal_stale_worker_on_join_sends_shutdown():
    """A worker joining with a version stamp that differs from the backend's is signalled."""
    broadcasts = []

    async def fake_broadcast(guild_slug, msg, *a, **kw):
        broadcasts.append((guild_slug, msg))

    with (
        patch("worker_lifecycle.get_current_version", return_value="v-new"),
        patch("worker_lifecycle.broadcast_msg", fake_broadcast),
    ):
        signalled = await signal_stale_worker_on_join("g-guild", "w-stale", "v-old")

    assert signalled is True
    assert len(broadcasts) == 1
    guild_slug, msg = broadcasts[0]
    assert guild_slug == "g-guild"
    assert msg.workerId == "w-stale"
    assert "version mismatch" in (msg.reason or "")


@pytest.mark.asyncio
async def test_signal_stale_worker_on_join_skips_matching_version():
    broadcast = AsyncMock()
    with (
        patch("worker_lifecycle.get_current_version", return_value="v-current"),
        patch("worker_lifecycle.broadcast_msg", broadcast),
    ):
        signalled = await signal_stale_worker_on_join("g-guild", "w-fresh", "v-current")

    assert signalled is False
    broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_signal_stale_worker_on_join_skips_external_workers():
    """Workers without a spawn version stamp (compose/manual CLI) are never signalled."""
    broadcast = AsyncMock()
    with (
        patch("worker_lifecycle.get_current_version", return_value="v-current"),
        patch("worker_lifecycle.broadcast_msg", broadcast),
    ):
        signalled = await signal_stale_worker_on_join("g-guild", "w-external", None)

    assert signalled is False
    broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_signal_stale_worker_on_join_skips_when_version_unknown():
    """An undetermined backend version can't be compared — no signal."""
    broadcast = AsyncMock()
    with (
        patch("worker_lifecycle.get_current_version", return_value=None),
        patch("worker_lifecycle.broadcast_msg", broadcast),
    ):
        signalled = await signal_stale_worker_on_join("g-guild", "w-any", "v-old")

    assert signalled is False
    broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_signal_stale_worker_on_join_spares_freshly_spawned():
    """A stale-version worker spawned seconds ago (deploy overlap) is not killed."""
    broadcast = AsyncMock()
    just_now = datetime.now(UTC) - timedelta(seconds=5)
    with (
        patch("worker_lifecycle.get_current_version", return_value="v-new"),
        patch("worker_lifecycle.broadcast_msg", broadcast),
    ):
        signalled = await signal_stale_worker_on_join("g-guild", "w-newborn", "v-old", just_now)

    assert signalled is False
    broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_signal_stale_worker_on_join_kills_old_stale_worker():
    """A stale-version worker spawned long ago (real zombie) is still signalled."""
    broadcasts = []

    async def fake_broadcast(guild_slug, msg, *a, **kw):
        broadcasts.append((guild_slug, msg))

    long_ago = datetime.now(UTC) - timedelta(hours=1)
    with (
        patch("worker_lifecycle.get_current_version", return_value="v-new"),
        patch("worker_lifecycle.broadcast_msg", fake_broadcast),
    ):
        signalled = await signal_stale_worker_on_join("g-guild", "w-zombie", "v-old", long_ago)

    assert signalled is True
    assert len(broadcasts) == 1


# ---------------------------------------------------------------------------
# disabled worker — drain exclusion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_skips_disabled_workers():
    """Disabled workers are excluded from the stale drain query and not re-spawned."""
    fake_worker = MagicMock()
    fake_worker.id = "w-disabled"
    fake_worker.container_id = None
    fake_worker.disabled = True

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

    # The query returns no rows because the disabled worker is filtered out at the DB level.
    sessions = [FakeDB([])]
    session_iter = iter(sessions)
    broadcast_calls = []

    async def fake_broadcast(guild_slug, msg, *a, **kw):
        broadcast_calls.append((guild_slug, msg))

    with (
        patch("worker_lifecycle.get_current_version", return_value="v-new"),
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle.broadcast_msg", fake_broadcast),
    ):
        result = await drain_stale_workers_on_startup()

    # Disabled worker is not drained — empty result, no broadcast.
    assert result == []
    assert broadcast_calls == []


# ---------------------------------------------------------------------------
# generate_worker_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_worker_id_returns_candidate_when_no_collision():
    """First candidate is used when the DB has no row with that ID."""
    mock_db = AsyncMock()
    mock_db.exec = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=None)))

    with patch("worker_lifecycle.secrets.token_hex", return_value="abc123"):
        worker_id = await generate_worker_id(mock_db)

    assert worker_id == "w-abc123"
    mock_db.exec.assert_called_once()


@pytest.mark.asyncio
async def test_generate_worker_id_retries_on_collision():
    """A colliding candidate is discarded and a fresh one is drawn."""
    mock_db = AsyncMock()
    mock_db.exec = AsyncMock(
        side_effect=[
            MagicMock(one_or_none=MagicMock(return_value="w-aaa111")),
            MagicMock(one_or_none=MagicMock(return_value=None)),
        ]
    )

    with patch("worker_lifecycle.secrets.token_hex", side_effect=["aaa111", "bbb222"]):
        worker_id = await generate_worker_id(mock_db)

    assert worker_id == "w-bbb222"
    assert mock_db.exec.call_count == 2


@pytest.mark.asyncio
async def test_generate_worker_id_raises_after_max_attempts():
    """Persistent collisions raise instead of silently returning a duplicate ID."""
    mock_db = AsyncMock()
    mock_db.exec = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value="w-taken")))

    with (
        patch("worker_lifecycle.secrets.token_hex", return_value="taken0"),
        pytest.raises(RuntimeError, match="unique worker_id"),
    ):
        await generate_worker_id(mock_db)

    assert mock_db.exec.call_count == 5


# ---------------------------------------------------------------------------
# force_kill_worker_if_unresponsive
# ---------------------------------------------------------------------------


class _FakeStateDB:
    """Fake session whose exec().one_or_none() returns states from a queue, in order.

    Each call to exec() advances to the next queued state, so a single instance
    can simulate a worker observed across several consecutive polls (e.g. online,
    online, then offline) rather than always returning the same result.
    """

    def __init__(self, states):
        self._states = iter(states)
        self.call_count = 0

    async def exec(self, *a, **k):
        self.call_count += 1
        return MagicMock(one_or_none=MagicMock(return_value=next(self._states)))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_shutdown_force_kill_timeout_default():
    assert SHUTDOWN_FORCE_KILL_TIMEOUT == 1800.0


def test_shutdown_force_kill_timeout_env_override(monkeypatch):
    monkeypatch.setenv("PIONEER_SHUTDOWN_TIMEOUT", "45")
    import importlib

    import worker_lifecycle as wl

    try:
        importlib.reload(wl)
        assert wl.SHUTDOWN_FORCE_KILL_TIMEOUT == 45.0
    finally:
        monkeypatch.delenv("PIONEER_SHUTDOWN_TIMEOUT", raising=False)
        importlib.reload(wl)


@pytest.mark.asyncio
async def test_force_kill_if_unresponsive_noop_when_worker_goes_offline():
    """A worker that reports offline within the timeout is never force-killed."""
    session_iter = iter([_FakeStateDB(["offline"])])
    mock_kill = AsyncMock()

    with (
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle._force_kill_containers", mock_kill),
    ):
        await force_kill_worker_if_unresponsive("w-graceful", timeout=0.01)

    mock_kill.assert_not_called()


@pytest.mark.asyncio
async def test_force_kill_if_unresponsive_noop_when_worker_row_gone():
    """A worker row that's disappeared (e.g. deleted) is treated as already gone."""
    session_iter = iter([_FakeStateDB([None])])
    mock_kill = AsyncMock()

    with (
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle._force_kill_containers", mock_kill),
    ):
        await force_kill_worker_if_unresponsive("w-gone", timeout=0.01)

    mock_kill.assert_not_called()


@pytest.mark.asyncio
async def test_force_kill_if_unresponsive_escalates_after_timeout():
    """A worker that never goes offline gets force-killed once the timeout elapses."""
    # Second _FakeStateDB is consumed by the final re-check done immediately
    # before force-killing.
    session_iter = iter([_FakeStateDB(["working"]), _FakeStateDB(["working"])])
    mock_kill = AsyncMock()

    with (
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle._force_kill_containers", mock_kill),
    ):
        await force_kill_worker_if_unresponsive("w-wedged", timeout=0.01)

    mock_kill.assert_called_once_with({"w-wedged"})


@pytest.mark.asyncio
async def test_force_kill_if_unresponsive_skips_kill_when_offline_just_before_escalation():
    """The final re-check catches a worker that went offline right after the last poll,
    avoiding an unnecessary force-kill.
    """
    session_iter = iter([_FakeStateDB(["working"]), _FakeStateDB(["offline"])])
    mock_kill = AsyncMock()

    with (
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle._force_kill_containers", mock_kill),
    ):
        await force_kill_worker_if_unresponsive("w-just-in-time", timeout=0.01)

    mock_kill.assert_not_called()


@pytest.mark.asyncio
async def test_force_kill_if_unresponsive_polls_across_multiple_iterations():
    """A worker seen online for two polls and offline on the third is neither
    force-killed nor mistaken for offline early — each poll advances the state queue.
    """
    fake_db = _FakeStateDB(["working", "working", "offline"])
    mock_kill = AsyncMock()
    fake_loop = MagicMock()
    fake_loop.time = MagicMock(side_effect=[0.0, 5.0, 10.0, 12.0])

    with (
        patch("worker_lifecycle.AsyncSessionLocal", lambda: fake_db),
        patch("worker_lifecycle._force_kill_containers", mock_kill),
        patch("worker_lifecycle.asyncio.sleep", AsyncMock()),
        patch("worker_lifecycle.asyncio.get_event_loop", return_value=fake_loop),
    ):
        await force_kill_worker_if_unresponsive("w-slow-to-drain", timeout=12.0)

    assert fake_db.call_count == 3
    mock_kill.assert_not_called()


# ---------------------------------------------------------------------------
# reap_idle_workers_once
# ---------------------------------------------------------------------------


class _FakeReapDB:
    """Sequenced fake session: each exec() pops the next canned result."""

    def __init__(self, results):
        self._results = list(results)
        self.commit = AsyncMock()
        self.exec_count = 0

    async def exec(self, *a, **kw):
        self.exec_count += 1
        if self._results:
            return self._results.pop(0)
        return MagicMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _rows_result(rows):
    return MagicMock(all=MagicMock(return_value=rows))


def _first_result(value):
    return MagicMock(first=MagicMock(return_value=value))


def _scalar_result(value):
    return MagicMock(one_or_none=MagicMock(return_value=value))


def _old(seconds: float = 7200):
    from datetime import timedelta

    return datetime.now(UTC) - timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_reap_idle_worker_shuts_down_and_escalates():
    """An online spawned worker with no live work and no recent task-log activity is
    gracefully shut down, disabled, and given a force-kill backstop."""
    from worker_lifecycle import reap_idle_workers_once

    # Session 1: candidates. Session 2: inactivity checks (no active task, no
    # busy agent, stale last log) + the disabled update.
    sessions = [
        _FakeReapDB([_rows_result([("w-idle", "idle", _old(60), "g-guild")])]),
        _FakeReapDB(
            [_first_result(None), _first_result(None), _scalar_result(_old()), MagicMock()]
        ),
    ]
    session_iter = iter(sessions)
    broadcast = AsyncMock()
    terminal = AsyncMock()

    def _swallow_coro(coro, **kw):
        coro.close()  # avoid "coroutine never awaited" warnings
        return MagicMock()

    escalate = MagicMock(side_effect=_swallow_coro)

    with (
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle.broadcast_msg", broadcast),
        patch("worker_lifecycle.emit_terminal_line", terminal),
        patch("util.tasks.spawn", escalate),
    ):
        reaped = await reap_idle_workers_once()

    assert reaped == ["w-idle"]
    broadcast.assert_called_once()
    guild_slug, msg = broadcast.call_args.args
    assert guild_slug == "g-guild"
    assert msg.workerId == "w-idle"
    assert "idle" in (msg.reason or "")
    sessions[1].commit.assert_called_once()
    escalate.assert_called_once()


@pytest.mark.asyncio
async def test_reap_skips_worker_with_active_task():
    from worker_lifecycle import reap_idle_workers_once

    sessions = [
        _FakeReapDB([_rows_result([("w-busy", "idle", _old(60), "g-guild")])]),
        _FakeReapDB([_first_result("t-live")]),  # active pending/working task found
    ]
    session_iter = iter(sessions)
    broadcast = AsyncMock()

    with (
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle.broadcast_msg", broadcast),
    ):
        reaped = await reap_idle_workers_once()

    assert reaped == []
    broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_reap_skips_worker_with_busy_agent():
    from worker_lifecycle import reap_idle_workers_once

    sessions = [
        _FakeReapDB([_rows_result([("w-agent", "online", None, "g-guild")])]),
        _FakeReapDB([_first_result(None), _first_result("a-busy")]),
    ]
    session_iter = iter(sessions)
    broadcast = AsyncMock()

    with (
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle.broadcast_msg", broadcast),
    ):
        reaped = await reap_idle_workers_once()

    assert reaped == []
    broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_reap_skips_worker_with_recent_task_log():
    from worker_lifecycle import reap_idle_workers_once

    sessions = [
        _FakeReapDB([_rows_result([("w-recent", "idle", _old(60), "g-guild")])]),
        _FakeReapDB([_first_result(None), _first_result(None), _scalar_result(_old(seconds=30))]),
    ]
    session_iter = iter(sessions)
    broadcast = AsyncMock()

    with (
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle.broadcast_msg", broadcast),
    ):
        reaped = await reap_idle_workers_once()

    assert reaped == []
    broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_reap_offline_zombie_force_kills_directly():
    """An offline spawned worker whose last_seen is past the cutoff (or NULL) gets its
    container/ECS task force-killed without the graceful-drain dance."""
    from worker_lifecycle import reap_idle_workers_once

    sessions = [
        _FakeReapDB([_rows_result([("w-zombie", "offline", None, "g-guild")])]),
        _FakeReapDB([MagicMock()]),  # disabled update
    ]
    session_iter = iter(sessions)
    mock_kill = AsyncMock()
    broadcast = AsyncMock()

    with (
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle._force_kill_containers", mock_kill),
        patch("worker_lifecycle.broadcast_msg", broadcast),
    ):
        reaped = await reap_idle_workers_once()

    assert reaped == ["w-zombie"]
    mock_kill.assert_called_once_with({"w-zombie"})
    broadcast.assert_not_called()  # nothing to signal — it never connected


@pytest.mark.asyncio
async def test_reap_skips_offline_worker_seen_recently():
    """An offline worker seen recently may be mid-reconnect after a backend restart —
    leave it alone."""
    from worker_lifecycle import reap_idle_workers_once

    sessions = [
        _FakeReapDB([_rows_result([("w-reconnecting", "offline", _old(seconds=30), "g-g")])]),
    ]
    session_iter = iter(sessions)
    mock_kill = AsyncMock()

    with (
        patch("worker_lifecycle.AsyncSessionLocal", lambda: next(session_iter)),
        patch("worker_lifecycle._force_kill_containers", mock_kill),
    ):
        reaped = await reap_idle_workers_once()

    assert reaped == []
    mock_kill.assert_not_called()


@pytest.mark.asyncio
async def test_reap_disabled_when_timeout_zero():
    from worker_lifecycle import reap_idle_workers_once

    with (
        patch("worker_lifecycle.WORKER_IDLE_TIMEOUT", 0),
        patch("worker_lifecycle.AsyncSessionLocal") as mock_session,
    ):
        reaped = await reap_idle_workers_once()

    assert reaped == []
    mock_session.assert_not_called()
