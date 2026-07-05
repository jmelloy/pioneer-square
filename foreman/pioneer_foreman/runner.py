"""Foreman AI runner: conversation management and main loop.

Standalone version — all DB access goes through ForemanHTTPClient REST calls;
broadcasts go through ws_send() which relays via foreman-broadcast WS messages.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from backend.foreman_core.constants import _24H_SECS, _HUMAN_TURN_WINDOW
from backend.foreman_core.llm import HAS_ANTHROPIC, make_anthropic_client
from backend.foreman_core.message_utils import (
    _inject_state_preamble,
    _serialize_content,
    _stamp_message_cache_breakpoint,
    _summarize_task,
    prune_history,
    strip_orphaned_tool_results,
    truncate_tool_result,
)

from .prompt import (
    build_child_state_preamble,
    build_child_system_blocks,
    build_state_preamble,
    build_system_blocks,
    build_system_prompt,
)

if TYPE_CHECKING:
    from .config import Config
    from .http_client import ForemanHTTPClient

# Callback invoked once per executed tool result: (tool_name, tool_input, result_dict).
# The parent foreman uses it to spawn/teardown child contexts on assign/finalize.
ToolObserver = Callable[[str, dict, dict], None]

logger = logging.getLogger(__name__)

# Client cache keyed on (provider, region, api_key) so different configs
# get different clients instead of silently reusing the first one created.
_anthropic_clients: dict[tuple, object] = {}


def _get_anthropic_client(config: Config):
    provider = getattr(config, "provider", "anthropic") or "anthropic"
    region = getattr(config, "aws_region", None)
    profile = getattr(config, "aws_profile", None)
    api_key = getattr(config, "api_key", None) or ""
    anthropic_auth_token = getattr(config, "anthropic_auth_token", None) or ""
    cache_key = (provider.lower(), region, profile, api_key, anthropic_auth_token)
    if cache_key not in _anthropic_clients:
        extra_env: dict[str, str] = {}
        if anthropic_auth_token:
            extra_env["ANTHROPIC_AUTH_TOKEN"] = anthropic_auth_token
        _anthropic_clients[cache_key] = make_anthropic_client(
            provider=provider,
            api_key=api_key or None,
            region=region,
            aws_profile=profile,
            extra_env=extra_env or None,
        )
    return _anthropic_clients[cache_key]


async def _load_history(
    guild_id: str, user_id: str, *, http: ForemanHTTPClient, task_id: str | None = None
) -> list[dict]:
    """Load history via REST and apply the same sliding-window as the embedded runner.

    When ``task_id`` is set, only that task's turns are loaded — the isolated
    conversation thread for a per-task child context.
    """
    turns = await http.get_history(user_id, task_id=task_id)

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
    api_calls: list | None = None,
    task_id: str | None = None,
) -> int:
    """Persist one turn via REST. Returns the new row's id."""
    result = await http.save_turn(
        user_id,
        role,
        _serialize_content(content),
        is_tool_response=is_tool_response,
        parent_id=parent_id,
        api_calls=api_calls,
        task_id=task_id,
    )
    return result["id"]


