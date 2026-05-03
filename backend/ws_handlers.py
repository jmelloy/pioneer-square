"""WebSocket message handlers for the per-guild ``/ws/{guild_id}`` endpoint.

Each handler is keyed by the inbound ``type`` field. Handlers receive a
``WSContext`` (the shared state held inside the live-connection coroutine —
DB session, socket, identity, the per-connection ``joined_agents`` set) and
the raw message dict.

Splitting the dispatch into named handlers keeps ``main.websocket_endpoint``
small enough to read at a glance and gives each branch a stable place to
land tests + future logic.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from events import (
    agent_owners,
    broadcast,
    connections,
    pending_claude_auth,
)
from fastapi import WebSocket
from foreman import maybe_post_plan_comment, reset_foreman_poll, run_foreman_ai
from models import Agent, Message, Task, TaskLog, User, Worker
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (pure DB lookups; live here because main.py + ws_handlers both use
# them and importing back into main.py from this module is the natural edge).
# ---------------------------------------------------------------------------


async def _resolve_user_identifier(db, identifier: str) -> str | None:
    """Look up a User by id (numeric github id as text) or github_login.

    Returns the canonical ``users.id`` or ``None`` if no match.
    """
    ident = (identifier or "").strip()
    if not ident:
        return None
    res = await db.execute(select(User.id).where((User.id == ident) | (User.github_login == ident)))
    return res.scalar_one_or_none()


async def _task_user_id(db, task_id: str | None) -> str | None:
    """Return the github_user_id who initiated *task_id*, or None if unknown.

    Used by worker-event handlers to route the resulting foreman conversation
    back to the originating user instead of always falling through to the guild
    owner. Returns None for legacy tasks (no user_id stamped) or unknown IDs;
    ``run_foreman_ai`` handles the None fallback itself.
    """
    if not task_id:
        return None
    res = await db.execute(select(Task.user_id).where(Task.id == task_id))
    return res.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class WSContext:
    """Per-connection state shared across handlers.

    Mutating ``joined_agents`` in the join handler is intentional — when the
    connection closes, the endpoint walks that set to mark agents offline.
    """

    websocket: WebSocket
    guild_id: str
    db: object  # AsyncSession; typed as object to avoid SQLAlchemy import dance
    joined_agents: set[str] = field(default_factory=set)
    ws_user_id: str | None = None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_ping(ctx: WSContext, data: dict) -> None:
    """Application-level heartbeat. Reply with pong so the worker can detect a
    half-open socket too."""
    await ctx.websocket.send_json({"type": "pong", "timestamp": datetime.now(UTC).isoformat()})


async def handle_join(ctx: WSContext, data: dict) -> None:
    agent_id = data.get("agentId")
    agent_name = data.get("agentName", "Unknown")
    agent_type = data.get("agentType", "worker")
    worker_id = data.get("workerId")
    joined_at = datetime.now(UTC).isoformat()
    stmt = sqlite_insert(Agent).values(
        id=agent_id,
        guild_id=ctx.guild_id,
        worker_id=worker_id,
        name=agent_name,
        type=agent_type,
        state="idle",
        joined_at=joined_at,
        last_seen=joined_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "guild_id": stmt.excluded.guild_id,
            "worker_id": stmt.excluded.worker_id,
            "name": stmt.excluded.name,
            "type": stmt.excluded.type,
            "state": stmt.excluded.state,
            "joined_at": stmt.excluded.joined_at,
            "last_seen": stmt.excluded.last_seen,
        },
    )
    await ctx.db.execute(stmt)
    if agent_type == "worker" and worker_id:
        await ctx.db.execute(
            update(Worker)
            .where(Worker.id == worker_id, Worker.guild_id == ctx.guild_id)
            .values(state="online", last_seen=joined_at)
        )
    await ctx.db.commit()
    if agent_id:
        ctx.joined_agents.add(agent_id)
        agent_owners[agent_id] = ctx.websocket
    await broadcast(
        ctx.guild_id,
        {
            "type": "agent-joined",
            "agentId": agent_id,
            "agentName": agent_name,
            "agentType": agent_type,
            "workerId": worker_id,
            "state": "idle",
            "joinedAt": joined_at,
        },
    )
    if agent_type == "worker":
        reset_foreman_poll(ctx.guild_id)


async def handle_agent_state(ctx: WSContext, data: dict) -> None:
    agent_id = data.get("agentId")
    state = data.get("state", "idle")
    activity = data.get("activity")
    update_vals: dict = {"state": state}
    if activity is not None:
        update_vals["activity"] = activity
    elif state in ("idle", "offline"):
        update_vals["activity"] = None
    await ctx.db.execute(
        update(Agent)
        .where(Agent.id == agent_id, Agent.guild_id == ctx.guild_id)
        .values(**update_vals)
    )
    await ctx.db.commit()
    msg: dict = {"type": "agent-state", "agentId": agent_id, "state": state}
    if "activity" in update_vals:
        msg["activity"] = update_vals["activity"]
    await broadcast(ctx.guild_id, msg)


async def handle_chat(ctx: WSContext, data: dict) -> None:
    """Persist the chat message + route to foreman.

    Each authenticated user gets their own foreman conversation thread (keyed
    by ``ws_user_id``). One special case: if a worker is waiting for a Claude
    auth code, the next user message is captured as that code and bypasses
    the foreman AI entirely.
    """
    from_agent = data.get("from", "user")
    to_agent = data.get("to", "foreman")
    content = data.get("content", "")
    created_at = datetime.now(UTC).isoformat()
    ctx.db.add(
        Message(
            guild_id=ctx.guild_id,
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            message_type="chat",
            created_at=created_at,
            user_id=ctx.ws_user_id if from_agent == "user" else None,
        )
    )
    await ctx.db.commit()
    await broadcast(
        ctx.guild_id,
        {
            "type": "chat",
            "from": from_agent,
            "to": to_agent,
            "content": content,
            "createdAt": created_at,
            **({"userId": ctx.ws_user_id} if ctx.ws_user_id and from_agent == "user" else {}),
        },
    )
    if not (from_agent == "user" and to_agent == "foreman" and content):
        return

    pending_workers = pending_claude_auth.get(ctx.guild_id, {})
    if pending_workers:
        pending_worker_id = next(iter(pending_workers))
        pending_workers.pop(pending_worker_id)
        logger.info(
            "chat intercepted as auth code for %s in guild %s code_len=%d",
            pending_worker_id,
            ctx.guild_id,
            len(content),
        )
        await broadcast(
            ctx.guild_id,
            {
                "type": "worker-auth-response",
                "workerId": pending_worker_id,
                "code": content,
            },
        )
    else:
        asyncio.create_task(run_foreman_ai(ctx.guild_id, content, user_id=ctx.ws_user_id))
        reset_foreman_poll(ctx.guild_id)


async def handle_terminal_output(ctx: WSContext, data: dict) -> None:
    msg_agent_id = data.get("agentId")
    line = data.get("line", "")
    task_id = data.get("taskId")
    detail = data.get("detail")
    created_at = datetime.now(UTC).isoformat()
    worker_id_for_log = None
    if msg_agent_id:
        result = await ctx.db.execute(select(Agent.worker_id).where(Agent.id == msg_agent_id))
        worker_id_for_log = result.scalar_one_or_none()
    if line:
        ctx.db.add(
            TaskLog(
                task_id=task_id or None,
                timestamp=created_at,
                line=line,
                worker_id=worker_id_for_log,
                agent_id=msg_agent_id,
                data=json.dumps(detail) if detail else None,
            )
        )
        await ctx.db.commit()
    await broadcast(
        ctx.guild_id,
        {
            "type": "terminal-output",
            "agentId": msg_agent_id,
            "workerId": worker_id_for_log,
            "taskId": task_id,
            "line": line,
            "timestamp": created_at,
            **({"detail": detail} if detail else {}),
        },
    )


async def handle_worker_register(ctx: WSContext, data: dict) -> None:
    worker_id = data.get("workerId")
    if not worker_id:
        return
    repos = data.get("repos") or []
    user_ident = data.get("user")
    update_vals: dict = {"repos": json.dumps(repos)}
    if user_ident:
        resolved = await _resolve_user_identifier(ctx.db, user_ident)
        if resolved:
            update_vals["user_id"] = resolved
    await ctx.db.execute(
        update(Worker)
        .where(Worker.id == worker_id, Worker.guild_id == ctx.guild_id)
        .values(**update_vals)
    )
    await ctx.db.commit()


async def handle_worker_disconnect(ctx: WSContext, data: dict) -> None:
    worker_id = data.get("workerId")
    for agent_id in ctx.joined_agents:
        await ctx.db.execute(
            update(Agent)
            .where(Agent.id == agent_id, Agent.guild_id == ctx.guild_id)
            .values(state="offline")
        )
    if worker_id:
        await ctx.db.execute(
            update(Worker)
            .where(Worker.id == worker_id, Worker.guild_id == ctx.guild_id)
            .values(state="offline")
        )
    if ctx.joined_agents or worker_id:
        await ctx.db.commit()
    for agent_id in ctx.joined_agents:
        await broadcast(
            ctx.guild_id,
            {"type": "agent-state", "agentId": agent_id, "state": "offline"},
        )
    reset_foreman_poll(ctx.guild_id)


async def handle_task_update(ctx: WSContext, data: dict) -> None:
    task_id = data.get("taskId")
    if not task_id:
        return
    update_values: dict = {}
    for src, col in (
        ("state", "state"),
        ("branch", "branch"),
        ("worktreePath", "worktree_path"),
        ("prUrl", "pr_url"),
        ("finishedAt", "finished_at"),
    ):
        if src in data:
            update_values[col] = data[src]
    if update_values:
        await ctx.db.execute(update(Task).where(Task.id == task_id).values(**update_values))
        await ctx.db.commit()
    await broadcast(ctx.guild_id, data, exclude=ctx.websocket)


async def handle_task_complete(ctx: WSContext, data: dict) -> None:
    task_id = data.get("taskId")
    worker_id_msg = data.get("workerId", "")
    desc = data.get("description", "")
    branch = data.get("branch", "")
    finalized_by = data.get("finalizedBy", "")
    last_text = data.get("lastText", "")
    if task_id:
        await ctx.db.execute(
            update(Task)
            .where(Task.id == task_id, Task.state == "working")
            .values(state="awaiting-review")
        )
        await ctx.db.commit()
    await broadcast(ctx.guild_id, data, exclude=ctx.websocket)
    if task_id and not finalized_by:
        asyncio.create_task(maybe_post_plan_comment(ctx.guild_id, task_id, last_text))
    if not task_id:
        return

    task_uid = await _task_user_id(ctx.db, task_id)
    if finalized_by == "timeout":
        finished_at = datetime.now(UTC).isoformat()
        await ctx.db.execute(
            update(Task).where(Task.id == task_id).values(state="done", finished_at=finished_at)
        )
        await ctx.db.commit()
        await broadcast(
            ctx.guild_id,
            {
                "type": "task-update",
                "taskId": task_id,
                "state": "done",
                "finishedAt": finished_at,
            },
        )
        asyncio.create_task(
            run_foreman_ai(
                ctx.guild_id,
                f"[timeout] Follow-up window expired for task {task_id} "
                f"(worker {worker_id_msg}). The task has been auto-finalized "
                "as done — no foreman action required.",
                user_id=task_uid,
            )
        )
    else:
        asyncio.create_task(
            run_foreman_ai(
                ctx.guild_id,
                f"[task-complete] Worker {worker_id_msg} finished task {task_id}: "
                f'"{desc[:80]}" — branch: {branch}. '
                "Review this result. Call send_followup if additional work is needed "
                "(e.g. update tests, add docs, fix lint errors). "
                "Otherwise call finalize_task to mark it complete.",
                user_id=task_uid,
            )
        )
    reset_foreman_poll(ctx.guild_id)


async def handle_task_followup_done(ctx: WSContext, data: dict) -> None:
    task_id = data.get("taskId")
    worker_id_msg = data.get("workerId", "")
    if task_id:
        await ctx.db.execute(update(Task).where(Task.id == task_id).values(state="awaiting-review"))
        await ctx.db.commit()
    await broadcast(ctx.guild_id, data, exclude=ctx.websocket)
    if not task_id:
        return
    task_uid = await _task_user_id(ctx.db, task_id)
    asyncio.create_task(
        run_foreman_ai(
            ctx.guild_id,
            f"[followup-done] Worker {worker_id_msg} completed a follow-up for task {task_id}. "
            "Decide: call send_followup for more work, or call finalize_task to mark it done.",
            user_id=task_uid,
        )
    )
    reset_foreman_poll(ctx.guild_id)


async def handle_needs_input(ctx: WSContext, data: dict) -> None:
    await broadcast(ctx.guild_id, data, exclude=ctx.websocket)
    wid = data.get("workerId", "a worker")
    task_id = data.get("taskId", "")
    description = data.get("description", "")
    stop_reason = data.get("stopReason", "")
    last_msg = data.get("lastMessage", "")
    escalation = (
        f"Worker {wid} could not complete task {task_id} and needs your help.\n"
        f"Task: {description}\n"
        f"Stop reason: {stop_reason}" + (f"\nLast message: {last_msg}" if last_msg else "")
    )
    task_uid = await _task_user_id(ctx.db, task_id) if task_id else None
    asyncio.create_task(run_foreman_ai(ctx.guild_id, escalation, user_id=task_uid))


async def handle_claude_auth_required(ctx: WSContext, data: dict) -> None:
    worker_id = data.get("workerId", "a worker")
    auth_url = data.get("url", "")
    logger.info(
        "claude-auth-required from %s in guild %s url=%s",
        worker_id,
        ctx.guild_id,
        auth_url[:80],
    )
    await broadcast(ctx.guild_id, data, exclude=ctx.websocket)
    pending_claude_auth.setdefault(ctx.guild_id, {})[worker_id] = auth_url
    logger.info(
        "pending_claude_auth now has %d entries for guild %s",
        len(pending_claude_auth.get(ctx.guild_id, {})),
        ctx.guild_id,
    )
    asyncio.create_task(
        run_foreman_ai(
            ctx.guild_id,
            f"Worker {worker_id} needs Claude authentication. "
            f"Auth URL: {auth_url}. "
            "A human must visit this URL, complete authentication, then paste the "
            "resulting code into the auth panel that has appeared in the chat UI "
            "(or type it into the Foreman Comms input). The worker is waiting.",
        )
    )


async def handle_worker_auth_response(ctx: WSContext, data: dict) -> None:
    worker_id = data.get("workerId", "")
    code_len = len(data.get("code", ""))
    logger.info(
        "worker-auth-response for %s in guild %s code_len=%d",
        worker_id,
        ctx.guild_id,
        code_len,
    )
    pending_claude_auth.get(ctx.guild_id, {}).pop(worker_id, None)
    peer_count = len(connections.get(ctx.guild_id, []))
    logger.info(
        "broadcasting worker-auth-response to %d connections in guild %s",
        peer_count,
        ctx.guild_id,
    )
    await broadcast(ctx.guild_id, data)


async def handle_webrtc_signal(ctx: WSContext, data: dict) -> None:
    """offer/answer/ice-candidate — forward to all peers in the guild."""
    await broadcast(ctx.guild_id, data, exclude=ctx.websocket)


HANDLERS: dict[str, callable] = {
    "ping": handle_ping,
    "join": handle_join,
    "agent-state": handle_agent_state,
    "chat": handle_chat,
    "terminal-output": handle_terminal_output,
    "worker-register": handle_worker_register,
    "worker-disconnect": handle_worker_disconnect,
    "task-update": handle_task_update,
    "task-complete": handle_task_complete,
    "task-followup-done": handle_task_followup_done,
    "needs-input": handle_needs_input,
    "claude-auth-required": handle_claude_auth_required,
    "worker-auth-response": handle_worker_auth_response,
    "offer": handle_webrtc_signal,
    "answer": handle_webrtc_signal,
    "ice-candidate": handle_webrtc_signal,
}


async def dispatch(ctx: WSContext, data: dict) -> None:
    """Route an inbound WS message to its handler.

    Unknown types fall through to a generic broadcast (legacy behaviour).
    """
    msg_type = data.get("type")
    handler = HANDLERS.get(msg_type)
    if handler is None:
        await broadcast(ctx.guild_id, data)
        return
    await handler(ctx, data)
