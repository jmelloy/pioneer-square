"""Tests for per-guild asyncio.Lock in run_foreman_ai.

Verifies that concurrent invocations for the same guild are dropped (not
queued) while invocations for different guilds proceed independently.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch


async def _run_foreman_ai_patched(guild_id: str, impl_event: asyncio.Event | None = None):
    """Call run_foreman_ai with _run_foreman_ai stubbed out.

    If *impl_event* is given, the stub waits for it before returning so the
    lock stays held for the duration of the test's choice.
    """
    import foreman.runner as runner

    async def _slow_impl(gid, msg, extra="", uid=None, task_id=None, child=False):
        if impl_event is not None:
            await impl_event.wait()

    with patch.object(runner, "_run_foreman_ai", side_effect=_slow_impl):
        await runner.run_foreman_ai(guild_id, "test message")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_concurrent_same_guild_drops_second():
    """A second run_foreman_ai call while the first is still running is dropped."""

    async def _test():
        import foreman.runner as runner

        # Reset lock state between tests
        runner._guild_locks.clear()

        call_count = 0
        hold = asyncio.Event()

        async def _impl(gid, msg, extra="", uid=None, task_id=None, child=False):
            nonlocal call_count
            call_count += 1
            await hold.wait()

        with patch.object(runner, "_run_foreman_ai", side_effect=_impl):
            # Start first call — it acquires the lock and waits on hold.
            first = asyncio.create_task(runner.run_foreman_ai("g1", "msg1"))
            # Yield so the first task runs and acquires the lock.
            await asyncio.sleep(0)

            # Second call should see the lock is held and drop.
            await runner.run_foreman_ai("g1", "msg2")

            # Unblock the first call.
            hold.set()
            await first

        assert call_count == 1, f"Expected 1 _run_foreman_ai call, got {call_count}"

    _run(_test())


def test_concurrent_different_guilds_both_run():
    """Concurrent runs for different guilds both proceed without interference."""

    async def _test():
        import foreman.runner as runner

        runner._guild_locks.clear()

        call_log: list[str] = []
        hold = asyncio.Event()

        async def _impl(gid, msg, extra="", uid=None, task_id=None, child=False):
            call_log.append(gid)
            await hold.wait()

        with patch.object(runner, "_run_foreman_ai", side_effect=_impl):
            t1 = asyncio.create_task(runner.run_foreman_ai("guild-a", "msg"))
            t2 = asyncio.create_task(runner.run_foreman_ai("guild-b", "msg"))
            await asyncio.sleep(0)
            hold.set()
            await asyncio.gather(t1, t2)

        assert sorted(call_log) == ["guild-a", "guild-b"]

    _run(_test())


def test_lock_released_after_completion():
    """After run_foreman_ai finishes, the lock is free for the next call."""

    async def _test():
        import foreman.runner as runner

        runner._guild_locks.clear()

        call_count = 0

        async def _impl(gid, msg, extra="", uid=None, task_id=None, child=False):
            nonlocal call_count
            call_count += 1

        with patch.object(runner, "_run_foreman_ai", side_effect=_impl):
            await runner.run_foreman_ai("g2", "first")
            await runner.run_foreman_ai("g2", "second")

        assert call_count == 2, "Both sequential calls should run"

    _run(_test())


def test_lock_released_after_impl_exception():
    """The lock is released even when _run_foreman_ai raises."""

    async def _test():
        import foreman.runner as runner

        runner._guild_locks.clear()

        call_count = 0

        async def _impl(gid, msg, extra="", uid=None, task_id=None, child=False):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")

        with patch.object(runner, "_run_foreman_ai", side_effect=_impl):
            try:
                await runner.run_foreman_ai("g3", "first")
            except RuntimeError:
                pass
            # Lock must be free now; second call should run.
            await runner.run_foreman_ai("g3", "second")

        assert call_count == 2

    _run(_test())


def test_human_message_queued_when_busy_then_drained():
    """A human message arriving while busy is queued, not dropped, and runs after."""

    async def _test():
        import foreman.runner as runner

        runner._guild_locks.clear()
        runner._human_queues.clear()

        call_log: list[str] = []
        hold = asyncio.Event()

        async def _impl(gid, msg, extra="", uid=None, task_id=None, child=False):
            call_log.append(msg)
            if len(call_log) == 1:
                await hold.wait()

        with patch.object(runner, "_run_foreman_ai", side_effect=_impl):
            first = asyncio.create_task(runner.run_foreman_ai("g4", "first", is_human=True))
            await asyncio.sleep(0)

            # Second human message while busy: queued, not dropped.
            await runner.run_foreman_ai("g4", "second", is_human=True)
            assert call_log == ["first"], "queued message must not run until the lock frees up"
            assert len(runner._human_queues[("g4", None)]) == 1

            hold.set()
            await first

        assert len(call_log) == 2
        assert "second" in call_log[1]
        # Queue is drained (and its entry removed) once processed.
        assert ("g4", None) not in runner._human_queues

    _run(_test())


def test_automated_still_drops_while_human_queue_exists():
    """is_human=False keeps drop-if-busy even though human messages now queue."""

    async def _test():
        import foreman.runner as runner

        runner._guild_locks.clear()
        runner._human_queues.clear()

        call_log: list[str] = []
        hold = asyncio.Event()

        async def _impl(gid, msg, extra="", uid=None, task_id=None, child=False):
            call_log.append(msg)
            if len(call_log) == 1:
                await hold.wait()

        with patch.object(runner, "_run_foreman_ai", side_effect=_impl):
            first = asyncio.create_task(runner.run_foreman_ai("g5", "first", is_human=True))
            await asyncio.sleep(0)

            # An automated trigger (e.g. periodic-check) while busy: dropped, not queued.
            await runner.run_foreman_ai("g5", "periodic", is_human=False)
            assert call_log == ["first"]
            assert ("g5", None) not in runner._human_queues

            hold.set()
            await first

        assert call_log == ["first"], "dropped automated trigger must never run"

    _run(_test())


def test_human_queue_bounded_drops_oldest():
    """The human queue is capped; overflow drops the oldest queued entry."""

    async def _test():
        import foreman.runner as runner

        runner._guild_locks.clear()
        runner._human_queues.clear()

        hold = asyncio.Event()

        async def _impl(gid, msg, extra="", uid=None, task_id=None, child=False):
            await hold.wait()

        with patch.object(runner, "_run_foreman_ai", side_effect=_impl):
            first = asyncio.create_task(runner.run_foreman_ai("g6", "first", is_human=True))
            await asyncio.sleep(0)

            max_queue = runner._HUMAN_QUEUE_MAX
            for i in range(max_queue + 1):
                await runner.run_foreman_ai("g6", f"queued-{i}", is_human=True)

            queue = runner._human_queues[("g6", None)]
            assert len(queue) == max_queue, "queue must not grow past the configured max"
            # The oldest entry (queued-0) was dropped to make room for the newest.
            assert queue[0].human_message == "queued-1"
            assert queue[-1].human_message == f"queued-{max_queue}"

            # Unblocking lets "first" finish and the drain loop process (and
            # empty) the whole backlog, since hold is already set by then.
            hold.set()
            await first

        assert ("g6", None) not in runner._human_queues

    _run(_test())
