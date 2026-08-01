"""Unit tests for pioneer_worker.claude_runner."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from pioneer_worker.claude_runner import (
    _summarize_lines,
    _truncate_at_word,
    _usage_tokens,
    parse_claude_event,
    run_claude_auto,
)

# ---------------------------------------------------------------------------
# _summarize_lines
# ---------------------------------------------------------------------------


def test_summarize_short_list_shows_all():
    lines = ["a", "b", "c"]
    result = _summarize_lines(lines)
    assert "a" in result
    assert "b" in result
    assert "c" in result
    assert "…" not in result


def test_summarize_exactly_four_lines_shows_all():
    lines = ["w", "x", "y", "z"]
    result = _summarize_lines(lines)
    assert all(line in result for line in lines)
    assert "…" not in result


def test_summarize_long_list_truncates_middle():
    lines = [f"line{i}" for i in range(10)]
    result = _summarize_lines(lines)
    assert "line0" in result
    assert "line9" in result
    assert "…" in result


def test_summarize_custom_prefix():
    lines = ["alpha", "beta", "gamma", "delta", "epsilon"]
    result = _summarize_lines(lines, prefix=">> ")
    assert result.count(">> ") >= 3


# ---------------------------------------------------------------------------
# parse_claude_event — assistant messages
# ---------------------------------------------------------------------------


def test_parse_assistant_plain_text():
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Hello world"}]},
    }
    pairs = parse_claude_event(event)
    assert pairs == [("Hello world", None)]


def test_parse_assistant_empty_text_skipped():
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "   "}]},
    }
    assert parse_claude_event(event) == []


def test_parse_assistant_thinking_block():
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": "I should do X"}]},
    }
    pairs = parse_claude_event(event)
    assert len(pairs) == 1
    assert pairs[0][0].startswith("[thinking]")
    assert "I should do X" in pairs[0][0]


def test_parse_assistant_thinking_truncates_at_300():
    long_thought = "x" * 400
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": long_thought}]},
    }
    pairs = parse_claude_event(event)
    assert "..." in pairs[0][0]
    text, detail = pairs[0]
    assert detail is not None
    assert detail["toolType"] == "thinking"
    assert detail["fullText"] == long_thought


def test_parse_assistant_thinking_word_boundary():
    long_thought = ("hello world " * 30).strip()  # spaces at word boundaries
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": long_thought}]},
    }
    pairs = parse_claude_event(event)
    text, _ = pairs[0]
    preview = text[len("[thinking] ") :]
    assert not preview.endswith("...") or preview[:-3].endswith(("o", "d", " "))


def test_parse_assistant_thinking_short_no_tooltype():
    short_thought = "I should do Y"
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": short_thought}]},
    }
    pairs = parse_claude_event(event)
    assert len(pairs) == 1
    _, detail = pairs[0]
    assert detail is not None
    assert "toolType" not in detail


def test_parse_assistant_bash_tool():
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "echo hello"},
                }
            ]
        },
    }
    pairs = parse_claude_event(event)
    assert len(pairs) == 1
    text, detail = pairs[0]
    assert text == "▶ bash: echo hello"
    assert detail["name"] == "Bash"
    assert detail["toolType"] == "tool_use"


def test_parse_assistant_read_tool():
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": "/tmp/foo.py"},
                }
            ]
        },
    }
    pairs = parse_claude_event(event)
    text, detail = pairs[0]
    assert text == "▶ read: /tmp/foo.py"
    assert detail["name"] == "Read"


def test_parse_assistant_write_tool():
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Write",
                    "input": {"file_path": "/tmp/out.txt"},
                }
            ]
        },
    }
    pairs = parse_claude_event(event)
    text, _ = pairs[0]
    assert text == "▶ write: /tmp/out.txt"


def test_parse_assistant_read_tool_strips_worktree_prefix():
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {
                        "file_path": "/work/dnsid/w-4de676/t-vdfpj3/dnsid-infra/envs/dev/main.tf"
                    },
                }
            ]
        },
    }
    pairs = parse_claude_event(event)
    text, _ = pairs[0]
    assert text == "▶ read: dnsid-infra/envs/dev/main.tf"


def test_parse_assistant_unknown_tool():
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "CustomTool",
                    "input": {"key": "value"},
                }
            ]
        },
    }
    pairs = parse_claude_event(event)
    text, detail = pairs[0]
    assert text.startswith("▶ CustomTool:")
    assert detail["name"] == "CustomTool"


def test_parse_assistant_multi_block():
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "First"},
                {"type": "text", "text": "Second"},
            ]
        },
    }
    pairs = parse_claude_event(event)
    assert len(pairs) == 2
    assert pairs[0] == ("First", None)
    assert pairs[1] == ("Second", None)


# ---------------------------------------------------------------------------
# parse_claude_event — user (tool_result) messages
# ---------------------------------------------------------------------------


def test_parse_user_tool_result_string():
    event = {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "content": "output line 1\noutput line 2"}]
        },
    }
    pairs = parse_claude_event(event)
    assert len(pairs) == 1
    text, detail = pairs[0]
    assert "output line 1" in text
    assert detail["toolType"] == "tool_result"


def test_parse_user_tool_result_list_content():
    event = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "content": [{"type": "text", "text": "result text"}],
                }
            ]
        },
    }
    pairs = parse_claude_event(event)
    assert len(pairs) == 1
    assert "result text" in pairs[0][1]["output"]


def test_parse_user_tool_result_summarized_line_keeps_full_output_in_detail():
    """Line is summarized (#781) middle-elided, but detail['output'] keeps the full text."""
    lines = [f"line {i}" for i in range(50)]
    full_output = "\n".join(lines)
    event = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": full_output}]},
    }
    pairs = parse_claude_event(event)
    assert len(pairs) == 1
    text, detail = pairs[0]
    assert "more lines" in text  # display line is elided
    assert text != full_output
    assert detail["output"] == full_output  # full content preserved untruncated


