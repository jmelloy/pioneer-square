"""Tests for strip_orphaned_tool_results in foreman.runner."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foreman.runner import strip_orphaned_tool_results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_use(id: str, name: str = "do_thing") -> dict:
    return {"type": "tool_use", "id": id, "name": name, "input": {}}


def _tool_result(tool_use_id: str, content: str = "ok") -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _user(*blocks) -> dict:
    return {"role": "user", "content": list(blocks)}


def _user_text(t: str) -> dict:
    return {"role": "user", "content": t}


def _asst(*blocks) -> dict:
    return {"role": "assistant", "content": list(blocks)}


# ---------------------------------------------------------------------------
# Failing case: orphaned tool_result (no preceding assistant with tool_use)
# ---------------------------------------------------------------------------


class TestOrphanedToolResult:
    def test_orphan_at_start_is_removed(self):
        messages = [
            _user(_tool_result("toolu_ORPHAN")),
            _asst(_text("hello")),
            _user_text("thanks"),
        ]
        result = strip_orphaned_tool_results(messages)
        # The orphaned user message should be dropped; list re-starts at assistant,
        # then strip_orphaned_tool_results re-enforces starts-with-user, so the
        # assistant is also dropped, leaving only the final user message.
        roles = [m["role"] for m in result]
        assert roles[0] == "user"
        assert not any(
            isinstance(m.get("content"), list)
            and any(b.get("type") == "tool_result" for b in m["content"] if isinstance(b, dict))
            for m in result
        ), "No orphaned tool_result blocks should remain"

    def test_orphan_at_start_with_plain_text_user_preceding_nonexistent(self):
        # First message is a user message whose content is entirely orphaned tool_results.
        messages = [
            _user(_tool_result("X1"), _tool_result("X2")),
        ]
        result = strip_orphaned_tool_results(messages)
        assert result == []

    def test_preceding_message_is_not_assistant(self):
        # Preceding message is a plain-text user message, not an assistant — still orphaned.
        messages = [
            _user_text("hey"),
            _user(_tool_result("toolu_ORPHAN")),
        ]
        result = strip_orphaned_tool_results(messages)
        for m in result:
            content = m.get("content")
            if isinstance(content, list):
                assert not any(b.get("type") == "tool_result" for b in content if isinstance(b, dict))


# ---------------------------------------------------------------------------
# Passing case: well-formed history — nothing should be removed
# ---------------------------------------------------------------------------


class TestWellFormedHistory:
    def test_matching_pair_unchanged(self):
        messages = [
            _user_text("do the thing"),
            _asst(_text("ok"), _tool_use("tu1")),
            _user(_tool_result("tu1")),
            _asst(_text("done")),
            _user_text("thanks"),
        ]
        result = strip_orphaned_tool_results(messages)
        assert result == messages

    def test_multiple_tool_uses_all_matched(self):
        messages = [
            _user_text("go"),
            _asst(_tool_use("A"), _tool_use("B")),
            _user(_tool_result("A"), _tool_result("B")),
            _asst(_text("done")),
            _user_text("ok"),
        ]
        result = strip_orphaned_tool_results(messages)
        assert result == messages

    def test_no_tool_use_at_all(self):
        messages = [
            _user_text("hello"),
            _asst(_text("hi")),
            _user_text("bye"),
        ]
        result = strip_orphaned_tool_results(messages)
        assert result == messages


# ---------------------------------------------------------------------------
# Mixed case: some orphaned, some valid
# ---------------------------------------------------------------------------


class TestMixedCase:
    def test_orphan_removed_cascades_when_asst_has_tool_use(self):
        # Dropping the first (fully orphaned) user message exposes an assistant.
        # That assistant must be removed (API requires first=user), which in turn
        # orphans the following tool_result — cascade drops all three.
        # Only the plain-text tail survives.
        messages = [
            _user(_tool_result("ORPHAN")),
            _asst(_text("I will act"), _tool_use("VALID")),
            _user(_tool_result("VALID")),
            _asst(_text("done")),
            _user_text("great"),
        ]
        result = strip_orphaned_tool_results(messages)
        assert result == [_user_text("great")]

    def test_orphan_removed_text_asst_pair_kept(self):
        # When the exposed assistant is text-only (no tool_use), removing it to
        # enforce starts-with-user exposes a plain-text user message — the cascade
        # stops there, and the subsequent valid pair survives.
        messages = [
            _user(_tool_result("ORPHAN")),
            _asst(_text("summary")),          # text-only: removed by start-with-user fix
            _user_text("please continue"),    # valid plain-text — cascade stops here
            _asst(_tool_use("VALID")),
            _user(_tool_result("VALID")),
            _asst(_text("done")),
            _user_text("great"),
        ]
        result = strip_orphaned_tool_results(messages)
        tool_result_ids = [
            b["tool_use_id"]
            for m in result
            for b in (m["content"] if isinstance(m.get("content"), list) else [])
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert "ORPHAN" not in tool_result_ids
        assert "VALID" in tool_result_ids

    def test_partial_orphan_in_user_message(self):
        # User message has one valid and one orphaned tool_result.
        messages = [
            _user_text("start"),
            _asst(_tool_use("GOOD")),
            _user(_tool_result("GOOD"), _tool_result("ORPHAN")),
            _asst(_text("done")),
            _user_text("ok"),
        ]
        result = strip_orphaned_tool_results(messages)
        tool_results = [
            b
            for m in result
            for b in (m["content"] if isinstance(m.get("content"), list) else [])
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        ids = [r["tool_use_id"] for r in tool_results]
        assert "GOOD" in ids
        assert "ORPHAN" not in ids


# ---------------------------------------------------------------------------
# Edge case: entire user message is orphaned tool_results → drop the message
# ---------------------------------------------------------------------------


class TestEmptyUserMessageDropped:
    def test_all_tool_results_orphaned_drops_message(self):
        messages = [
            _user(_tool_result("GONE1"), _tool_result("GONE2")),
            _asst(_text("hi")),
            _user_text("hey"),
        ]
        result = strip_orphaned_tool_results(messages)
        # The first user message is fully orphaned → dropped.
        # The leading assistant is then stripped → starts with the final user message.
        assert result == [_user_text("hey")]

    def test_result_always_starts_with_user(self):
        # After removing an orphaned user message, the next message may be assistant —
        # ensure the output still starts with a user turn.
        messages = [
            _user(_tool_result("ORPHAN")),
            _asst(_text("something")),
            _user_text("hello"),
        ]
        result = strip_orphaned_tool_results(messages)
        if result:
            assert result[0]["role"] == "user"


# ---------------------------------------------------------------------------
# Dangling tool_use: assistant has tool_use with no corresponding tool_result
# ---------------------------------------------------------------------------


class TestDanglingToolUse:
    def test_dangling_tool_use_removed_from_assistant(self):
        # Assistant has two tool_uses but user only has one tool_result.
        messages = [
            _user_text("go"),
            _asst(_tool_use("A"), _tool_use("DANGLING")),
            _user(_tool_result("A")),
            _asst(_text("done")),
            _user_text("ok"),
        ]
        result = strip_orphaned_tool_results(messages)
        asst_blocks = [
            b
            for m in result
            if m["role"] == "assistant"
            for b in (m["content"] if isinstance(m.get("content"), list) else [])
        ]
        tool_use_ids = [b["id"] for b in asst_blocks if b.get("type") == "tool_use"]
        assert "DANGLING" not in tool_use_ids
        assert "A" in tool_use_ids

    def test_assistant_text_preserved_when_tool_use_removed(self):
        messages = [
            _user_text("go"),
            _asst(_text("thinking…"), _tool_use("DANGLING")),
            _user_text("next"),
        ]
        result = strip_orphaned_tool_results(messages)
        asst_msgs = [m for m in result if m["role"] == "assistant"]
        assert asst_msgs, "Assistant message with text should be preserved"
        texts = [b["text"] for m in asst_msgs for b in m["content"] if b.get("type") == "text"]
        assert "thinking…" in texts

    def test_all_tool_uses_dangling_empty_assistant_dropped(self):
        # Assistant has only tool_uses, all dangling — the whole message is dropped.
        messages = [
            _user_text("go"),
            _asst(_tool_use("DANGLING")),
            _user_text("next"),
        ]
        result = strip_orphaned_tool_results(messages)
        asst_msgs = [m for m in result if m["role"] == "assistant"]
        # The assistant message should be gone (or its content should be empty/text only).
        for m in asst_msgs:
            content = m.get("content", [])
            assert not any(b.get("type") == "tool_use" for b in content if isinstance(b, dict))


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_idempotent_on_clean_history(self):
        messages = [
            _user_text("go"),
            _asst(_tool_use("A")),
            _user(_tool_result("A")),
            _asst(_text("done")),
            _user_text("thanks"),
        ]
        once = strip_orphaned_tool_results(messages)
        twice = strip_orphaned_tool_results(once)
        assert once == twice

    def test_idempotent_on_dirty_history(self):
        messages = [
            _user(_tool_result("ORPHAN")),
            _asst(_tool_use("A")),
            _user(_tool_result("A")),
            _asst(_text("done")),
            _user_text("thanks"),
        ]
        once = strip_orphaned_tool_results(messages)
        twice = strip_orphaned_tool_results(once)
        assert once == twice
