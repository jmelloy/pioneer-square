"""Foreman AI runner: conversation management and main loop.

Standalone version — all DB access goes through ForemanHTTPClient REST calls;
broadcasts go through ws_send() which relays via foreman-broadcast WS messages.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .prompt import build_state_preamble, build_system_blocks, build_system_prompt

if TYPE_CHECKING:
    from .config import Config
    from .http_client import ForemanHTTPClient

try:
    import anthropic as _anthropic

    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

logger = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 8_000
MAX_HISTORY_MESSAGES = 20
MAX_FOREMAN_ROUNDS = 10
_HUMAN_TURN_WINDOW = 5
_TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})
_24H_SECS = 86_400

# Module-level Anthropic client (reused across calls)
_anthropic_client: _anthropic.AsyncAnthropic | None = None


def _get_anthropic_client(api_key: str | None = None) -> _anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        kwargs: dict = {}
        if api_key:
            kwargs["api_key"] = api_key
        _anthropic_client = _anthropic.AsyncAnthropic(**kwargs)
    return _anthropic_client


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


def truncate_tool_result(content: str, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Cap a tool result string; append a truncation notice when trimmed."""
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + f"\n… [truncated: {len(content) - max_chars} chars omitted]"


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


def _summarize_task(task: dict, cutoff_ts: float) -> dict | None:
    """Return a (possibly stripped) task dict, or None to exclude it.

    Terminal tasks older than 24 h are dropped; terminal tasks within 24 h lose
    their ``description`` field to keep context lean.  Non-terminal tasks are
    returned unchanged.
    """
    state = task.get("state", "")
    if state not in _TERMINAL_STATES:
        return task
    finished_at = task.get("finished_at")
    if finished_at:
        try:
            finished_ts = datetime.fromisoformat(finished_at.replace("Z", "+00:00")).timestamp()
            if finished_ts < cutoff_ts:
                return None  # older than 24 h — drop entirely
        except (ValueError, AttributeError):
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


async def _load_history(guild_id: str, user_id: str, *, http: ForemanHTTPClient) -> list[dict]:
    """Load history via REST and apply the same sliding-window as the embedded runner."""
    turns = await http.get_history(user_id)

    if not turns:
        return []

    # Same sliding window as backend _load_history
    cutoff = 0
    human_count = 0
    for i in range(len(turns) - 1, -1, -1):
        t = turns[i]
        if t["role"] == "user" and not t["is_tool_response"]:
            human_count += 1
            if human_count >= _HUMAN_TURN_WINDOW:
                cutoff = i
                break

    messages = [
        {"role": t["role"], "content": json.loads(t["content_json"])}
        for t in turns[cutoff:]
        if t["role"] != "system"
    ]

    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    return messages


async def _save_turn(
    guild_id: str,
    user_id: str,
    role: str,
    content,
    *,
    http: ForemanHTTPClient,
    is_tool_response: bool = False,
    parent_id: int | None = None,
) -> int:
    """Persist one turn via REST. Returns the new row's id."""
    result = await http.save_turn(
        user_id,
        role,
        _serialize_content(content),
        is_tool_response=is_tool_response,
        parent_id=parent_id,
    )
    return result["id"]


