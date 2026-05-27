"""Foreman AI runner: conversation management and main loop (embedded)."""

import asyncio
import json
import logging
from datetime import UTC, datetime

from auth_deps import get_guild_pk
from database import get_db
from events import broadcast
from foreman.prompt import build_state_preamble, build_system_blocks, build_system_prompt
from foreman.tools import exec_tools
from foreman_core.constants import (
    _24H_SECS,
    _HUMAN_TURN_WINDOW,
    _TERMINAL_STATES,
    MAX_FOREMAN_ROUNDS,
)
from foreman_core.llm import HAS_ANTHROPIC, get_foreman_model, make_anthropic_client
from foreman_core.message_utils import (
    _inject_state_preamble,
    _serialize_content,
    _stamp_message_cache_breakpoint,
    _summarize_task,
    prune_history,
    strip_orphaned_tool_results,
    truncate_tool_result,
)
from foreman_core.tools_schema import FOREMAN_TOOLS
from models import Agent, ForemanTurn, Guild, GuildMember, Message, Task, Worker, live_tasks_filter
from sqlalchemy import delete, func
from sqlmodel import col, select
from util.tasks import spawn

logger = logging.getLogger(__name__)

POLL_MIN_SECS = 60  # initial poll interval: 1 minute
POLL_MAX_SECS = 3600  # maximum poll interval: 60 minutes

# Per-guild background poll task registry
_poll_tasks: dict[str, "asyncio.Task[None]"] = {}

# Module-level client — reused across calls so the underlying httpx connection
# pool isn't thrown away every invocation. Lazily initialised so import works
# without an API key (Anthropic) or AWS credentials (Bedrock).
_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = make_anthropic_client()
    return _anthropic_client


async def _get_guild_user_id(guild_id: str) -> str | None:
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            return None
        result = await db.exec(
            select(col(GuildMember.user_id))
            .where(col(GuildMember.guild_id) == guild_pk, col(GuildMember.role) == "owner")
            .limit(1)
        )
        return result.one_or_none()
    finally:
        await db.close()


async def _load_history(guild_id: str, user_id: str) -> list[dict]:
    """Load the last _HUMAN_TURN_WINDOW non-tool-response turns (plus all tool exchange
    turns between them) as a list of Anthropic-API-compatible message dicts.

    Because the cutoff always lands on a human-initiated user turn, every
    assistant-turn / tool_result-user-turn pair that follows it is guaranteed to
    be included intact — no orphaned tool_use blocks, no synthetic repairs needed.
    """
    db = await get_db()
    try:
        guild_pk_val = await get_guild_pk(db, guild_id)
        result = await db.exec(
            select(ForemanTurn)
            .where(col(ForemanTurn.guild_id) == guild_pk_val, col(ForemanTurn.user_id) == user_id)
            .order_by(col(ForemanTurn.id))
        )
        turns = result.all()
    finally:
        await db.close()

    logger.debug(
        "guild=%s _load_history: %d total turns in DB for user=%s", guild_id, len(turns), user_id
    )

    if not turns:
        return []

    # Walk backwards: find the index of the 5th-from-last non-tool-response user turn.
    cutoff = 0
    human_count = 0
    for i in range(len(turns) - 1, -1, -1):
        t = turns[i]
        if t.role == "user" and not t.is_tool_response:
            human_count += 1
            if human_count >= _HUMAN_TURN_WINDOW:
                cutoff = i
                break

    # Exclude system turns — they are persisted for auditing but must not appear
    # in the messages array sent to the Anthropic API (system prompt is a top-level param).
    messages = [
        {"role": t.role, "content": json.loads(t.content_json)}
        for t in turns[cutoff:]
        if t.role != "system"
    ]

    # Anthropic API requires the first message to have role "user"
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    logger.debug(
        "guild=%s _load_history: cutoff at turn index %d → %d messages after trim (roles: %s)",
        guild_id,
        cutoff,
        len(messages),
        [m["role"] for m in messages],
    )
    return messages


async def _save_turn(
    guild_id: str,
    user_id: str,
    role: str,
    content,
    *,
    is_tool_response: bool = False,
    parent_id: int | None = None,
) -> int:
    """Persist one turn to the DB. Returns the new row's id."""
    db = await get_db()
    try:
        guild_pk_val = await get_guild_pk(db, guild_id)
        turn = ForemanTurn(
            guild_id=guild_pk_val or 0,
            user_id=user_id,
            role=role,
            content_json=_serialize_content(content),
            is_tool_response=1 if is_tool_response else 0,
            parent_id=parent_id,
            created_at=datetime.now(UTC),
        )
        db.add(turn)
        await db.commit()
        await db.refresh(turn)
        return turn.id or 0
    finally:
        await db.close()


