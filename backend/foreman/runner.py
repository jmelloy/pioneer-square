"""Foreman AI runner: conversation management and main loop."""

import asyncio
import json
import logging
from datetime import UTC, datetime

from database import get_db
from events import broadcast
from models import ForemanTurn, Guild, Message, Task
from sqlalchemy import delete, select

from foreman.prompt import build_system_prompt
from foreman.tools import FOREMAN_TOOLS, exec_tools

try:
    import anthropic as _anthropic

    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

logger = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 8_000  # ~2 k tokens; cap per-result content before storing/sending
MAX_HISTORY_MESSAGES = 20  # sliding window cap on messages sent to Anthropic
_HUMAN_TURN_WINDOW = 5  # how many non-tool-response user turns to load from DB
_TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})
_24H_SECS = 86_400

POLL_MIN_SECS = 60  # initial poll interval: 1 minute
POLL_MAX_SECS = 3600  # maximum poll interval: 60 minutes

# Per-guild background poll task registry
_poll_tasks: dict[str, "asyncio.Task[None]"] = {}


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
    import copy

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


async def _get_guild_user_id(guild_id: str) -> str | None:
    db = await get_db()
    try:
        result = await db.execute(select(Guild.github_user_id).where(Guild.id == guild_id))
        return result.scalar_one_or_none()
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
        result = await db.execute(
            select(ForemanTurn)
            .where(ForemanTurn.guild_id == guild_id, ForemanTurn.user_id == user_id)
            .order_by(ForemanTurn.id)
        )
        turns = result.scalars().all()
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
        turn = ForemanTurn(
            guild_id=guild_id,
            user_id=user_id,
            role=role,
            content_json=_serialize_content(content),
            is_tool_response=1 if is_tool_response else 0,
            parent_id=parent_id,
            created_at=datetime.now(UTC).isoformat(),
        )
        db.add(turn)
        await db.commit()
        await db.refresh(turn)
        return turn.id
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

        # Query non-terminal tasks for this guild.
        db = await get_db()
        try:
            result = await db.execute(
                select(Task.id, Task.state, Task.name).where(
                    Task.guild_id == guild_id,
                    ~Task.state.in_(list(_TERMINAL_STATES)),
                )
            )
            active_tasks = [dict(r._mapping) for r in result.fetchall()]
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
            task_summary = "; ".join(f"{t['id']} ({t['state']})" for t in active_tasks[:8])
            msg = (
                f"[periodic-check] Automated status poll — {n} non-terminal "
                f"task(s): {task_summary}. Check whether any are stalled. "
                "Use get_task_status to inspect a task if it looks stuck. "
                "If everything looks healthy, no action is needed."
            )
            asyncio.create_task(run_foreman_ai(guild_id, msg))

        # Announce next check interval so the UI can display a countdown.
        interval = next_interval
        try:
            await broadcast(
                guild_id,
                {"type": "foreman-poll-status", "nextCheckIn": interval},
            )
        except Exception:
            pass


def reset_foreman_poll(guild_id: str) -> None:
    """Cancel any running poll loop for this guild and start a fresh one at POLL_MIN_SECS.

    Call whenever a significant event occurs (human message, worker state change,
    task transition) to reset the backoff so the next check happens in 1 minute.
    """
    old = _poll_tasks.pop(guild_id, None)
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(_poll_loop(guild_id))
    _poll_tasks[guild_id] = task


