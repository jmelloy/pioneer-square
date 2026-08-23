"""ASG scale-in protection: enabled while any agent is busy, released once idle.

Regression covered here: the warm ASG worker fleet (terraform/asg_workers.tf)
had a lifecycle hook that pauses termination until a picked instance drains,
but nothing stopped the ASG from picking a *busy* instance for scale-in in
the first place, since no code ever called SetInstanceProtection. See
worker/pioneer_worker/asg_protection.py and Worker._sync_scale_in_protection
in worker.py (hooked into every _set_state transition).

``Worker._set_state`` fires the AWS call as a fire-and-forget background
task, so these tests call ``worker._sync_scale_in_protection()`` directly and
await the task it returns instead of racing the event loop.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from pioneer_worker import asg_protection
from pioneer_worker.config import Config
from pioneer_worker.worker import Worker


def _make_worker(max_agents: int = 2) -> Worker:
    cfg = Config(
        backend_url="ws://localhost:8000",
        guild_id="g-test",
        worker_id="w-test01",
        max_agents=max_agents,
        pull_interval=3600.0,
    )
    worker = Worker(cfg)
    worker._send = AsyncMock()
    return worker


async def _set_state_and_wait(worker: Worker, state: str, agent) -> None:
    """Set an agent's state and wait for any resulting protection call to land."""
    agent.state = state
    task = worker._sync_scale_in_protection()
    if task is not None:
        await task


class TestIsAsgWorker:
    def test_false_without_env_var(self, monkeypatch):
        monkeypatch.delenv("PIONEER_ASG_NAME", raising=False)
        assert asg_protection.is_asg_worker() is False

    def test_true_with_env_var(self, monkeypatch):
        monkeypatch.setenv("PIONEER_ASG_NAME", "pioneer-worker-asg")
        assert asg_protection.is_asg_worker() is True


class TestSetScaleInProtection:
    async def test_noop_without_asg_name(self, monkeypatch):
        monkeypatch.delenv("PIONEER_ASG_NAME", raising=False)
        called = False

        def _boom(**kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(asg_protection, "_set_protection_sync", _boom)
        await asg_protection.set_scale_in_protection("i-0123", True)
        assert called is False

    async def test_calls_boto3_with_expected_args(self, monkeypatch):
        monkeypatch.setenv("PIONEER_ASG_NAME", "pioneer-worker-asg")
        calls = []

        def _record(**kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(asg_protection, "_set_protection_sync", _record)
        await asg_protection.set_scale_in_protection("i-0123", True)

        assert calls == [
            {"asg_name": "pioneer-worker-asg", "instance_id": "i-0123", "protect": True}
        ]

    async def test_swallows_failures(self, monkeypatch):
        monkeypatch.setenv("PIONEER_ASG_NAME", "pioneer-worker-asg")

        def _raise(**kwargs):
            raise RuntimeError("AWS is down")

        monkeypatch.setattr(asg_protection, "_set_protection_sync", _raise)
        # Must not raise — this is best-effort and must never affect task execution.
        await asg_protection.set_scale_in_protection("i-0123", True)


class TestWorkerSyncScaleInProtection:
    async def test_noop_off_asg_fleet(self, monkeypatch):
        """Without PIONEER_ASG_NAME, agent transitions never touch AWS at all."""
        monkeypatch.delenv("PIONEER_ASG_NAME", raising=False)
        worker = _make_worker()
        mock_set = AsyncMock()
        monkeypatch.setattr(asg_protection, "set_scale_in_protection", mock_set)

        await _set_state_and_wait(worker, "working", worker.agents[0])

        mock_set.assert_not_called()
        assert worker._scale_in_protected is False

    async def test_first_task_enables_protection(self, monkeypatch):
        monkeypatch.setenv("PIONEER_ASG_NAME", "pioneer-worker-asg")
        worker = _make_worker()
        mock_set = AsyncMock()
        monkeypatch.setattr(asg_protection, "set_scale_in_protection", mock_set)

        await _set_state_and_wait(worker, "working", worker.agents[0])

        mock_set.assert_awaited_once_with(worker._hostname(), True)
        assert worker._scale_in_protected is True

    async def test_second_busy_agent_does_not_call_again(self, monkeypatch):
        monkeypatch.setenv("PIONEER_ASG_NAME", "pioneer-worker-asg")
        worker = _make_worker(max_agents=2)
        mock_set = AsyncMock()
        monkeypatch.setattr(asg_protection, "set_scale_in_protection", mock_set)

        await _set_state_and_wait(worker, "working", worker.agents[0])
        await _set_state_and_wait(worker, "working", worker.agents[1])

        mock_set.assert_awaited_once_with(worker._hostname(), True)

    async def test_disables_only_once_all_agents_idle(self, monkeypatch):
        monkeypatch.setenv("PIONEER_ASG_NAME", "pioneer-worker-asg")
        worker = _make_worker(max_agents=2)
        mock_set = AsyncMock()
        monkeypatch.setattr(asg_protection, "set_scale_in_protection", mock_set)

        await _set_state_and_wait(worker, "working", worker.agents[0])
        await _set_state_and_wait(worker, "working", worker.agents[1])
        await _set_state_and_wait(worker, "idle", worker.agents[0])

        # One agent still working — must remain protected, no disable call yet.
        assert worker._scale_in_protected is True
        mock_set.assert_awaited_once_with(worker._hostname(), True)

        await _set_state_and_wait(worker, "idle", worker.agents[1])

        assert worker._scale_in_protected is False
        assert mock_set.await_args_list[-1].args == (worker._hostname(), False)