def test_parse_user_tool_result_empty_skipped():
    event = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": ""}]},
    }
    assert parse_claude_event(event) == []


# ---------------------------------------------------------------------------
# parse_claude_event — result events
# ---------------------------------------------------------------------------


def test_parse_result_success():
    event = {"type": "result", "subtype": "success", "num_turns": 5}
    pairs = parse_claude_event(event)
    assert pairs == [("✓ Done in 5 turns", None)]


def test_parse_result_success_with_cost():
    event = {"type": "result", "subtype": "success", "num_turns": 2, "cost_usd": 0.0123}
    pairs = parse_claude_event(event)
    assert "$0.0123" in pairs[0][0]


def test_parse_result_max_turns():
    event = {"type": "result", "subtype": "max_turns", "num_turns": 50}
    pairs = parse_claude_event(event)
    assert "max_turns" in pairs[0][0]
    assert pairs[0][0].startswith("✗")


def test_parse_result_error():
    event = {"type": "result", "subtype": "error_during_execution", "error": "boom"}
    pairs = parse_claude_event(event)
    assert "error_during_execution" in pairs[0][0]
    assert "boom" in pairs[0][0]


# ---------------------------------------------------------------------------
# parse_claude_event — system init
# ---------------------------------------------------------------------------


def test_parse_system_init_lists_tools():
    event = {
        "type": "system",
        "subtype": "init",
        "tools": ["Bash", "Read", "Write", "Edit"],
    }
    pairs = parse_claude_event(event)
    assert len(pairs) == 1
    text = pairs[0][0]
    assert "Bash" in text
    assert "Read" in text


def test_parse_system_other_subtype_ignored():
    event = {"type": "system", "subtype": "other"}
    assert parse_claude_event(event) == []


# ---------------------------------------------------------------------------
# parse_claude_event — unknown types
# ---------------------------------------------------------------------------


