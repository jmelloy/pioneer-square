"""An abort must return the agent slot to idle.

The backend routes follow-ups only to agents whose state is ``idle``
(``_select_followup_worker``), and the only thing that clears an agent's state
is running another task. So an agent left in ``error`` can never be handed the
work that would clear it: one repo that reliably fails to clone would retire the
worker's slots one at a time until it looks online but is unroutable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pioneer_worker.config import Config
from pioneer_worker.worker import Worker


def _make_cfg(**kwargs) -> Config:
    return Config(
        backend_url="ws://localhost:8000",
        guild_id="test-guild",
        worker_id="w-test01",
        max_agents=1,
        pull_interval=3600.0,
        **kwargs,
    )


def _states_sent(worker: Worker) -> list[str]:
    return [
        m.args[0]["state"]
        for m in worker._send.await_args_list
        if m.args and m.args[0].get("type") == "agent-state"
    ]


@pytest.mark.asyncio
async def test_worktree_failure_returns_the_slot_to_idle(tmp_path):
    worker = Worker(_make_cfg(work_dir=str(tmp_path), repos=["org/myrepo"]))
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()

    task = {"id": "t-noclone", "description": "do something", "name": "do something"}

    with (
        # Clone fails, so no worktree is ever created for the task.
        patch("pioneer_worker.worker.git_ops.ensure_repo", new=AsyncMock(return_value=None)),
        patch("pioneer_worker.worker.Worker._task_github_token", new=AsyncMock(return_value=None)),
    ):
        await worker._execute_task(task, worker.agents[0])

    states = _states_sent(worker)
    assert "error" in states, states
    assert states[-1] == "idle", states
    assert worker.agents[0].state == "idle"


@pytest.mark.asyncio
async def test_unavailable_tool_returns_the_slot_to_idle(tmp_path):
    """The sibling abort path, pinned so the two stay consistent."""
    worker = Worker(_make_cfg(work_dir=str(tmp_path), repos=["org/myrepo"]))
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()
    worker._available_tools = ["pi"]

    task = {"id": "t-notool", "description": "x", "name": "x", "tool": "claude"}

    with patch("pioneer_worker.worker.Worker._task_github_token", new=AsyncMock(return_value=None)):
        await worker._execute_task(task, worker.agents[0])

    assert _states_sent(worker)[-1] == "idle"
    assert worker.agents[0].state == "idle"
