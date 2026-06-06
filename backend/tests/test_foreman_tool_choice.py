"""Tests for required_tool / tool_choice enforcement in the foreman runner (issue #602).

Verifies that:
- When required_tool is set, the API call includes tool_choice on round 0.
- When the model returns no tool call despite required_tool being set, a RuntimeError is raised.
- When the model returns a valid tool call, execution proceeds normally.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    return asyncio.run(coro)


def _mock_db_session() -> MagicMock:
    """Return a mock that quacks like an AsyncSession for runner tests."""
    result = MagicMock()
    result.one_or_none = MagicMock(return_value=None)
    result.all = MagicMock(return_value=[])

    session = MagicMock()
    session.exec = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    return session


def _make_text_response(text: str = "I'll think about this.") -> MagicMock:
    """Mock Anthropic response containing only a text block (no tool call)."""
    block = MagicMock()
    block.type = "text"
    block.text = text

    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.cache_read_input_tokens = 0
    usage.cache_creation_input_tokens = 0

    resp = MagicMock()
    resp.content = [block]
    resp.stop_reason = "end_turn"
    resp.usage = usage
    resp.model_dump = MagicMock(return_value={"stop_reason": "end_turn"})

    raw = MagicMock()
    raw.parse = MagicMock(return_value=resp)
    raw.headers = {"request-id": "req-test-123"}
    return raw


def _make_tool_use_response(tool_name: str = "send_followup") -> MagicMock:
    """Mock Anthropic response containing a tool_use block."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = tool_name
    tool_block.id = "toolu_test_abc"
    tool_block.input = {"task_id": "t-test", "instructions": "continue"}

    usage = MagicMock()
    usage.input_tokens = 20
    usage.output_tokens = 10
    usage.cache_read_input_tokens = 0
    usage.cache_creation_input_tokens = 0

    resp = MagicMock()
    resp.content = [tool_block]
    resp.stop_reason = "tool_use"
    resp.usage = usage
    resp.model_dump = MagicMock(return_value={"stop_reason": "tool_use"})

    raw = MagicMock()
    raw.parse = MagicMock(return_value=resp)
    raw.headers = {"request-id": "req-test-456"}
    return raw


def _apply_runner_patches(stack: ExitStack, client_mock=None, exec_tools_mock=None) -> MagicMock:
    """Enter all standard patches needed for _run_foreman_ai tests into an ExitStack."""
    if client_mock is None:
        client_mock = MagicMock()

    db = _mock_db_session()

    stack.enter_context(patch("foreman.runner._get_anthropic_client", return_value=client_mock))
    stack.enter_context(patch("foreman.runner.get_foreman_model", return_value="claude-test"))
    stack.enter_context(
        patch("foreman.runner._load_foreman_config", new=AsyncMock(return_value={}))
    )
    stack.enter_context(
        patch("foreman.runner._get_guild_user_id", new=AsyncMock(return_value="user-1"))
    )
    stack.enter_context(patch("foreman.runner.get_db", new=AsyncMock(return_value=db)))
    stack.enter_context(patch("foreman.runner.get_guild_pk", new=AsyncMock(return_value=1)))
    stack.enter_context(
        patch("foreman.runner._fetch_online_workers", new=AsyncMock(return_value=[]))
    )
    stack.enter_context(
        patch(
            "foreman.runner._load_history",
            new=AsyncMock(return_value=[{"role": "user", "content": "test message"}]),
        )
    )
    stack.enter_context(patch("foreman.runner._save_turn", new=AsyncMock(return_value=1)))
    stack.enter_context(
        patch("foreman.runner._create_api_request_log", new=AsyncMock(return_value=42))
    )
    stack.enter_context(patch("foreman.runner._complete_api_request_log", new=AsyncMock()))
    stack.enter_context(patch("foreman.runner.broadcast_msg", new=AsyncMock()))
    stack.enter_context(patch("foreman.runner.prune_history", side_effect=lambda m: m))
    stack.enter_context(
        patch("foreman.runner.strip_orphaned_tool_results", side_effect=lambda m: m)
    )
    stack.enter_context(patch("foreman.runner._stamp_message_cache_breakpoint"))
    stack.enter_context(
        patch("foreman.runner.build_system_blocks", return_value=[{"type": "text", "text": "sys"}])
    )
    stack.enter_context(patch("foreman.runner.build_state_preamble", return_value="preamble"))
    stack.enter_context(patch("foreman.runner.build_system_prompt", return_value="audit"))
    stack.enter_context(patch("foreman.runner._inject_state_preamble"))
    stack.enter_context(
        patch("foreman.runner._serialize_content", side_effect=lambda c: json.dumps(str(c)))
    )
    stack.enter_context(patch("foreman.runner._summarize_task", return_value=None))
    stack.enter_context(patch("foreman.runner.HAS_ANTHROPIC", True))
    stack.enter_context(patch("foreman.runner.Message"))
    stack.enter_context(patch("foreman.runner.live_tasks_filter", return_value=True))
    if exec_tools_mock is not None:
        stack.enter_context(patch("foreman.runner.exec_tools", exec_tools_mock))
        stack.enter_context(patch("foreman.runner.truncate_tool_result", side_effect=lambda x: x))
    return client_mock