def test_parse_unknown_type_returns_empty():
    assert parse_claude_event({"type": "something_new"}) == []


def test_parse_empty_event_returns_empty():
    assert parse_claude_event({}) == []


# ---------------------------------------------------------------------------
# _usage_tokens
# ---------------------------------------------------------------------------


def test_usage_tokens_full():
    usage = {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 40,
    }
    assert _usage_tokens(usage) == {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 40,
    }


def test_usage_tokens_missing_and_none_default_zero():
    assert _usage_tokens({"input_tokens": None}) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


# ---------------------------------------------------------------------------
# run_claude_auto — on_usage callback
# ---------------------------------------------------------------------------


class _FakeStream:
    """Async-iterable byte stream of pre-baked lines."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self):
        async def _gen():
            for ln in self._lines:
                yield ln

        return _gen()


class _FakeProc:
    def __init__(self, stdout_lines: list[bytes]) -> None:
        self.pid = 4242
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream([])
        self.stdin = None
        # Real asyncio Processes always expose this; run_claude_auto's reaper
        # reads it to decide whether the child still needs killing.
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0


async def test_run_claude_auto_emits_per_call_and_result_usage():
    events = [
        {"type": "system", "subtype": "init", "session_id": "sess-1", "tools": []},
        {
            "type": "assistant",
            "message": {
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 7,
                    "cache_read_input_tokens": 1,
                    "cache_creation_input_tokens": 2,
                },
            },
        },
        {
            "type": "assistant",
            "message": {
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": "bye"}],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "num_turns": 2,
            "total_cost_usd": 0.5,
            "usage": {"input_tokens": 100, "output_tokens": 11},
        },
    ]
    lines = [(json.dumps(e) + "\n").encode() for e in events]

    captured: list[dict] = []

    async def _emit(text, detail=None):
        return None

    async def _on_usage(rec):
        captured.append(rec)

    async def _fake_exec(*args, **kwargs):
        return _FakeProc(lines)

    with patch("asyncio.create_subprocess_exec", _fake_exec):
        success, stop_reason, last_text, session_id = await run_claude_auto(
            "do it",
            "/tmp",
            max_turns=10,
            emit=_emit,
            on_usage=_on_usage,
        )

    assert success is True
    assert stop_reason == "success"
    assert session_id == "sess-1"

    api_calls = [r for r in captured if r["kind"] == "api_call"]
    results = [r for r in captured if r["kind"] == "result"]
    assert len(api_calls) == 2
    assert len(results) == 1

    assert api_calls[0]["input_tokens"] == 5
    assert api_calls[0]["output_tokens"] == 7
    assert api_calls[0]["cache_read_input_tokens"] == 1
    assert api_calls[0]["model"] == "claude-opus-4-8"
    # second call's missing cache fields default to 0
    assert api_calls[1]["cache_read_input_tokens"] == 0

    # The result event's self-reported total (100) differs from the per-call
    # input sum (5 + 3 = 8) — the discrepancy this feature surfaces.
    assert results[0]["input_tokens"] == 100
    assert sum(c["input_tokens"] for c in api_calls) == 8
    assert results[0]["cost_usd"] == 0.5
    assert results[0]["num_turns"] == 2
    assert results[0]["stop_reason"] == "success"


async def test_run_claude_auto_no_usage_callback_is_optional():
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "result", "subtype": "success", "num_turns": 1},
    ]
    lines = [(json.dumps(e) + "\n").encode() for e in events]

    async def _emit(text, detail=None):
        return None

    async def _fake_exec(*args, **kwargs):
        return _FakeProc(lines)

    with patch("asyncio.create_subprocess_exec", _fake_exec):
        success, stop_reason, _last, _sid = await run_claude_auto(
            "do it", "/tmp", max_turns=10, emit=_emit
        )
    assert success is True
    assert stop_reason == "success"