async def _fetch_online_workers(db, guild_id: str) -> list[dict]:
    """Return workers visible to the Foreman: only those whose ``state == 'online'``.

    Offline workers can't accept task assignments, so listing them in the system
    prompt just invites bad routing decisions. Orphan agents (no ``worker_id``)
    that are non-offline are still surfaced for legacy compatibility.
    """
    from sqlalchemy import text

    result = await db.execute(
        text(
            "SELECT w.id, w.repos, w.state as worker_state,"
            " COUNT(a.id) as agent_count,"
            " GROUP_CONCAT(a.id || ':' || a.state) as agents"
            " FROM workers w"
            " LEFT JOIN agents a ON a.worker_id = w.id AND a.state != 'offline'"
            " WHERE w.guild_id = :guild_id AND w.state = 'online'"
            " GROUP BY w.id"
            " UNION ALL"
            " SELECT a.id, '[]', a.state, 1, a.id || ':' || a.state"
            " FROM agents a"
            " WHERE a.guild_id = :guild_id AND a.type = 'worker'"
            " AND a.worker_id IS NULL AND a.state != 'offline'"
        ),
        {"guild_id": guild_id},
    )
    return [dict(r._mapping) for r in result.fetchall()]


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

    # Build live context for the system prompt
    db = await get_db()
    try:
        guild_result = await db.execute(
            select(Guild.name, Guild.primary_repo).where(Guild.id == guild_id)
        )
        guild_row = guild_result.one_or_none()
        primary_repo = guild_row.primary_repo if guild_row else None

        worker_rows = await _fetch_online_workers(db, guild_id)
        task_result = await db.execute(
            select(
                Task.id,
                Task.worker_id,
                Task.name,
                Task.description,
                Task.state,
                Task.branch,
                Task.pr_url,
                Task.finished_at,
            )
            .where(Task.guild_id == guild_id)
            .order_by(Task.created_at.desc())
            .limit(10)
        )
        task_rows = [
            {**dict(r._mapping), "description": dict(r._mapping).get("description") or ""}
            for r in task_result.fetchall()
        ]
        guild_result = await db.execute(select(Guild.primary_repo).where(Guild.id == guild_id))
        primary_repo: str | None = guild_result.scalar_one_or_none()
    finally:
        await db.close()

    workers_block = json.dumps(
        [
            {
                "id": r["id"],
                "state": r["worker_state"] or "idle",
                "repos": json.loads(r["repos"] or "[]"),
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
    tasks_block = json.dumps(summarized_tasks[:6], indent=2)
    system = build_system_prompt(
        workers_block, tasks_block, extra_context, primary_repo=primary_repo
    )

    logger.info(
        "guild=%s run_foreman_ai: workers=%d tasks_in_context=%d "
        "system_prompt_chars=%d extra_context_chars=%d",
        guild_id,
        len(worker_rows),
        len(summarized_tasks),
        len(system),
        len(extra_context),
    )
    logger.debug("guild=%s workers_block: %s", guild_id, workers_block)
    logger.debug("guild=%s tasks_block: %s", guild_id, tasks_block)

    # Persist system prompt for auditing; excluded when rebuilding messages for the API
    await _save_turn(guild_id, user_id, "system", system)
    # Persist and load the new human turn
    await _save_turn(guild_id, user_id, "user", human_message)
    messages = await _load_history(guild_id, user_id)

    logger.info(
        "guild=%s run_foreman_ai: %d messages loaded from history; human_message_chars=%d",
        guild_id,
        len(messages),
        len(human_message),
    )

    client = _anthropic.AsyncAnthropic()

    try:
        text_parts = []
        for round_num in range(6):  # safety cap on tool-call rounds
            messages = prune_history(messages)
            messages = strip_orphaned_tool_results(messages)
            logger.info(
                "guild=%s run_foreman_ai round %d: sending %d messages to Claude",
                guild_id,
                round_num,
                len(messages),
            )
            resp = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system,
                messages=messages,
                tools=FOREMAN_TOOLS,
            )
            logger.info(
                "guild=%s run_foreman_ai round %d: stop_reason=%s content_blocks=%d",
                guild_id,
                round_num,
                resp.stop_reason,
                len(resp.content),
            )

            # Persist assistant turn and append to local messages
            asst_turn_id = await _save_turn(guild_id, user_id, "assistant", resp.content)
            messages.append({"role": "assistant", "content": _serialize_content(resp.content)})
            # Re-parse so messages stays as plain dicts (not SDK objects)
            messages[-1]["content"] = json.loads(messages[-1]["content"])

            text_parts += [b.text for b in resp.content if b.type == "text" and b.text.strip()]

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break  # end_turn — foreman is done

            # Broadcast tool-use events so the frontend chat shows them live
            _now = datetime.now(UTC).isoformat()
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
                        "createdAt": _now,
                    },
                )

            tool_results = await exec_tools(guild_id, tool_uses)
            # Truncate verbose results before storing/sending
            trimmed = [
                {**r, "content": truncate_tool_result(r["content"])} if r.get("content") else r
                for r in tool_results
            ]

            # Broadcast tool-result events
            _now = datetime.now(UTC).isoformat()
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
                        "createdAt": _now,
                    },
                )
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

        response_text = "\n".join(text_parts).strip()
        if response_text:
            now = datetime.now(UTC).isoformat()
            await broadcast(
                guild_id,
                {
                    "type": "chat",
                    "from": "foreman",
                    "to": "user",
                    "content": response_text,
                    "createdAt": now,
                },
            )
            db = await get_db()
            try:
                db.add(
                    Message(
                        guild_id=guild_id,
                        from_agent="foreman",
                        to_agent="user",
                        content=response_text,
                        message_type="chat",
                        created_at=now,
                    )
                )
                await db.commit()
            finally:
                await db.close()

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


async def clear_foreman_history(guild_id: str, user_id: str) -> int:
    """Delete all stored turns for this guild+user. Returns count removed."""
    db = await get_db()
    try:
        result = await db.execute(
            delete(ForemanTurn).where(
                ForemanTurn.guild_id == guild_id, ForemanTurn.user_id == user_id
            )
        )
        await db.commit()
        return result.rowcount
    finally:
        await db.close()


async def get_foreman_history(guild_id: str, user_id: str) -> list[dict]:
    """Return all stored turns as JSON-safe dicts (for the debug endpoint)."""
    db = await get_db()
    try:
        result = await db.execute(
            select(ForemanTurn)
            .where(ForemanTurn.guild_id == guild_id, ForemanTurn.user_id == user_id)
            .order_by(ForemanTurn.id)
        )
        turns = result.scalars().all()
    finally:
        await db.close()

    return [
        {
            "id": t.id,
            "role": t.role,
            "is_tool_response": bool(t.is_tool_response),
            "parent_id": t.parent_id,
            "content": json.loads(t.content_json),
            "created_at": t.created_at,
        }
        for t in turns
    ]
