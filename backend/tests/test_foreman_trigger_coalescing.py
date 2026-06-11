"""Tests for guild-level spawned foreman trigger coalescing."""

from __future__ import annotations

import asyncio
from unittest.mock import patch


def _run(coro):
    return asyncio.run(coro)


def test_schedule_coalesces_to_one_pending_rerun():
    async def _test():
        import foreman.runner as runner

        runner._trigger_inflight_keys.clear()
        runner._trigger_pending_by_key.clear()

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
        assert ("g1", None) not in runner._trigger_inflight_keys
        assert ("g1", None) not in runner._trigger_pending_by_key

    _run(_test())


def test_schedule_runs_different_guilds_in_parallel():
    async def _test():
        import foreman.runner as runner

        runner._trigger_inflight_keys.clear()
        runner._trigger_pending_by_key.clear()

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


def test_schedule_runs_same_guild_different_users_in_parallel():
    async def _test():
        import foreman.runner as runner

        runner._trigger_inflight_keys.clear()
        runner._trigger_pending_by_key.clear()

        seen: list[tuple[str, str | None]] = []
        hold = asyncio.Event()

        async def _impl(gid, msg, extra_context="", user_id=None):
            seen.append((gid, user_id))
            await hold.wait()

        with patch.object(runner, "run_foreman_ai", side_effect=_impl):
            runner.schedule_foreman_run("guild-a", "u1", task_name="foreman.u1", user_id="user-1")
            runner.schedule_foreman_run("guild-a", "u2", task_name="foreman.u2", user_id="user-2")
            await asyncio.sleep(0)
            hold.set()
            await asyncio.sleep(0)

        assert sorted(seen) == [("guild-a", "user-1"), ("guild-a", "user-2")]

    _run(_test())
