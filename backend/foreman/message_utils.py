"""Shared message manipulation utilities for the foreman AI runner."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime
from typing import Any

from .constants import (
    _DEFAULT_TASK_TTL_SECS,
    _TERMINAL_STATES,
    MAX_HISTORY_MESSAGES,
    MAX_TOOL_RESULT_CHARS,
)


def _json_default(obj: Any) -> Any:
    """JSON encoder fallback: serialize datetime/date objects as ISO strings."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def truncate_tool_result(content: str, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Cap a tool result string; append a truncation notice when trimmed."""
    if len(content) <= max_chars:
        return content
    omitted = len(content) - max_chars
    return content[:max_chars] + f"\n\n[TRUNCATED — {omitted} chars omitted]"


def prune_history(messages: list, max_messages: int = MAX_HISTORY_MESSAGES) -> list:
    """Return at most *max_messages* tail entries, always starting with a user turn."""
    if len(messages) > max_messages:
        messages = messages[-max_messages:]
    while messages and messages[0]["role"] != "user":
        messages = messages[1:]
    return messages


def strip_orphaned_tool_results(messages: list[dict]) -> list[dict]:
    """Remove any tool_result blocks (and their containing user message if it becomes empty)
    whose tool_use_id has no matching tool_use block in the immediately-preceding
    assistant message. Also remove the dangling tool_use blocks from the assistant
    message that have no corresponding tool_result.
    Returns a cleaned copy of the messages list.
    """
    out = copy.deepcopy(messages)
    i = 0
    while i < len(out):
        msg = out[i]
        content = msg.get("content")

        if msg["role"] == "assistant" and isinstance(content, list):
            # Collect tool_result IDs from the immediately-following user message.
            if i + 1 < len(out):
                nxt = out[i + 1]
                if nxt["role"] == "user" and isinstance(nxt.get("content"), list):
                    result_ids = {
                        b["tool_use_id"]
                        for b in nxt["content"]
                        if isinstance(b, dict) and b.get("type") == "tool_result"
                    }
                else:
                    result_ids = set()
            else:
                result_ids = set()

            new_content = [
                b
                for b in content
                if not (isinstance(b, dict) and b.get("type") == "tool_use")
                or b.get("id") in result_ids
            ]
            if new_content != content:
                if not new_content:
                    out.pop(i)
                    continue
                msg["content"] = new_content

        elif msg["role"] == "user" and isinstance(content, list):
            # Collect tool_use IDs from the immediately-preceding assistant message.
            if i > 0:
                prev = out[i - 1]
                if prev["role"] == "assistant" and isinstance(prev.get("content"), list):
                    valid_ids = {
                        b["id"]
                        for b in prev["content"]
                        if isinstance(b, dict) and b.get("type") == "tool_use"
                    }
                else:
                    valid_ids = set()
            else:
                valid_ids = set()

            new_content = [
                b
                for b in content
                if not (isinstance(b, dict) and b.get("type") == "tool_result")
                or b.get("tool_use_id") in valid_ids
            ]
            if new_content != content:
                if not new_content:
                    out.pop(i)
                    continue
                msg["content"] = new_content

        i += 1

    # Re-enforce starts-with-user and handle cascading orphans at the head.
    # Removing a leading orphaned user message exposes a leading assistant; removing
    # that assistant may expose another user message whose tool_results are now
    # orphaned (their matching assistant was just dropped). Loop until stable.
    while out:
        if out[0]["role"] != "user":
            out.pop(0)
            continue
        content = out[0].get("content")
        if isinstance(content, list):
            new_content = [
                b for b in content if not (isinstance(b, dict) and b.get("type") == "tool_result")
            ]
            if not new_content:
                out.pop(0)
                continue
            if new_content != content:
                out[0]["content"] = new_content
        break
    return out


def _inject_state_preamble(messages: list[dict], state_preamble: str) -> None:
    """Prepend the live-state block to the last user turn (in place).

    Called after history is loaded so the current human turn carries the latest
    workers/tasks snapshot without persisting that snapshot to the DB.
    """
    if not messages or messages[-1]["role"] != "user":
        return
    last = messages[-1]
    content = last["content"]
    state_block = {"type": "text", "text": state_preamble}
    if isinstance(content, str):
        last["content"] = [state_block, {"type": "text", "text": content}]
    elif isinstance(content, list):
        last["content"] = [state_block, *content]


def _stamp_message_cache_breakpoint(messages: list[dict]) -> None:
    """Move the messages-level cache breakpoint to the last block of the last turn.

    Clears any prior message-level cache_control first so we never accumulate
    past the 4-breakpoint API cap (1 used by the system block). Earlier cached
    prefixes remain readable via the API's 20-block lookback.
    """
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    if not messages:
        return
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
        return
    if isinstance(content, list):
        for block in reversed(content):
            if isinstance(block, dict):
                block["cache_control"] = {"type": "ephemeral"}
                return


def _summarize_task(task: dict, cutoff_ts: float) -> dict | None:
    """Return a (possibly stripped) task dict, or None to exclude it.

    Terminal tasks older than 24 h are dropped; terminal tasks within 24 h lose
    their ``description`` field to keep context lean.  Non-terminal tasks are
    returned unchanged.

    Completion time is approximated as ``deleted_at - DEFAULT_TTL`` since
    ``finished_at`` was removed in favour of the single ``deleted_at`` column.
    """
    state = task.get("state", "")
    if state not in _TERMINAL_STATES:
        return task
    deleted_at = task.get("deleted_at")
    if deleted_at:
        try:
            deleted_ts = datetime.fromisoformat(
                deleted_at.replace("Z", "+00:00") if isinstance(deleted_at, str) else deleted_at
            ).timestamp()
            finished_ts = deleted_ts - _DEFAULT_TASK_TTL_SECS
            if finished_ts < cutoff_ts:
                return None  # older than 24 h — drop entirely
        except (ValueError, AttributeError, TypeError):
            pass
    # Within 24 h or undatable: compact summary without description
    return {k: v for k, v in task.items() if k != "description"}


def _serialize_content(content) -> str:
    """Convert SDK content objects or dicts to a JSON string for DB storage."""
    if isinstance(content, str):
        return json.dumps(content)
    if isinstance(content, list):
        blocks = []
        for b in content:
            if isinstance(b, dict):
                blocks.append(b)
            else:
                try:
                    blocks.append(b.model_dump())
                except AttributeError:
                    blocks.append({"type": str(getattr(b, "type", "unknown")), "raw": str(b)})
        return json.dumps(blocks)
    return json.dumps(str(content))