async def run_foreman_ai(
    guild_id: str,
    human_message: str,
    extra_context: str = "",
    user_id: str | None = None,
    *,
    http: ForemanHTTPClient,
    ws_send: Callable[[dict], Awaitable[None]],
    config: Config,
) -> None:
    """Process a human message (or system escalation) through the Claude foreman AI.

    Standalone version — uses REST for state/history, WS for broadcasts.
    """
    from .tools import FOREMAN_TOOLS, exec_tools

    if not HAS_ANTHROPIC:
        now = datetime.now(UTC).isoformat()
        await ws_send(
            {
                "type": "chat",
                "from": "foreman",
                "to": "user",
                "content": "Foreman AI offline (install `anthropic` package to enable).",
                "createdAt": now,
            }
        )
        return

    # Fetch guild state
    state = await http.get_state()
    guild_data = state.get("guild") or {}
    primary_repo = guild_data.get("primary_repo")
    worker_rows = state.get("workers") or []
    task_rows = state.get("tasks") or []

    # Resolve user_id from guild owner if not provided
    if not user_id:
        user_id = guild_data.get("owner_user_id") or guild_id

    try:
        workers_block = json.dumps(
            [
                {
                    "id": r["id"],
                    "state": r.get("state") or "idle",
                    "repos": r.get("repos") or [],
                    **({"org": r["org"]} if r.get("org") else {}),
                    "agent_count": r.get("agent_count") or 0,
                }
                for r in worker_rows
            ],
            indent=2,
        )
        cutoff_ts = datetime.now(UTC).timestamp() - _24H_SECS
        summarized_tasks = [
            s for row in task_rows if (s := _summarize_task(row, cutoff_ts)) is not None
        ]
        tasks_block = json.dumps(summarized_tasks, indent=2)
        system_blocks = build_system_blocks(primary_repo=primary_repo)
        state_preamble = build_state_preamble(workers_block, tasks_block, extra_context)
        audit_system = build_system_prompt(
            workers_block, tasks_block, extra_context, primary_repo=primary_repo
        )

        logger.info(
            "guild=%s run_foreman_ai: workers=%d tasks_in_context=%d",
            guild_id,
            len(worker_rows),
            len(summarized_tasks),
        )

        await _save_turn(guild_id, user_id, "system", audit_system, http=http)
        await _save_turn(guild_id, user_id, "user", human_message, http=http)
        messages = await _load_history(guild_id, user_id, http=http)

        _inject_state_preamble(messages, state_preamble)

        client = _get_anthropic_client(config.api_key)

        text_parts = []
        for round_num in range(config.max_rounds):
            messages = prune_history(messages)
            messages = strip_orphaned_tool_results(messages)
            _stamp_message_cache_breakpoint(messages)
            logger.info(
                "guild=%s round %d: sending %d messages to Claude",
                guild_id,
                round_num,
                len(messages),
            )
            resp = await client.messages.create(
                model=config.model,
                max_tokens=1024,
                system=system_blocks,
                messages=messages,
                tools=FOREMAN_TOOLS,
            )
            usage = resp.usage
            _input_tokens = getattr(usage, "input_tokens", 0) or 0
            _output_tokens = getattr(usage, "output_tokens", 0) or 0
            logger.info(
                "guild=%s round %d: stop_reason=%s input=%d output=%d",
                guild_id,
                round_num,
                resp.stop_reason,
                _input_tokens,
                _output_tokens,
            )

            asst_turn_id = await _save_turn(guild_id, user_id, "assistant", resp.content, http=http)
            await http.update_turn_tokens(asst_turn_id, _input_tokens, _output_tokens)
            messages.append({"role": "assistant", "content": _serialize_content(resp.content)})
            # Re-parse so messages stays as plain dicts (not SDK objects)
            messages[-1]["content"] = json.loads(messages[-1]["content"])

            _now = datetime.now(UTC).isoformat()
            for b in resp.content:
                if b.type == "text" and b.text.strip():
                    text_parts.append(b.text.strip())
                    await ws_send(
                        {
                            "type": "chat",
                            "from": "foreman",
                            "to": "user",
                            "content": b.text.strip(),
                            "createdAt": _now,
                        }
                    )

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break

            for tu in tool_uses:
                await ws_send(
                    {
                        "type": "chat",
                        "from": "foreman",
                        "role": "tool_use",
                        "to": "user",
                        "content": f"▶ {tu.name}",
                        "toolName": tu.name,
                        "toolInput": dict(tu.input) if tu.input else {},
                        "toolId": tu.id,
                        "createdAt": _now,
                    }
                )

            _tool_use_ts = _now  # noqa: F841

            tool_results = await exec_tools(guild_id, tool_uses, http=http, user_id=user_id)
            current_tool_use_ids = {tu.id for tu in tool_uses}
            trimmed = [
                {**r, "content": truncate_tool_result(r["content"])} if r.get("content") else r
                for r in tool_results
                if r.get("tool_use_id") in current_tool_use_ids
            ]

            _now = datetime.now(UTC).isoformat()
            for result in trimmed:
                await ws_send(
                    {
                        "type": "chat",
                        "from": "foreman",
                        "role": "tool_result",
                        "to": "user",
                        "content": result.get("content", ""),
                        "toolId": result.get("tool_use_id"),
                        "toolOutput": result.get("content", ""),
                        "isError": result.get("is_error", False),
                        "createdAt": _now,
                    }
                )

            await _save_turn(
                guild_id,
                user_id,
                "user",
                trimmed,
                http=http,
                is_tool_response=True,
                parent_id=asst_turn_id,
            )
            logger.info(
                "guild=%s round %d: %d tool call(s): %s",
                guild_id,
                round_num,
                len(trimmed),
                [r.get("tool_use_id") for r in trimmed],
            )
            messages.append({"role": "user", "content": trimmed})
        else:
            # Safety cap wrap-up (same as embedded runner)
            logger.warning("guild=%s hit %d-round safety cap", guild_id, config.max_rounds)
            messages = prune_history(messages)
            messages = strip_orphaned_tool_results(messages)
            _stamp_message_cache_breakpoint(messages)
            wrap_resp = await client.messages.create(
                model=config.model,
                max_tokens=1024,
                system=system_blocks,
                messages=messages,
                tools=FOREMAN_TOOLS,
                tool_choice={"type": "none"},
            )
            wrap_usage = wrap_resp.usage
            _wrap_input = getattr(wrap_usage, "input_tokens", 0) or 0
            _wrap_output = getattr(wrap_usage, "output_tokens", 0) or 0
            wrap_turn_id = await _save_turn(
                guild_id, user_id, "assistant", wrap_resp.content, http=http
            )
            await http.update_turn_tokens(wrap_turn_id, _wrap_input, _wrap_output)
            _now = datetime.now(UTC).isoformat()
            for b in wrap_resp.content:
                if b.type == "text" and b.text.strip():
                    text_parts.append(b.text.strip())
                    await ws_send(
                        {
                            "type": "chat",
                            "from": "foreman",
                            "to": "user",
                            "content": b.text.strip(),
                            "createdAt": _now,
                        }
                    )
            cap_note = f"_(Foreman hit {config.max_rounds}-round safety cap and stopped.)_"
            text_parts.append(cap_note)
            await ws_send(
                {
                    "type": "chat",
                    "from": "foreman",
                    "to": "user",
                    "content": cap_note,
                    "createdAt": _now,
                }
            )

        response_text = "\n".join(text_parts).strip()
        if response_text:
            await http.save_message(
                "foreman",
                "user",
                response_text,
                user_id=user_id,
            )

    except Exception as exc:
        logger.exception("guild=%s run_foreman_ai failed", guild_id)
        now = datetime.now(UTC).isoformat()
        await ws_send(
            {
                "type": "chat",
                "from": "foreman",
                "to": "user",
                "content": f"Foreman error: {exc}",
                "createdAt": now,
            }
        )
