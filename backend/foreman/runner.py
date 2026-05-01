"""Foreman AI runner: conversation management and main loop."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select

from database import get_db
from events import broadcast
from foreman.prompt import build_system_prompt
from foreman.tools import FOREMAN_TOOLS, exec_tools
from models import ForemanTurn, Guild, Message, Task

try:
    import anthropic as _anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

logger = logging.getLogger(__name__)

_RESULT_MAX = 400   # chars stored per tool result (keeps DB and context lean)
_HUMAN_TURN_WINDOW = 5  # how many non-tool-response user turns to include


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

    messages = [
        {"role": t.role, "content": json.loads(t.content_json)}
        for t in turns[cutoff:]
    ]

    # Anthropic API requires the first message to have role "user"
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

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
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(turn)
        await db.commit()
        await db.refresh(turn)
        return turn.id
    finally:
        await db.close()


async def run_foreman_ai(
    guild_id: str,
    human_message: str,
    extra_context: str = "",
    user_id: str | None = None,
):
    """Process a human message (or system escalation) through the Claude foreman AI."""
    if not HAS_ANTHROPIC:
        now = datetime.now(timezone.utc).isoformat()
        await broadcast(guild_id, {
            "type": "chat", "from": "foreman", "to": "user",
            "content": "Foreman AI offline (install `anthropic` package to enable).",
            "createdAt": now,
        })
        return

    if not user_id:
        user_id = await _get_guild_user_id(guild_id) or guild_id

    # Build live context for the system prompt
    from sqlalchemy import text
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
        guild_result = await db.execute(
            select(Guild.primary_repo).where(Guild.id == guild_id)
        )
        primary_repo: str | None = guild_result.scalar_one_or_none()
    finally:
        await db.close()

    workers_block = json.dumps(
        [{"id": r["id"], "state": r["worker_state"] or "idle",
          "repos": json.loads(r["repos"] or "[]"),
          "agent_count": r["agent_count"] or 0} for r in worker_rows],
        indent=2,
    )
    tasks_block = json.dumps(task_rows[:6], indent=2)
    system = build_system_prompt(workers_block, tasks_block, extra_context, primary_repo=primary_repo)

    # Persist and load the new human turn
    await _save_turn(guild_id, user_id, "user", human_message)
    messages = await _load_history(guild_id, user_id)

    client = _anthropic.AsyncAnthropic()

    try:
        text_parts = []
        for _ in range(6):  # safety cap on tool-call rounds
            resp = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system,
                messages=messages,
                tools=FOREMAN_TOOLS,
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

            tool_results = await exec_tools(guild_id, tool_uses)
            # Truncate verbose results before storing
            trimmed = [
                {**r, "content": r["content"][:_RESULT_MAX] + " …[truncated]"}
                if len(r.get("content", "")) > _RESULT_MAX else r
                for r in tool_results
            ]
            # Persist tool_result turn as a child of the assistant turn
            await _save_turn(
                guild_id, user_id, "user", trimmed,
                is_tool_response=True, parent_id=asst_turn_id,
            )
            messages.append({"role": "user", "content": trimmed})

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


async def clear_foreman_history(guild_id: str, user_id: str) -> int:
    """Delete all stored turns for this guild+user. Returns count removed."""
    db = await get_db()
    try:
        result = await db.execute(
            delete(ForemanTurn)
            .where(ForemanTurn.guild_id == guild_id, ForemanTurn.user_id == user_id)
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
