"""Tests for per-task child context prompt builders and tool set
(backend.foreman)."""

from __future__ import annotations

from backend.foreman.prompt import (
    build_child_state_preamble,
    build_child_system_blocks,
)
from backend.foreman.tools_schema import CHILD_FOREMAN_TOOLS, FOREMAN_TOOLS


def test_child_tools_exclude_create_and_assign():
    names = {t["name"] for t in CHILD_FOREMAN_TOOLS}
    assert "create_task" not in names
    assert "assign_task" not in names
    # Everything else carries over.
    assert names == {t["name"] for t in FOREMAN_TOOLS} - {"create_task", "assign_task"}


def test_child_tools_keep_lifecycle_tools():
    names = {t["name"] for t in CHILD_FOREMAN_TOOLS}
    for expected in ("send_followup", "finalize_task", "get_pr_status", "redirect_task"):
        assert expected in names


def test_child_system_blocks_single_cached_block():
    blocks = build_child_system_blocks(
        task_id="t-abc", task_name="Fix CI", worker_id="w-1", phase="execute", repo="o/r"
    )
    assert len(blocks) == 1
    block = blocks[0]
    assert block["type"] == "text"
    assert block["cache_control"] == {"type": "ephemeral"}
    text = block["text"]
    # Task metadata is embedded.
    assert "t-abc" in text
    assert "Fix CI" in text
    assert "w-1" in text
    assert "execute" in text
    assert "o/r" in text
    # Single-task framing, not whole-guild.
    assert "single-task context" in text


def test_child_system_blocks_tolerates_missing_fields():
    blocks = build_child_system_blocks(task_id="t-abc", task_name="t-abc")
    text = blocks[0]["text"]
    assert "(unassigned)" in text
    assert "(none)" in text


def test_child_state_preamble_includes_worker_and_task():
    out = build_child_state_preamble('{"id": "w-1"}', '{"id": "t-abc"}')
    assert "<state>" in out and "</state>" in out
    assert "Assigned worker" in out
    assert "w-1" in out
    assert "t-abc" in out


def test_child_state_preamble_includes_fresh_current_time():
    out = build_child_state_preamble('{"id": "w-1"}', '{"id": "t-abc"}')
    assert "Current UTC time: " in out
    # Timestamp must precede the rest of the state, and land ahead of any state data.
    assert out.index("Current UTC time: ") < out.index("Assigned worker")


def test_child_state_preamble_handles_offline_worker():
    out = build_child_state_preamble("[]", '{"id": "t-abc"}')
    assert "not currently online" in out
    assert "t-abc" in out


def test_child_state_preamble_appends_extra_context():
    out = build_child_state_preamble('{"id": "w-1"}', '{"id": "t-abc"}', "CI is red")
    assert "## Context" in out
    assert "CI is red" in out
