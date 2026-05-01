"""Unit tests for pioneer_worker.claude_runner."""

from __future__ import annotations

import pytest
from pioneer_worker.claude_runner import _summarize_lines, parse_claude_event

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


def test_parse_assistant_thinking_truncates_at_100():
    long_thought = "x" * 200
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": long_thought}]},
    }
    pairs = parse_claude_event(event)
    assert "..." in pairs[0][0]


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
