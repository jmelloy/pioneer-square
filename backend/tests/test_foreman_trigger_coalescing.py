"""Tests for guild-level spawned foreman trigger coalescing."""

from __future__ import annotations

import asyncio
from unittest.mock import patch


def _run(coro):
    return asyncio.run(coro)


def test_schedule_coalesces_to_one_pending_rerun():
    async def _test():
        import foreman.runner as runner

        runner._trigger_inflight_guilds.clear()
        runner._trigger_pending_by_guild.clear()

        calls: list[str] = []
        hold = asyncio.Event()

        async def _impl(gid, msg, extra_context="", user_id=None):
            calls.append(msg)
            if len(calls) == 1:
                await hold.wait()

        with patch.object(runner, "run_foreman_ai", side_effect=_impl):
            runner.schedule_foreman_run("g1", "first", task_name="foreman.t1")
            await asyncio.sleep(0)
            runner.schedule_foreman_run("g1", "second", task_name="foreman.t2")
            runner.schedule_foreman_run("g1", "third", task_name="foreman.t3")
            await asyncio.sleep(0)

            assert calls == ["first"]

            hold.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert calls == ["first", "third"]
        assert "g1" not in runner._trigger_inflight_guilds
        assert "g1" not in runner._trigger_pending_by_guild

    _run(_test())


def test_schedule_runs_different_guilds_in_parallel():
    async def _test():
        import foreman.runner as runner

        runner._trigger_inflight_guilds.clear()
        runner._trigger_pending_by_guild.clear()

        seen: list[str] = []
        hold = asyncio.Event()

        async def _impl(gid, msg, extra_context="", user_id=None):
            seen.append(gid)
            await hold.wait()

        with patch.object(runner, "run_foreman_ai", side_effect=_impl):
            runner.schedule_foreman_run("guild-a", "a", task_name="foreman.a")
            runner.schedule_foreman_run("guild-b", "b", task_name="foreman.b")
            await asyncio.sleep(0)
            hold.set()
            await asyncio.sleep(0)

        assert sorted(seen) == ["guild-a", "guild-b"]

    _run(_test())
