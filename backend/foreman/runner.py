"""Foreman AI runner: conversation management and main loop."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, text

from database import get_db
from events import broadcast
from foreman.prompt import build_system_prompt
from foreman.state import foreman_conversations
from foreman.tools import FOREMAN_TOOLS, exec_tools
from models import Message, Task

try:
    import anthropic as _anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

logger = logging.getLogger(__name__)


def serialize_history_for_debug(history: list) -> list:
    """Convert in-memory foreman history (may contain SDK objects) to JSON-safe dicts."""
    out = []
    for msg in history:
        role = msg.get("role", "?") if isinstance(msg, dict) else getattr(msg, "role", "?")
        content = msg.get("content", []) if isinstance(msg, dict) else getattr(msg, "content", [])
        if isinstance(content, str):
            out.append({"role": role, "content": content})
        elif isinstance(content, list):
            blocks = []
            for b in content:
                if isinstance(b, dict):
                    blocks.append(b)
                else:
                    try:
                        blocks.append(b.model_dump())
                    except AttributeError:
                        blocks.append({"type": str(getattr(b, "type", "unknown")), "raw": str(b)})
            out.append({"role": role, "content": blocks})
        else:
            out.append({"role": role, "content": str(content)})
    return out


def _repair_tool_pairs(messages: list) -> list:
    """
    Ensure every tool_use block in an assistant turn has a matching tool_result
    in the immediately following user turn.

    When the conversation history is trimmed or otherwise truncated, assistant
    messages that contain tool_use blocks can lose their paired tool_result
    responses.  The Anthropic API rejects such histories.  This function does a
    single forward pass and inserts a synthetic error tool_result for every
    orphaned tool_use ID, keeping the history self-consistent.
    """
    result: list = []
    i = 0
    while i < len(messages):
        msg = messages[i]

        if msg.get("role") != "assistant":
            result.append(msg)
            i += 1
            continue

        content = msg.get("content", [])
        tool_use_ids: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "tool_use":
                    tool_use_ids.append(block["id"])
            elif getattr(block, "type", None) == "tool_use":
                tool_use_ids.append(block.id)

        result.append(msg)
        i += 1

        if not tool_use_ids:
            continue

        next_msg = messages[i] if i < len(messages) else None

        if next_msg is not None and next_msg.get("role") == "user":
            next_content = next_msg.get("content", [])
            if isinstance(next_content, list):
                covered = {
                    b.get("tool_use_id")
                    for b in next_content
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                }
            else:
                covered = set()
            orphans = [tid for tid in tool_use_ids if tid not in covered]
        else:
            orphans = list(tool_use_ids)

        if not orphans:
            continue

        logger.warning(
            "Repairing foreman history: inserting synthetic tool_result for "
            "orphaned tool_use ids %s",
            orphans,
        )
        synthetic = [
            {
                "type": "tool_result",
                "tool_use_id": tid,
                "content": '{"error": "tool result missing"}',
            }
            for tid in orphans
        ]
        if next_msg is not None and next_msg.get("role") == "user":
            existing = next_msg.get("content", [])
            existing_list = existing if isinstance(existing, list) else [{"type": "text", "text": str(existing)}]
            result.append({"role": "user", "content": synthetic + existing_list})
            i += 1  # skip the original next_msg; we just replaced it
        else:
            result.append({"role": "user", "content": synthetic})

    return result


async def run_foreman_ai(guild_id: str, human_message: str, extra_context: str = ""):
    """Process a human message (or escalation) through the Claude foreman AI."""
    if not HAS_ANTHROPIC:
        now = datetime.now(timezone.utc).isoformat()
        await broadcast(guild_id, {
            "type": "chat", "from": "foreman", "to": "user",
            "content": "Foreman AI offline (install `anthropic` package to enable).",
            "createdAt": now,
        })
        return

    # Build live context for the system prompt
    db = await get_db()
    try:
        result = await db.execute(
            text(
                "SELECT w.id, w.repos, w.state as worker_state,"
                " COUNT(a.id) as agent_count,"
                " GROUP_CONCAT(a.id || ':' || a.state) as agents"
                " FROM workers w"
                " LEFT JOIN agents a ON a.worker_id = w.id AND a.state != 'offline'"
                " WHERE w.guild_id = :guild_id"
                " GROUP BY w.id"
                " UNION ALL"
                " SELECT a.id, '[]', a.state, 1, a.id || ':' || a.state"
                " FROM agents a"
                " WHERE a.guild_id = :guild_id AND a.type = 'worker'"
                " AND a.worker_id IS NULL AND a.state != 'offline'"
            ),
            {"guild_id": guild_id},
        )
        worker_rows = [dict(r._mapping) for r in result.fetchall()]
        task_result = await db.execute(
            select(Task.id, Task.worker_id, Task.description, Task.state, Task.branch, Task.pr_url)
            .where(Task.guild_id == guild_id)
            .order_by(Task.created_at.desc())
            .limit(10)
        )
        task_rows = [
            {**dict(r._mapping), "description": dict(r._mapping).get("description") or ""}
            for r in task_result.fetchall()
        ]
    finally:
        await db.close()

    workers_block = json.dumps(
        [{"id": r["id"], "state": r["worker_state"] or "idle",
          "repos": json.loads(r["repos"] or "[]"),
          "agent_count": r["agent_count"] or 0} for r in worker_rows],
        indent=2,
    )
    tasks_block = json.dumps(task_rows[:6], indent=2)
    system = build_system_prompt(workers_block, tasks_block, extra_context)

    history = foreman_conversations.setdefault(guild_id, [])
    history.append({"role": "user", "content": human_message})

    client = _anthropic.AsyncAnthropic()

    _RESULT_MAX = 400  # chars kept per tool result in history

    try:
        text_parts = []
        for _ in range(6):  # safety cap on tool-call rounds
            history = _repair_tool_pairs(history)
            resp = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system,
                messages=history,
                tools=FOREMAN_TOOLS,
            )
            history.append({"role": "assistant", "content": resp.content})
            text_parts += [b.text for b in resp.content if b.type == "text" and b.text.strip()]

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break  # end_turn — foreman is done

            tool_results = await exec_tools(guild_id, tool_uses)
            # Truncate verbose results (e.g. GitHub JSON) before storing in history
            trimmed = [
                {**r, "content": r["content"][:_RESULT_MAX] + " …[truncated]"}
                if len(r.get("content", "")) > _RESULT_MAX else r
                for r in tool_results
            ]
            history.append({"role": "user", "content": trimmed})

        # Trim history, repair orphaned tool pairs, then drop any leading assistant
        # turns (which the API forbids as the first message).
        trimmed_history = history[-10:]
        repaired = _repair_tool_pairs(trimmed_history)
        while repaired and repaired[0].get("role") != "user":
            repaired = repaired[1:]
        foreman_conversations[guild_id] = repaired

        response_text = "\n".join(text_parts).strip()
        if response_text:
            now = datetime.now(timezone.utc).isoformat()
            await broadcast(guild_id, {
                "type": "chat", "from": "foreman", "to": "user",
                "content": response_text, "createdAt": now,
            })
            db = await get_db()
            try:
                db.add(Message(
                    guild_id=guild_id,
                    from_agent="foreman",
                    to_agent="user",
                    content=response_text,
                    message_type="chat",
                    created_at=now,
                ))
                await db.commit()
            finally:
                await db.close()

    except Exception as exc:
        now = datetime.now(timezone.utc).isoformat()
        await broadcast(guild_id, {
            "type": "chat", "from": "foreman", "to": "user",
            "content": f"Foreman error: {exc}", "createdAt": now,
        })