async def _update_turn_tokens(turn_id: int, input_tokens: int, output_tokens: int) -> None:
    """Write token counts back to an existing ForemanTurn row."""
    from sqlalchemy import update as sa_update

    db = await get_db()
    try:
        await db.exec(
            sa_update(ForemanTurn)
            .where(col(ForemanTurn.id) == turn_id)
            .values(input_tokens=input_tokens, output_tokens=output_tokens)
        )
        await db.commit()
    finally:
        await db.close()


async def _poll_loop(guild_id: str) -> None:
    """Background loop: check non-terminal tasks and call the foreman if any are found.

    Interval doubles each cycle from POLL_MIN_SECS up to POLL_MAX_SECS.
    Cancelled (via reset_foreman_poll) whenever a significant event arrives.
    The foreman-poll-status broadcast is sent after each poll so the UI can
    display the countdown to the *next* check.
    """
    interval = POLL_MIN_SECS
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

        # Bail if this task was superseded by a reset.
        if asyncio.current_task() is not _poll_tasks.get(guild_id):
            return

        try:
            # If an external foreman is connected for this guild it owns the
            # poll loop — skip the embedded run to avoid double-triggering.
            from events import foreman_connections

            if guild_id in foreman_connections:
                next_interval = min(interval * 2, POLL_MAX_SECS)
                logger.debug(
                    "guild=%s external foreman connected, skipping embedded poll", guild_id
                )
                interval = next_interval
                await broadcast(
                    guild_id,
                    {"type": "foreman-poll-status", "nextCheckIn": interval},
                )
                continue

            db = await get_db()
            try:
                guild_pk_val = await get_guild_pk(db, guild_id)
                result = await db.exec(
                    select(col(Task.id), col(Task.state), col(Task.name)).where(
                        col(Task.guild_id) == guild_pk_val,
                        ~col(Task.state).in_(list(_TERMINAL_STATES)),
                        live_tasks_filter(),
                    )
                )
                active_tasks = [dict(r._mapping) for r in result.all()]
            finally:
                await db.close()

            n = len(active_tasks)
            next_interval = min(interval * 2, POLL_MAX_SECS)
            logger.debug(
                "guild=%s polling %d active tasks, next check in %.0fm",
                guild_id,
                n,
                next_interval / 60,
            )

            if active_tasks:
                task_summary = "; ".join(f"{t['id']} ({t['state']})" for t in active_tasks)
                msg = (
                    f"[periodic-check] Automated status poll — {n} non-terminal "
                    f"task(s): {task_summary}. Check whether any are stalled. "
                    "Use get_task_status to inspect a task if it looks stuck. "
                    "If everything looks healthy, no action is needed."
                )
                spawn(run_foreman_ai(guild_id, msg), name=f"foreman.poll:{guild_id}")

            # Announce next check interval so the UI can display a countdown.
            interval = next_interval
            await broadcast(
                guild_id,
                {"type": "foreman-poll-status", "nextCheckIn": interval},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("guild=%s _poll_loop iteration failed", guild_id)


def reset_foreman_poll(guild_id: str) -> None:
    """Cancel any running poll loop for this guild and start a fresh one at POLL_MIN_SECS.

    Call whenever a significant event occurs (human message, worker state change,
    task transition) to reset the backoff so the next check happens in 1 minute.
    """
    old = _poll_tasks.pop(guild_id, None)
    if old and not old.done():
        old.cancel()
    task = spawn(_poll_loop(guild_id), name=f"foreman.poll-loop:{guild_id}")
    _poll_tasks[guild_id] = task


async def _fetch_online_workers(db, guild_id: str) -> list[dict]:
    """Return online workers for the Foreman, with their active agent count and summary.

    Offline workers can't accept task assignments, so they are excluded.
    Each row's ``agents`` field is a comma-separated ``agentId:state`` string
    (empty string when a worker has no non-offline agents).
    """
    guild_pk_val = await get_guild_pk(db, guild_id)
    result = await db.exec(
        select(
            col(Worker.id),
            col(Worker.repos),
            col(Worker.org),
            col(Worker.state).label("worker_state"),
            func.count(col(Agent.id)).label("agent_count"),
            func.string_agg(col(Agent.id) + ":" + col(Agent.state), ",").label("agents"),
        )
        .outerjoin(
            Agent, (col(Agent.worker_id) == col(Worker.id)) & (col(Agent.state) != "offline")
        )
        .where(col(Worker.guild_id) == guild_pk_val, col(Worker.state) == "online")
        .group_by(col(Worker.id))
    )
    return [dict(r._mapping) for r in result.all()]


async def run_foreman_ai(
    guild_id: str,
    human_message: str,
    extra_context: str = "",
    user_id: str | None = None,
):
    """Process a human message (or system escalation) through the Claude foreman AI."""
    if not HAS_ANTHROPIC:
        now = datetime.now(UTC).isoformat()
        await broadcast(
            guild_id,
            {
                "type": "chat",
                "from": "foreman",
                "to": "user",
                "content": "Foreman AI offline (install `anthropic` package to enable).",
                "createdAt": now,
            },
        )
        return

    if not user_id:
        user_id = await _get_guild_user_id(guild_id) or guild_id

    # Build live context for the system prompt.  The session is kept open until
    # the end of the function so the tool_use / tool_result Message rows and the
    # final chat Message can all be written without opening fresh connections.
    db = await get_db()
    try:
        guild_result = await db.exec(
            select(col(Guild.name), col(Guild.primary_repo)).where(col(Guild.guild_id) == guild_id)
        )
        guild_row = guild_result.one_or_none()
        primary_repo = guild_row.primary_repo if guild_row else None

        worker_rows = await _fetch_online_workers(db, guild_id)
        guild_pk_val = await get_guild_pk(db, guild_id)
        if guild_pk_val is None:
            raise ValueError(f"Guild not found: {guild_id}")
        task_result = await db.exec(
            select(
                col(Task.id),
                col(Task.worker_id),
                col(Task.name),
                col(Task.description),
                col(Task.state),
                col(Task.branch),
                col(Task.pr_url),
                col(Task.finished_at),
            )
            .where(
                col(Task.guild_id) == guild_pk_val,
                ~col(Task.state).in_(list(_TERMINAL_STATES)),
                live_tasks_filter(),
            )
            .order_by(col(Task.created_at).desc())
        )
        task_rows = [
            {**dict(r._mapping), "description": dict(r._mapping).get("description") or ""}
            for r in task_result.all()
        ]
    except Exception:
        await db.close()
        raise

    try:
        workers_block = json.dumps(
            [
                {
                    "id": r["id"],
                    "state": r["worker_state"] or "idle",
                    "repos": json.loads(r["repos"] or "[]"),
                    **({"org": r["org"]} if r.get("org") else {}),
                    "agent_count": r["agent_count"] or 0,
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
        # Legacy single-string render — persisted for audit only, not sent to the API.
        audit_system = build_system_prompt(
            workers_block, tasks_block, extra_context, primary_repo=primary_repo
        )

        logger.info(
            "guild=%s run_foreman_ai: workers=%d tasks_in_context=%d "
            "system_chars=%d state_chars=%d extra_context_chars=%d",
            guild_id,
            len(worker_rows),
            len(summarized_tasks),
            len(system_blocks[0]["text"]),
            len(state_preamble),
            len(extra_context),
        )
        logger.debug("guild=%s workers_block: %s", guild_id, workers_block)
        logger.debug("guild=%s tasks_block: %s", guild_id, tasks_block)

        # Persist the rendered prompt + human turn for auditing; the API receives
        # `system_blocks` (cacheable) and the state preamble injected at send time.
        await _save_turn(guild_id, user_id, "system", audit_system)
        await _save_turn(guild_id, user_id, "user", human_message)
        messages = await _load_history(guild_id, user_id)

        logger.info(
            "guild=%s run_foreman_ai: %d messages loaded from history; human_message_chars=%d",
            guild_id,
            len(messages),
            len(human_message),
        )

        # Inject live state into the just-loaded current human turn so it travels
        # with this call only — the DB still holds just the human's literal text.
        _inject_state_preamble(messages, state_preamble)

        client = _get_anthropic_client()

        text_parts = []
        for round_num in range(MAX_FOREMAN_ROUNDS):
            messages = prune_history(messages)
            messages = strip_orphaned_tool_results(messages)
            _stamp_message_cache_breakpoint(messages)
            logger.info(
                "guild=%s run_foreman_ai round %d: sending %d messages to Claude",
                guild_id,
                round_num,
                len(messages),
            )
            resp = await client.messages.create(
                model=get_foreman_model(),
                max_tokens=1024,
                system=system_blocks,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
                tools=FOREMAN_TOOLS,  # type: ignore[arg-type]
            )
            usage = resp.usage
            _input_tokens = getattr(usage, "input_tokens", 0) or 0
            _output_tokens = getattr(usage, "output_tokens", 0) or 0
            logger.info(
                "guild=%s run_foreman_ai round %d: stop_reason=%s content_blocks=%d "
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

            # Persist assistant turn and append to local messages
            asst_turn_id = await _save_turn(guild_id, user_id, "assistant", resp.content)
            await _update_turn_tokens(asst_turn_id, _input_tokens, _output_tokens)
            messages.append({"role": "assistant", "content": _serialize_content(resp.content)})
            # Re-parse so messages stays as plain dicts (not SDK objects)
            messages[-1]["content"] = json.loads(messages[-1]["content"])

            # Emit text blocks immediately so narration appears inline with tool calls,
            # not batched at the end of the turn.
            _now = datetime.now(UTC)
            for b in resp.content:
                if b.type == "text" and b.text.strip():
                    text_parts.append(b.text.strip())
                    await broadcast(
                        guild_id,
                        {
                            "type": "chat",
                            "from": "foreman",
                            "to": "user",
                            "content": b.text.strip(),
                            "createdAt": _now.isoformat(),
                        },
                    )

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break  # end_turn — foreman is done

            # Broadcast tool-use events so the frontend chat shows them live
            for tu in tool_uses:
                await broadcast(
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
                        "createdAt": _now.isoformat(),
                    },
                )

            _tool_use_ts = _now  # capture before exec_tools may raise

            tool_results = await exec_tools(guild_id, tool_uses, user_id=user_id)
            # Truncate verbose results; filter to only IDs in the current batch so
            # stale results that survived history trimming are never persisted.
            current_tool_use_ids = {tu.id for tu in tool_uses}
            trimmed = [
                {**r, "content": truncate_tool_result(r["content"])} if r.get("content") else r
                for r in tool_results
                if r.get("tool_use_id") in current_tool_use_ids
            ]

            # Broadcast tool-result events
            _now = datetime.now(UTC)
            for result in trimmed:
                await broadcast(
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
                        "createdAt": _now.isoformat(),
                    },
                )

            # Persist tool_use and tool_result together in one transaction
            # so exec_tools raising never leaves tool_use rows without their results.
            try:
                for tu in tool_uses:
                    db.add(
                        Message(
                            guild_id=guild_pk_val,
                            from_agent="foreman",
                            to_agent="user",
                            content=f"▶ {tu.name}",
                            message_type="chat",
                            role="tool_use",
                            meta=json.dumps(
                                {
                                    "toolId": tu.id,
                                    "toolName": tu.name,
                                    "toolInput": dict(tu.input) if tu.input else {},
                                }
                            ),
                            created_at=_tool_use_ts,
                        )
                    )
                for result in trimmed:
                    db.add(
                        Message(
                            guild_id=guild_pk_val,
                            from_agent="foreman",
                            to_agent="user",
                            content=result.get("content", "") or "",
                            message_type="chat",
                            role="tool_result",
                            meta=json.dumps(
                                {
                                    "toolId": result.get("tool_use_id"),
                                    "isError": result.get("is_error", False),
                                }
                            ),
                            created_at=_now,
                        )
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

            # Persist tool_result turn as a child of the assistant turn
            await _save_turn(
                guild_id,
                user_id,
                "user",
                trimmed,
                is_tool_response=True,
                parent_id=asst_turn_id,
            )
            logger.info(
                "guild=%s round %d: %d tool call(s) dispatched: %s",
                guild_id,
                round_num,
                len(trimmed),
                [
                    {
                        "tool_use_id": r["tool_use_id"],
                        "name": r.get("name"),
                        "is_error": r.get("is_error", False),
                    }
                    for r in trimmed
                ],
            )
            messages.append({"role": "user", "content": trimmed})
        else:
            # Loop exhausted: round MAX_FOREMAN_ROUNDS-1 returned tool_uses and
            # we just executed them, but have no rounds left to send results
            # back. Force a final tool-free wrap-up so the conversation ends
            # cleanly (no orphaned tool_use, no consecutive user turns) and
            # the human gets a summary of what the foreman accomplished.
            logger.warning(
                "guild=%s run_foreman_ai: hit %d-round safety cap, forcing wrap-up",
                guild_id,
                MAX_FOREMAN_ROUNDS,
            )
            messages = prune_history(messages)
            messages = strip_orphaned_tool_results(messages)
            _stamp_message_cache_breakpoint(messages)
            wrap_resp = await client.messages.create(
                model=get_foreman_model(),
                max_tokens=1024,
                system=system_blocks,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
                tools=FOREMAN_TOOLS,  # type: ignore[arg-type]
                tool_choice={"type": "none"},  # type: ignore[arg-type]
            )
            wrap_usage = wrap_resp.usage
            _wrap_input = getattr(wrap_usage, "input_tokens", 0) or 0
            _wrap_output = getattr(wrap_usage, "output_tokens", 0) or 0
            logger.info(
                "guild=%s run_foreman_ai wrap-up: stop_reason=%s "
                "input=%d cache_read=%d cache_write=%d output=%d",
                guild_id,
                wrap_resp.stop_reason,
                _wrap_input,
                getattr(wrap_usage, "cache_read_input_tokens", 0) or 0,
                getattr(wrap_usage, "cache_creation_input_tokens", 0) or 0,
                _wrap_output,
            )
            wrap_turn_id = await _save_turn(guild_id, user_id, "assistant", wrap_resp.content)
            await _update_turn_tokens(wrap_turn_id, _wrap_input, _wrap_output)
            _now = datetime.now(UTC).isoformat()
            for b in wrap_resp.content:
                if b.type == "text" and b.text.strip():
                    text_parts.append(b.text.strip())
                    await broadcast(
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
            await broadcast(
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
            now = datetime.now(UTC)
            db.add(
                Message(
                    guild_id=guild_pk_val,
                    from_agent="foreman",
                    to_agent="user",
                    content=response_text,
                    message_type="chat",
                    created_at=now,
                )
            )
            await db.commit()

    except Exception as exc:
        now = datetime.now(UTC).isoformat()
        await broadcast(
            guild_id,
            {
                "type": "chat",
                "from": "foreman",
                "to": "user",
                "content": f"Foreman error: {exc}",
                "createdAt": now,
            },
        )
    finally:
        await db.close()


async def clear_foreman_history(guild_id: str, user_id: str) -> int:
    """Delete all stored turns for this guild+user. Returns count removed."""
    db = await get_db()
    try:
        guild_pk_val = await get_guild_pk(db, guild_id)
        result = await db.exec(
            delete(ForemanTurn).where(
                col(ForemanTurn.guild_id) == guild_pk_val, col(ForemanTurn.user_id) == user_id
            )
        )
        await db.commit()
        return getattr(result, "rowcount", 0) or 0
    finally:
        await db.close()


async def get_foreman_history(guild_id: str, user_id: str) -> dict:
    """Return stored turns structured for the debug view.

    Returns::

        {
            "system": <str | None>,   # most-recent audit system prompt text
            "messages": [...],        # non-system turns only, in DB order
        }

    System turns are persisted for auditing but are never part of the
    ``messages[]`` array sent to the Anthropic API — they appear here once
    at the top so the debug pane accurately mirrors what would be sent.
    """
    db = await get_db()
    try:
        guild_pk_val = await get_guild_pk(db, guild_id)
        result = await db.exec(
            select(ForemanTurn)
            .where(col(ForemanTurn.guild_id) == guild_pk_val, col(ForemanTurn.user_id) == user_id)
            .order_by(col(ForemanTurn.id))
        )
        turns = result.all()
    finally:
        await db.close()

    # Grab the most-recent system turn (there's one per invocation; we only
    # show the latest to avoid duplicates in the debug pane).
    system_content: str | None = None
    for t in reversed(turns):
        if t.role == "system":
            raw = json.loads(t.content_json)
            system_content = raw if isinstance(raw, str) else json.dumps(raw)
            break

    messages = [
        {
            "id": t.id,
            "role": t.role,
            "is_tool_response": bool(t.is_tool_response),
            "parent_id": t.parent_id,
            "content": json.loads(t.content_json),
            "created_at": t.created_at,
            "input_tokens": t.input_tokens,
            "output_tokens": t.output_tokens,
        }
        for t in turns
        if t.role != "system"
    ]

    return {"system": system_content, "messages": messages}
