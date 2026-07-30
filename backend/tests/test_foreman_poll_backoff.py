"""Tests for the poll-loop backoff behaviour (issue #556).

Verifies that:
- reset_foreman_poll only resets the interval to POLL_MIN_SECS when the foreman
  was recently active (made tool calls within POLL_MIN_SECS seconds).
- reset_foreman_poll does not restart an existing loop if the foreman is idle.
- reset_foreman_poll is a no-op when a foreman run is already in-flight.
- _record_guild_action and _guild_active_recently behave correctly.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _record_guild_action / _guild_active_recently helpers
# ---------------------------------------------------------------------------


def test_record_guild_action_sets_timestamp():
    import foreman.runner as runner

    runner._guild_last_action_at.pop("g-ts", None)
    before = time.monotonic()
    runner._record_guild_action("g-ts")
    after = time.monotonic()

    ts = runner._guild_last_action_at["g-ts"]
    assert before <= ts <= after


def test_guild_active_recently_true_for_fresh_action():
    import foreman.runner as runner

    runner._guild_last_action_at["g-fresh"] = time.monotonic()
    assert runner._guild_active_recently("g-fresh") is True


def test_guild_active_recently_false_for_old_action():
    import foreman.runner as runner

    # Simulate an action that happened long before POLL_MIN_SECS
    runner._guild_last_action_at["g-old"] = time.monotonic() - (runner.POLL_MIN_SECS * 2)
    assert runner._guild_active_recently("g-old") is False


def test_guild_active_recently_false_for_unknown_guild():
    import foreman.runner as runner

    runner._guild_last_action_at.pop("g-never", None)
    assert runner._guild_active_recently("g-never") is False


# ---------------------------------------------------------------------------
# reset_foreman_poll — in-flight debounce
# ---------------------------------------------------------------------------


def test_reset_poll_skips_when_run_in_flight():
    """reset_foreman_poll must not restart the loop when a run is in-flight."""

    async def _test():
        import foreman.runner as runner

        guild = "g-inflight"
        runner._guild_locks.clear()
        runner._poll_tasks.pop(guild, None)
        runner._guild_last_action_at[guild] = time.monotonic()  # recently active

        # Mark the slot busy, as if a run is in progress.
        runner._guild_locks[(guild, None)] = runner._GuildRunLock(busy=True)

        spawned = []
        with patch.object(runner, "spawn", side_effect=lambda c, **kw: spawned.append(c)):
            runner.reset_foreman_poll(guild)

        assert spawned == [], "spawn must not be called while a run is in-flight"

    _run(_test())


# ---------------------------------------------------------------------------
# reset_foreman_poll — idle guard (no recent tool calls)
# ---------------------------------------------------------------------------


def test_reset_poll_does_not_restart_existing_loop_when_idle():
    """When foreman is idle, an existing running poll loop must not be restarted."""

    async def _test():
        import foreman.runner as runner

        guild = "g-idle-existing"
        runner._guild_locks.clear()
        runner._guild_last_action_at.pop(guild, None)  # never active

        # Install a fake task that is *not* done.
        fake_task = MagicMock()
        fake_task.done.return_value = False
        runner._poll_tasks[guild] = fake_task

        spawned = []
        with patch.object(runner, "spawn", side_effect=lambda c, **kw: spawned.append(c)):
            runner.reset_foreman_poll(guild)

        assert spawned == [], "existing loop must not be cancelled/restarted when idle"
        # The fake task must not have been cancelled.
        fake_task.cancel.assert_not_called()

    _run(_test())


def test_reset_poll_starts_loop_when_none_exists_and_idle():
    """When foreman is idle but no loop exists, reset_foreman_poll must start one."""

    async def _test():
        import foreman.runner as runner

        guild = "g-idle-no-loop"
        runner._guild_locks.clear()
        runner._guild_last_action_at.pop(guild, None)  # never active
        runner._poll_tasks.pop(guild, None)  # no existing loop

        fake_task = MagicMock()
        spawned = []

        def _fake_spawn(coro, **kw):
            spawned.append(coro)
            return fake_task

        with patch.object(runner, "spawn", side_effect=_fake_spawn):
            runner.reset_foreman_poll(guild)

        assert len(spawned) == 1, "should start a new loop when none exists"
        assert runner._poll_tasks[guild] is fake_task

    _run(_test())


# ---------------------------------------------------------------------------
# reset_foreman_poll — active reset
# ---------------------------------------------------------------------------


def test_reset_poll_cancels_and_restarts_when_recently_active():
    """When foreman was recently active, the poll loop is restarted at POLL_MIN_SECS."""

    async def _test():
        import foreman.runner as runner

        guild = "g-active-reset"
        runner._guild_locks.clear()
        runner._guild_last_action_at[guild] = time.monotonic()  # just made tool calls

        # Install a fake running task.
        old_task = MagicMock()
        old_task.done.return_value = False
        runner._poll_tasks[guild] = old_task

        new_task = MagicMock()
        spawned = []

        def _fake_spawn(coro, **kw):
            spawned.append(coro)
            return new_task

        with patch.object(runner, "spawn", side_effect=_fake_spawn):
            runner.reset_foreman_poll(guild)

        old_task.cancel.assert_called_once()
        assert len(spawned) == 1, "should spawn a new poll loop"
        assert runner._poll_tasks[guild] is new_task

    _run(_test())


# ---------------------------------------------------------------------------
# _run_foreman_ai records action when tool calls are made
# ---------------------------------------------------------------------------


def test_run_foreman_ai_records_action_on_tool_calls():
    """_run_foreman_ai must call _record_guild_action when tool_uses are present."""

    async def _test():
        import foreman.runner as runner

        guild = "g-action-record"
        runner._guild_last_action_at.pop(guild, None)

        # Build a minimal fake response with one tool_use block
        tool_use_block = MagicMock()
        tool_use_block.type = "tool_use"
        tool_use_block.name = "get_task_status"
        tool_use_block.id = "tu-1"
        tool_use_block.input = {"task_id": "t-1"}
        # _serialize_content calls b.model_dump(); return a proper dict so json.dumps works.
        tool_use_block.model_dump.return_value = {
            "type": "tool_use",
            "id": "tu-1",
            "name": "get_task_status",
            "input": {"task_id": "t-1"},
        }

        end_turn_block = MagicMock()
        end_turn_block.type = "text"
        end_turn_block.text = "done"
        end_turn_block.model_dump.return_value = {"type": "text", "text": "done"}

        first_resp = MagicMock()
        first_resp.stop_reason = "tool_use"
        first_resp.content = [tool_use_block]
        first_resp.usage = MagicMock(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )

        second_resp = MagicMock()
        second_resp.stop_reason = "end_turn"
        second_resp.content = [end_turn_block]
        second_resp.usage = MagicMock(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )

        call_count = 0

        async def _fake_create(**kwargs):
            nonlocal call_count
            call_count += 1
            resp = first_resp if call_count == 1 else second_resp
            raw = MagicMock()
            raw.parse.return_value = resp
            raw.headers = {"request-id": "req-1"}
            return raw

        fake_client = MagicMock()
        fake_client.messages.with_raw_response.create = _fake_create

        from unittest.mock import AsyncMock

        with (
            patch.object(runner, "_get_anthropic_client", return_value=fake_client),
            patch.object(runner, "HAS_ANTHROPIC", True),
            patch.object(runner, "_get_guild_user_id", AsyncMock(return_value="u-1")),
            patch.object(runner, "_load_foreman_config", AsyncMock(return_value={})),
            patch.object(runner, "_save_turn", AsyncMock(return_value=1)),
            patch.object(runner, "_load_history", AsyncMock(return_value=[])),
            patch.object(runner, "_create_api_request_log", AsyncMock(return_value=1)),
            patch.object(runner, "_complete_api_request_log", AsyncMock()),
            patch.object(runner, "broadcast_msg", AsyncMock()),
            patch.object(
                runner,
                "exec_tools",
                AsyncMock(
                    return_value=[
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-1",
                            "content": "ok",
                        }
                    ]
                ),
            ),
            patch.object(runner, "_fetch_online_workers", AsyncMock(return_value=[])),
            patch("foreman.runner.get_db") as mock_get_db,
            patch("foreman.runner.get_guild_pk", AsyncMock(return_value=1)),
        ):
            mock_db = AsyncMock()
            mock_db.close = AsyncMock()
            mock_db.commit = AsyncMock()
            mock_db.add = MagicMock()
            mock_db.rollback = AsyncMock()
            mock_db.exec = AsyncMock(
                return_value=MagicMock(
                    one_or_none=MagicMock(return_value=None),
                    all=MagicMock(return_value=[]),
                )
            )
            mock_get_db.return_value = mock_db

            await runner._run_foreman_ai(guild, "check tasks")

        assert guild in runner._guild_last_action_at, "_record_guild_action should have been called"
        assert runner._guild_active_recently(guild) is True

    _run(_test())


# ---------------------------------------------------------------------------
# ensure_poll_loop — starts a loop but never resets the backoff
# ---------------------------------------------------------------------------


def test_ensure_poll_loop_starts_when_none_exists():
    """ensure_poll_loop spawns a loop when the guild has none."""

    async def _test():
        import foreman.runner as runner

        guild = "g-ensure-new"
        runner._poll_tasks.pop(guild, None)

        fake_task = MagicMock()
        spawned = []

        def _fake_spawn(coro, **kw):
            spawned.append(coro)
            coro.close()  # avoid "coroutine never awaited" warning
            return fake_task

        with patch.object(runner, "spawn", side_effect=_fake_spawn):
            runner.ensure_poll_loop(guild)

        assert len(spawned) == 1
        assert runner._poll_tasks[guild] is fake_task

    _run(_test())


def test_ensure_poll_loop_leaves_existing_loop_even_when_recently_active():
    """The whole point of the decoupling: automated events must NOT reset the
    backoff. Even with a fresh action timestamp, an existing running loop is left
    untouched — no cancel, no respawn."""

    async def _test():
        import foreman.runner as runner

        guild = "g-ensure-existing"
        runner._guild_last_action_at[guild] = time.monotonic()  # recently active

        old_task = MagicMock()
        old_task.done.return_value = False
        runner._poll_tasks[guild] = old_task

        with patch.object(runner, "spawn", side_effect=AssertionError("must not spawn")):
            runner.ensure_poll_loop(guild)

        old_task.cancel.assert_not_called()
        assert runner._poll_tasks[guild] is old_task

    _run(_test())
