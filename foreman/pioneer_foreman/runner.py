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

from .prompt import build_state_preamble, build_system_blocks, build_system_prompt

if TYPE_CHECKING:
    from .config import Config
    from .http_client import ForemanHTTPClient

logger = logging.getLogger(__name__)

# Client cache keyed on (provider, region, api_key) so different configs
# get different clients instead of silently reusing the first one created.
_anthropic_clients: dict[tuple, object] = {}


def _get_anthropic_client(config: Config):
    provider = getattr(config, "provider", "anthropic") or "anthropic"
    region = getattr(config, "aws_region", None)
    api_key = getattr(config, "api_key", None) or ""
    cache_key = (provider.lower(), region, api_key)
    if cache_key not in _anthropic_clients:
        _anthropic_clients[cache_key] = make_anthropic_client(
            provider=provider,
            api_key=api_key or None,
            region=region,
        )
    return _anthropic_clients[cache_key]


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

        await _save_turn(guild_id, user_id, "system", audit_system, http=http)
        await _save_turn(guild_id, user_id, "user", human_message, http=http)
        messages = await _load_history(guild_id, user_id, http=http)

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
            resp = await client.messages.create(
                model=config.effective_model,
                max_tokens=1024,
                system=system_blocks,
                messages=messages,
                tools=FOREMAN_TOOLS,
            )
            usage = resp.usage
            _input_tokens = getattr(usage, "input_tokens", 0) or 0
            _output_tokens = getattr(usage, "output_tokens", 0) or 0
            logger.info(
                "guild=%s round %d: stop_reason=%s content_blocks=%d "
                "input=%d cache_read=%d cache_write=%d output=%d",
                guild_id,
                round_num,
                resp.stop_reason,
                len(resp.content),
                _input_tokens,
                getattr(usage, "cache_read_input_tokens", 0) or 0,
                getattr(usage, "cache_creation_input_tokens", 0) or 0,
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
                model=config.effective_model,
                max_tokens=1024,
                system=system_blocks,
                messages=messages,
                tools=FOREMAN_TOOLS,
                tool_choice={"type": "none"},
            )
            wrap_usage = wrap_resp.usage
            _wrap_input = getattr(wrap_usage, "input_tokens", 0) or 0
            _wrap_output = getattr(wrap_usage, "output_tokens", 0) or 0
            logger.info(
                "guild=%s wrap-up: stop_reason=%s input=%d cache_read=%d cache_write=%d output=%d",
                guild_id,
                wrap_resp.stop_reason,
                _wrap_input,
                getattr(wrap_usage, "cache_read_input_tokens", 0) or 0,
                getattr(wrap_usage, "cache_creation_input_tokens", 0) or 0,
                _wrap_output,
            )
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