async def run_foreman_ai(
    guild_id: str,
    human_message: str,
    extra_context: str = "",
    user_id: str | None = None,
    task_id: str | None = None,
    *,
    http: ForemanHTTPClient,
    ws_send: Callable[[dict], Awaitable[None]],
    config: Config,
    child: bool = False,
    tool_observer: ToolObserver | None = None,
) -> bool:
    """Process a human message (or system escalation) through the Claude foreman AI.

    Standalone version — uses REST for state/history, WS for broadcasts.
    ``task_id`` must be passed explicitly by the caller; it is not inferred from
    guild state so there is no ambiguity when multiple tasks are active.

    When ``child`` is True the run is scoped to a single task (``task_id`` is
    required): history, system prompt, state preamble, and tool set are all
    narrowed to that one task (see docs/foreman-per-task-context.md). When False
    this is the parent/legacy whole-guild run.

    ``tool_observer``, if given, is called once per executed tool result with
    ``(tool_name, tool_input, result_dict)`` — the parent uses it to spawn or
    tear down child contexts in reaction to assign_task / finalize_task.

    Returns True if the foreman made at least one tool call, False otherwise.
    Callers use this to decide whether to reset the poll backoff.
    """
    from .tools import CHILD_FOREMAN_TOOLS, FOREMAN_TOOLS, exec_tools

    if child and not task_id:
        raise ValueError("run_foreman_ai(child=True) requires a task_id")
    tools = CHILD_FOREMAN_TOOLS if child else FOREMAN_TOOLS

    # Frontend badge id: set only on child (per-task) runs so the chat pane can
    # mark lines scoped to a single task. Parent/whole-guild runs stay unbadged
    # even when a task_id is present. See docs/foreman-per-task-context.md.
    _badge_task_id = task_id if child else None

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
        return False

    # Fetch guild state
    state = await http.get_state()
    guild_data = state.get("guild") or {}
    primary_repo = guild_data.get("primary_repo")
    worker_rows = state.get("workers") or []
    task_rows = state.get("tasks") or []

    # Resolve user_id from guild owner if not provided
    if not user_id:
        user_id = guild_data.get("owner_user_id") or guild_id

    # In child mode, narrow worker/task rows to just this task and its worker.
    task_row: dict | None = None
    if child:
        task_row = next((t for t in task_rows if t.get("id") == task_id), None)
        child_worker_id = task_row.get("worker_id") if task_row else None
        worker_rows = [r for r in worker_rows if r.get("id") == child_worker_id]
        task_rows = [task_row] if task_row else []

    made_tool_calls = False
    try:
        workers_block = json.dumps(
            [
                {
                    "id": r["id"],
                    "state": r.get("state") or "idle",
                    "repos": r.get("repos") or [],
                    **({"org": r["org"]} if r.get("org") else {}),
                    "agent_count": r.get("agent_count") or 0,
                    "tools": r.get("tools") or [],
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

        if child:
            _t = task_row or {}
            system_blocks = build_child_system_blocks(
                task_id=task_id,
                task_name=_t.get("name") or task_id,
                worker_id=_t.get("worker_id"),
                phase=_t.get("phase"),
                repo=_t.get("issue_repo") or primary_repo,
            )
            state_preamble = build_child_state_preamble(workers_block, tasks_block, extra_context)
        else:
            system_blocks = build_system_blocks(primary_repo=primary_repo)
            state_preamble = build_state_preamble(workers_block, tasks_block, extra_context)
        audit_system = build_system_prompt(
            workers_block, tasks_block, extra_context, primary_repo=primary_repo
        )

        _system_chars = next((len(b["text"]) for b in system_blocks if b.get("type") == "text"), 0)
        logger.info(
            "guild=%s run_foreman_ai: workers=%d tasks_in_context=%d "
            "system_chars=%d state_chars=%d extra_context_chars=%d",
            guild_id,
            len(worker_rows),
            len(summarized_tasks),
            _system_chars,
            len(state_preamble),
            len(extra_context),
        )

        await _save_turn(guild_id, user_id, "system", audit_system, http=http, task_id=task_id)
        await _save_turn(guild_id, user_id, "user", human_message, http=http, task_id=task_id)
        messages = await _load_history(
            guild_id, user_id, http=http, task_id=task_id if child else None
        )

        _inject_state_preamble(messages, state_preamble)

        client = _get_anthropic_client(config)

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
            _raw = await client.messages.with_raw_response.create(
                model=config.effective_model,
                max_tokens=1024,
                system=system_blocks,
                messages=messages,
                tools=tools,
            )
            resp = _raw.parse()
            usage = resp.usage
            _input_tokens = getattr(usage, "input_tokens", 0) or 0
            _output_tokens = getattr(usage, "output_tokens", 0) or 0
            _cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            _cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
            _anthropic_request_id = _raw.headers.get("request-id")
            logger.info(
                "guild=%s round %d: stop_reason=%s content_blocks=%d "
                "input=%d cache_read=%d cache_write=%d output=%d request_id=%s",
                guild_id,
                round_num,
                resp.stop_reason,
                len(resp.content),
                _input_tokens,
                _cache_read,
                _cache_write,
                _output_tokens,
                _anthropic_request_id,
            )

            _api_call_meta = {
                "request_id": _anthropic_request_id,
                "model": config.effective_model,
                "input_tokens": _input_tokens,
                "output_tokens": _output_tokens,
                "cache_read_tokens": _cache_read,
                "cache_write_tokens": _cache_write,
                "ts": datetime.now(UTC).isoformat(),
            }

            asst_turn_id = await _save_turn(
                guild_id,
                user_id,
                "assistant",
                resp.content,
                http=http,
                api_calls=[_api_call_meta],
                task_id=task_id,
            )
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
                            "taskId": _badge_task_id,
                        }
                    )

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break

            made_tool_calls = True

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
                        "taskId": _badge_task_id,
                    }
                )

            _tool_use_ts = _now  # noqa: F841

            tool_results = await exec_tools(guild_id, tool_uses, http=http, user_id=user_id)
            current_tool_use_ids = {tu.id for tu in tool_uses}
            trimmed: list[dict] = []
            for r in tool_results:
                if r.get("tool_use_id") not in current_tool_use_ids:
                    continue
                entry = {k: v for k, v in r.items() if k != "api_calls"}
                if entry.get("content"):
                    entry = {**entry, "content": truncate_tool_result(entry["content"])}
                trimmed.append(entry)

            if tool_observer is not None:
                _tool_by_id = {tu.id: tu for tu in tool_uses}
                for r in trimmed:
                    tu = _tool_by_id.get(r.get("tool_use_id"))
                    if tu is None:
                        continue
                    try:
                        tool_observer(tu.name, dict(tu.input) if tu.input else {}, r)
                    except Exception:
                        logger.exception("tool_observer raised for %s", tu.name)

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
                        "taskId": _badge_task_id,
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
                task_id=task_id,
            )
            logger.info(
                "guild=%s round %d: %d tool call(s): %s",
                guild_id,
                round_num,
                len(trimmed),
                [
                    {"tool_use_id": r.get("tool_use_id"), "is_error": r.get("is_error", False)}
                    for r in trimmed
                ],
            )
            messages.append({"role": "user", "content": trimmed})
        else:
            # Safety cap wrap-up (same as embedded runner)
            logger.warning("guild=%s hit %d-round safety cap", guild_id, config.max_rounds)
            messages = prune_history(messages)
            messages = strip_orphaned_tool_results(messages)
            _stamp_message_cache_breakpoint(messages)
            _wrap_raw = await client.messages.with_raw_response.create(
                model=config.effective_model,
                max_tokens=1024,
                system=system_blocks,
                messages=messages,
                tools=tools,
                tool_choice={"type": "none"},
            )
            wrap_resp = _wrap_raw.parse()
            wrap_usage = wrap_resp.usage
            _wrap_input = getattr(wrap_usage, "input_tokens", 0) or 0
            _wrap_output = getattr(wrap_usage, "output_tokens", 0) or 0
            _wrap_cache_read = getattr(wrap_usage, "cache_read_input_tokens", 0) or 0
            _wrap_cache_write = getattr(wrap_usage, "cache_creation_input_tokens", 0) or 0
            _wrap_request_id = _wrap_raw.headers.get("request-id")
            logger.info(
                "guild=%s wrap-up: stop_reason=%s input=%d cache_read=%d cache_write=%d output=%d request_id=%s",
                guild_id,
                wrap_resp.stop_reason,
                _wrap_input,
                _wrap_cache_read,
                _wrap_cache_write,
                _wrap_output,
                _wrap_request_id,
            )
            _wrap_api_meta = {
                "request_id": _wrap_request_id,
                "model": config.effective_model,
                "input_tokens": _wrap_input,
                "output_tokens": _wrap_output,
                "cache_read_tokens": _wrap_cache_read,
                "cache_write_tokens": _wrap_cache_write,
                "ts": datetime.now(UTC).isoformat(),
            }
            wrap_turn_id = await _save_turn(
                guild_id,
                user_id,
                "assistant",
                wrap_resp.content,
                http=http,
                api_calls=[_wrap_api_meta],
                task_id=task_id,
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
                            "taskId": _badge_task_id,
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
                    "taskId": _badge_task_id,
                }
            )

        response_text = "\n".join(text_parts).strip()
        if response_text:
            await http.save_message(
                "foreman",
                "user",
                response_text,
                user_id=user_id,
                task_id=_badge_task_id,
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
        return False

    return made_tool_calls
