"""Foreman AI runner: REST-based conversation management and main AI loop.

Mirrors backend/foreman/runner.py but replaces every direct DB call with a
REST API call so the standalone foreman has no backend package dependency.

Key differences from the embedded runner:
- History is loaded/saved via GET/POST /guilds/{guild_id}/foreman/history.
- Guild state is fetched via GET /guilds/{guild_id}/foreman/state.
- Broadcasts are sent through the broadcast_fn callback (WS relay).
- Final chat messages are persisted via POST /guilds/{guild_id}/messages.
- The poll loop polls the state endpoint instead of querying the DB directly.
- user_id falls back to guild_id when no guild member lookup is possible.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import UTC, datetime

import anthropic
import httpx

from pioneer_foreman.prompt import build_state_preamble, build_system_blocks
from pioneer_foreman.tools import FOREMAN_TOOLS, BroadcastFn, exec_tools

logger = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 8_000
MAX_HISTORY_MESSAGES = 20
MAX_FOREMAN_ROUNDS = 10
_HUMAN_TURN_WINDOW = 5
_TERMINAL_STATES = frozenset({"done", "failed", "cancelled", "error"})
_24H_SECS = 86_400

POLL_MIN_SECS = 60
POLL_MAX_SECS = 3600

# Module-level Anthropic client — reused across calls.
_anthropic_client: anthropic.AsyncAnthropic | None = None

# Per-guild background poll task registry
_poll_tasks: dict[str, asyncio.Task] = {}


def _get_anthropic_client() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic()
    return _anthropic_client


def _serialize_content(content) -> str:
    """Convert SDK content objects or dicts to a JSON string for storage."""
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


def _inject_state_preamble(messages: list[dict], state_preamble: str) -> None:
    """Prepend the live-state block to the last user turn (in place)."""
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
    """Move the messages-level cache breakpoint to the last block of the last turn."""
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
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + f"\n… [truncated: {len(content) - max_chars} chars omitted]"


def prune_history(messages: list, max_messages: int = MAX_HISTORY_MESSAGES) -> list:
    if len(messages) > max_messages:
        messages = messages[-max_messages:]
    while messages and messages[0]["role"] != "user":
        messages = messages[1:]
    return messages


def strip_orphaned_tool_results(messages: list[dict]) -> list[dict]:
    """Remove orphaned tool_result/tool_use blocks from messages."""
    out = copy.deepcopy(messages)
    i = 0
    while i < len(out):
        msg = out[i]
        content = msg.get("content")

        if msg["role"] == "assistant" and isinstance(content, list):
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
    state = task.get("state", "")
    if state not in _TERMINAL_STATES:
        return task
    finished_at = task.get("finished_at")
    if finished_at:
        try:
            finished_ts = datetime.fromisoformat(finished_at.replace("Z", "+00:00")).timestamp()
            if finished_ts < cutoff_ts:
                return None
        except (ValueError, AttributeError):
            pass
    return {k: v for k, v in task.items() if k != "description"}


async def _load_history(
    guild_id: str,
    user_id: str,
    *,
    http: httpx.AsyncClient,
) -> list[dict]:
    """Load conversation history via REST and apply the sliding-window filter."""
    try:
        resp = await http.get(
            f"/guilds/{guild_id}/foreman/history",
            params={"user_id": user_id},
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        turns = resp.json()
    except Exception as exc:
        logger.warning("guild=%s _load_history failed: %s", guild_id, exc)
        return []

    if not turns:
        return []

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

    logger.debug(
        "guild=%s _load_history: %d turns from API → %d messages after window",
        guild_id,
        len(turns),
        len(messages),
    )
    return messages


async def _save_turn(
    guild_id: str,
    user_id: str,
    role: str,
    content,
    *,
    http: httpx.AsyncClient,
    is_tool_response: bool = False,
    parent_id: int | None = None,
) -> int | None:
    """Persist one turn via REST. Returns the new row ID, or None on failure."""
    try:
        resp = await http.post(
            f"/guilds/{guild_id}/foreman/history",
            json={
                "user_id": user_id,
                "role": role,
                "content_json": _serialize_content(content),
                "is_tool_response": is_tool_response,
                "parent_id": parent_id,
            },
        )
        resp.raise_for_status()
        return resp.json()["id"]
    except Exception as exc:
        logger.warning("guild=%s _save_turn(%s) failed: %s", guild_id, role, exc)
        return None


async def run_foreman_ai(
    guild_id: str,
    human_message: str,
    extra_context: str = "",
    user_id: str | None = None,
    *,
    http: httpx.AsyncClient,
    broadcast_fn: BroadcastFn,
    github_token: str | None = None,
    model: str = "claude-sonnet-4-6",
) -> None:
    """Process a human message or system escalation through the Claude foreman AI.

    Equivalent to backend/foreman/runner.run_foreman_ai() but uses REST for all
    state I/O.  The httpx.AsyncClient must already have Authorization headers set.
    """
    if not user_id:
        user_id = guild_id

    # Fetch guild state snapshot
    try:
        state_resp = await http.get(f"/guilds/{guild_id}/foreman/state")
        state_resp.raise_for_status()
        state = state_resp.json()
    except Exception as exc:
        now = datetime.now(UTC).isoformat()
        logger.error("guild=%s failed to fetch state: %s", guild_id, exc)
        await broadcast_fn(
            guild_id,
            {
                "type": "chat",
                "from": "foreman",
                "to": "user",
                "content": f"Foreman error fetching guild state: {exc}",
                "createdAt": now,
            },
        )
        return

    guild_data = state.get("guild") or {}
    workers_data = state.get("workers") or []
    tasks_data = state.get("tasks") or []
    primary_repo = guild_data.get("primary_repo")

    workers_block = json.dumps(
        [
            {
                "id": w["id"],
                "state": w.get("state") or "idle",
                "repos": w.get("repos") or [],
                **({"org": w["org"]} if w.get("org") else {}),
                "agent_count": w.get("agent_count") or 0,
            }
            for w in workers_data
        ],
        indent=2,
    )
    cutoff_ts = datetime.now(UTC).timestamp() - _24H_SECS
    summarized_tasks = [
        s for row in tasks_data if (s := _summarize_task(row, cutoff_ts)) is not None
    ]
    tasks_block = json.dumps(summarized_tasks, indent=2)

    system_blocks = build_system_blocks(primary_repo=primary_repo)
    state_preamble = build_state_preamble(workers_block, tasks_block, extra_context)
    audit_system = f"{system_blocks[0]['text']}\n\n{state_preamble}"

    logger.info(
        "guild=%s run_foreman_ai: workers=%d tasks_in_context=%d "
        "state_chars=%d extra_context_chars=%d",
        guild_id,
        len(workers_data),
        len(summarized_tasks),
        len(state_preamble),
        len(extra_context),
    )

    await _save_turn(guild_id, user_id, "system", audit_system, http=http)
    await _save_turn(guild_id, user_id, "user", human_message, http=http)
    messages = await _load_history(guild_id, user_id, http=http)

    if not messages:
        messages = [{"role": "user", "content": human_message}]

    _inject_state_preamble(messages, state_preamble)

    anthropic_client = _get_anthropic_client()

    try:
        text_parts: list[str] = []

        for round_num in range(MAX_FOREMAN_ROUNDS):
            messages = prune_history(messages)
            messages = strip_orphaned_tool_results(messages)
            _stamp_message_cache_breakpoint(messages)
            logger.info(
                "guild=%s round %d: sending %d messages to Claude",
                guild_id,
                round_num,
                len(messages),
            )
            resp = await anthropic_client.messages.create(
                model=model,
                max_tokens=1024,
                system=system_blocks,
                messages=messages,
                tools=FOREMAN_TOOLS,
            )
            usage = resp.usage
            _in = getattr(usage, "input_tokens", 0) or 0
            _out = getattr(usage, "output_tokens", 0) or 0
            logger.info(
                "guild=%s round %d: stop_reason=%s blocks=%d "
                "in=%d cache_read=%d cache_write=%d out=%d",
                guild_id,
                round_num,
                resp.stop_reason,
                len(resp.content),
                _in,
                getattr(usage, "cache_read_input_tokens", 0) or 0,
                getattr(usage, "cache_creation_input_tokens", 0) or 0,
                _out,
            )

            asst_turn_id = await _save_turn(
                guild_id,
                user_id,
                "assistant",
                resp.content,
                http=http,
            )

            messages.append({"role": "assistant", "content": _serialize_content(resp.content)})
            messages[-1]["content"] = json.loads(messages[-1]["content"])

            _now = datetime.now(UTC).isoformat()
            for b in resp.content:
                if b.type == "text" and b.text.strip():
                    text_parts.append(b.text.strip())
                    await broadcast_fn(
                        guild_id,
                        {
                            "type": "chat",
                            "from": "foreman",
                            "to": "user",
                            "content": b.text.strip(),
                            "createdAt": _now,
                        },
                    )

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break

            for tu in tool_uses:
                await broadcast_fn(
                    guild_id,
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
                    },
                )

            tool_results = await exec_tools(
                guild_id,
                tool_uses,
                client=http,
                broadcast_fn=broadcast_fn,
                github_token=github_token,
                user_id=user_id,
            )
            trimmed = [
                {**r, "content": truncate_tool_result(r["content"])} if r.get("content") else r
                for r in tool_results
            ]

            _now = datetime.now(UTC).isoformat()
            for result in trimmed:
                await broadcast_fn(
                    guild_id,
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
                    },
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
                [{"id": r["tool_use_id"], "err": r.get("is_error", False)} for r in trimmed],
            )
            messages.append({"role": "user", "content": trimmed})

        else:
            # Safety cap hit — force a tool-free wrap-up turn
            logger.warning(
                "guild=%s hit %d-round safety cap, forcing wrap-up",
                guild_id,
                MAX_FOREMAN_ROUNDS,
            )
            messages = prune_history(messages)
            messages = strip_orphaned_tool_results(messages)
            _stamp_message_cache_breakpoint(messages)
            wrap_resp = await anthropic_client.messages.create(
                model=model,
                max_tokens=1024,
                system=system_blocks,
                messages=messages,
                tools=FOREMAN_TOOLS,
                tool_choice={"type": "none"},
            )
            await _save_turn(guild_id, user_id, "assistant", wrap_resp.content, http=http)
            _now = datetime.now(UTC).isoformat()
            for b in wrap_resp.content:
                if b.type == "text" and b.text.strip():
                    text_parts.append(b.text.strip())
                    await broadcast_fn(
                        guild_id,
                        {
                            "type": "chat",
                            "from": "foreman",
                            "to": "user",
                            "content": b.text.strip(),
                            "createdAt": _now,
                        },
                    )
            cap_note = f"_(Foreman hit {MAX_FOREMAN_ROUNDS}-round safety cap and stopped.)_"
            text_parts.append(cap_note)
            await broadcast_fn(
                guild_id,
                {
                    "type": "chat",
                    "from": "foreman",
                    "to": "user",
                    "content": cap_note,
                    "createdAt": _now,
                },
            )

        response_text = "\n".join(text_parts).strip()
        if response_text:
            try:
                await http.post(
                    f"/guilds/{guild_id}/messages",
                    json={
                        "from_agent": "foreman",
                        "to_agent": "user",
                        "content": response_text,
                        "message_type": "chat",
                        "user_id": user_id,
                    },
                )
            except Exception as exc:
                logger.warning("guild=%s failed to persist chat message: %s", guild_id, exc)

    except Exception as exc:
        now = datetime.now(UTC).isoformat()
        logger.exception("guild=%s run_foreman_ai failed", guild_id)
        await broadcast_fn(
            guild_id,
            {
                "type": "chat",
                "from": "foreman",
                "to": "user",
                "content": f"Foreman error: {exc}",
                "createdAt": now,
            },
        )


async def _poll_loop(
    guild_id: str,
    *,
    http: httpx.AsyncClient,
    broadcast_fn: BroadcastFn,
    github_token: str | None,
    model: str,
    poll_interval: float,
) -> None:
    """Background loop: poll non-terminal tasks and trigger the foreman if any are active.

    Interval doubles each cycle from poll_interval (config value) up to POLL_MAX_SECS.
    Cancelled via reset_foreman_poll() whenever a significant event arrives.
    """
    interval = poll_interval
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

        if asyncio.current_task() is not _poll_tasks.get(guild_id):
            return

        try:
            resp = await http.get(f"/guilds/{guild_id}/foreman/state")
            resp.raise_for_status()
            state = resp.json()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("guild=%s poll_loop state fetch failed: %s", guild_id, exc)
            interval = min(interval * 2, POLL_MAX_SECS)
            continue

        tasks_data = state.get("tasks") or []
        active_tasks = [t for t in tasks_data if t.get("state") not in _TERMINAL_STATES]
        next_interval = min(interval * 2, POLL_MAX_SECS)

        logger.debug(
            "guild=%s polling %d active task(s), next check in %.0fm",
            guild_id,
            len(active_tasks),
            next_interval / 60,
        )

        if active_tasks:
            task_summary = "; ".join(f"{t['id']} ({t.get('state', '?')})" for t in active_tasks)
            n = len(active_tasks)
            msg = (
                f"[periodic-check] Automated status poll — {n} non-terminal "
                f"task(s): {task_summary}. Check whether any are stalled. "
                "Use get_task_status to inspect a task if it looks stuck. "
                "If everything looks healthy, no action is needed."
            )
            asyncio.create_task(
                run_foreman_ai(
                    guild_id,
                    msg,
                    http=http,
                    broadcast_fn=broadcast_fn,
                    github_token=github_token,
                    model=model,
                ),
                name=f"foreman.poll:{guild_id}",
            )

        interval = next_interval
        try:
            await broadcast_fn(
                guild_id,
                {"type": "foreman-poll-status", "nextCheckIn": interval},
            )
        except Exception as exc:
            logger.warning("guild=%s poll_loop broadcast failed: %s", guild_id, exc)


def reset_foreman_poll(
    guild_id: str,
    *,
    http: httpx.AsyncClient,
    broadcast_fn: BroadcastFn,
    github_token: str | None,
    model: str,
    poll_interval: float,
) -> None:
    """Cancel any running poll loop and start a fresh one at poll_interval seconds."""
    old = _poll_tasks.pop(guild_id, None)
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(
        _poll_loop(
            guild_id,
            http=http,
            broadcast_fn=broadcast_fn,
            github_token=github_token,
            model=model,
            poll_interval=poll_interval,
        ),
        name=f"foreman.poll-loop:{guild_id}",
    )
    _poll_tasks[guild_id] = task