# ---------------------------------------------------------------------------
# Tool-choice enforcement: API call must include tool_choice when required_tool set
# ---------------------------------------------------------------------------


def test_required_tool_passes_tool_choice_to_api():
    """When required_tool is set, the first API call must include tool_choice."""
    api_calls = []
    tool_result = {"type": "tool_result", "tool_use_id": "toolu_test_abc", "content": "ok"}
    exec_tools_mock = AsyncMock(return_value=[tool_result])

    # Round 0 returns a tool_use; round 1 returns text to exit the loop.
    responses = [_make_tool_use_response("send_followup"), _make_text_response()]
    call_idx = [0]

    async def _side_effect(**kwargs):
        api_calls.append(kwargs)
        idx = call_idx[0]
        call_idx[0] += 1
        return responses[min(idx, len(responses) - 1)]

    client_mock = MagicMock()
    client_mock.messages.with_raw_response.create = AsyncMock(side_effect=_side_effect)

    with ExitStack() as stack:
        _apply_runner_patches(stack, client_mock=client_mock, exec_tools_mock=exec_tools_mock)
        from foreman import runner as fr

        _run(fr._run_foreman_ai("guild1", "continue please", required_tool="send_followup"))

    assert api_calls, "API was never called"
    first_call = api_calls[0]
    assert "tool_choice" in first_call, "tool_choice must be present in round-0 API call"
    assert first_call["tool_choice"] == {"type": "tool", "name": "send_followup"}


def test_no_required_tool_omits_tool_choice():
    """When required_tool is None, the API call must NOT include tool_choice."""
    api_calls = []

    async def _capture_create(**kwargs):
        api_calls.append(kwargs)
        return _make_text_response()

    client_mock = MagicMock()
    client_mock.messages.with_raw_response.create = AsyncMock(side_effect=_capture_create)

    with ExitStack() as stack:
        _apply_runner_patches(stack, client_mock=client_mock)
        from foreman import runner as fr

        _run(fr._run_foreman_ai("guild1", "just chatting", required_tool=None))

    assert api_calls, "API was never called"
    assert "tool_choice" not in api_calls[0], (
        "tool_choice must NOT be set when required_tool is None"
    )


# ---------------------------------------------------------------------------
# Error enforcement: RuntimeError when model ignores required_tool
# ---------------------------------------------------------------------------


def test_required_tool_raises_when_model_returns_text_only():
    """When required_tool is set and the model returns no tool call, raise RuntimeError."""
    client_mock = MagicMock()
    client_mock.messages.with_raw_response.create = AsyncMock(
        return_value=_make_text_response("I'll think about it.")
    )

    with ExitStack() as stack:
        _apply_runner_patches(stack, client_mock=client_mock)
        from foreman import runner as fr

        with pytest.raises(RuntimeError, match="required_tool='send_followup'"):
            _run(fr._run_foreman_ai("guild1", "continue please", required_tool="send_followup"))


def test_no_required_tool_text_only_response_does_not_raise():
    """Without required_tool, a text-only response should exit the loop cleanly."""
    client_mock = MagicMock()
    client_mock.messages.with_raw_response.create = AsyncMock(
        return_value=_make_text_response("Sure, I'll just reply.")
    )

    with ExitStack() as stack:
        _apply_runner_patches(stack, client_mock=client_mock)
        from foreman import runner as fr

        # Should not raise — text-only is acceptable when no tool is required
        _run(fr._run_foreman_ai("guild1", "hi there", required_tool=None))


# ---------------------------------------------------------------------------
# Valid tool-use response: accepted and dispatched
# ---------------------------------------------------------------------------


def test_required_tool_valid_response_dispatches_tool():
    """When required_tool is set and the model returns the matching tool, exec_tools is called."""
    tool_result = {"type": "tool_result", "tool_use_id": "toolu_test_abc", "content": "ok"}
    exec_tools_mock = AsyncMock(return_value=[tool_result])

    responses = [_make_tool_use_response("send_followup"), _make_text_response("Done.")]
    call_idx = [0]

    async def _side_effect(**kwargs):
        idx = call_idx[0]
        call_idx[0] += 1
        return responses[min(idx, len(responses) - 1)]

    client_mock = MagicMock()
    client_mock.messages.with_raw_response.create = AsyncMock(side_effect=_side_effect)

    with ExitStack() as stack:
        _apply_runner_patches(stack, client_mock=client_mock, exec_tools_mock=exec_tools_mock)
        from foreman import runner as fr

        _run(fr._run_foreman_ai("guild1", "continue the task", required_tool="send_followup"))

    exec_tools_mock.assert_called_once()
    tool_uses_passed = exec_tools_mock.call_args[0][1]
    assert len(tool_uses_passed) == 1
    assert tool_uses_passed[0].name == "send_followup"
