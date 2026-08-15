"""WebSocket connection state and broadcast utilities.

Extracted here so both the backend app and the foreman package can import
broadcast/emit_terminal_line without circular dependencies.
"""

import asyncio
import json
import logging
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from database import get_db
from fastapi import WebSocket, WebSocketDisconnect
from models import Agent, TaskLog
from sqlmodel import col, select

if TYPE_CHECKING:
    from ws_types import _WS

logger = logging.getLogger(__name__)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def ws_dumps(payload: Any) -> str:
    """Serialize payload to a JSON string with datetime/UUID support."""
    return json.dumps(payload, default=_json_default)


# In-memory WebSocket connections: guild_id -> list of WebSocket
connections: dict[str, list[WebSocket]] = {}

# Tracks which WebSocket currently owns a given agent_id.
agent_owners: dict[str, WebSocket] = {}

# Per-guild lock that serialises agent-ownership transitions (the join handler
# installing a new owner vs. the disconnect cleanup nulling out a stale one).
# Without this, a fast reconnect can race the previous socket's cleanup and
# get its just-joined agent stamped offline.
_agent_owner_locks: dict[str, asyncio.Lock] = {}

# Active external Foreman API proxy WebSocket per guild — at most one per guild
# at a time. The backend still runs the Foreman loop; the external process only
# executes LLM API requests that the backend sends over this socket.
foreman_connections: dict[str, WebSocket] = {}

# Worker liveness probes currently awaiting a response. Keys are
# (guild_pk, worker_id); values are the UTC instant the backend sent worker-ping.
pending_worker_probes: dict[tuple[int, str], datetime] = {}


def agent_owner_lock(guild_id: str) -> asyncio.Lock:
    """Return the per-guild lock used to serialise ``agent_owners`` writes."""
    lock = _agent_owner_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        _agent_owner_locks[guild_id] = lock
    return lock


async def broadcast(guild_id: str, message: dict, exclude: WebSocket | None = None):
    """Broadcast a message to all connections in a guild."""
    if guild_id not in connections:
        return
    # Serialize once before iterating — let TypeError propagate immediately so
    # serialization bugs are visible rather than silently evicting connections.
    data = ws_dumps(message)
    dead = []
    for ws in connections[guild_id]:
        if ws is exclude:
            continue
        try:
            await ws.send_text(data)
        except (WebSocketDisconnect, RuntimeError, OSError) as exc:
            logger.warning("broadcast: send failed for %s guild=%s: %s", ws, guild_id, exc)
            dead.append(ws)
        except Exception as exc:
            logger.warning("Unexpected error evicting WebSocket: %s", exc, exc_info=True)
            dead.append(ws)
    for ws in dead:
        connections[guild_id].remove(ws)


async def send_ws_message(ws: WebSocket, message: "_WS") -> None:
    """Serialise a typed WS model and send it to a single WebSocket."""
    await ws.send_text(ws_dumps(message.model_dump(by_alias=True)))


async def broadcast_msg(guild_id: str, message: "_WS", exclude: WebSocket | None = None) -> None:
    """Serialise a typed WS model and broadcast it to all guild connections."""
    await broadcast(guild_id, message.model_dump(by_alias=True), exclude)


async def emit_terminal_line(
    guild_id: str,
    agent_id: str | None = None,
    line: str = "",
    *,
    worker_id: str | None = None,
    task_id: str | None = None,
    detail: Any = None,
    level: str | None = None,
    db: Any = None,
) -> None:
    """Broadcast and persist a terminal output line.

    Single source of truth for "a line of agent/worker output happened" — both
    the inbound ``terminal-output`` WS handler (``ws_handlers.handle_terminal_output``)
    and backend-internal callers that synthesize a line (foreman tool messages,
    worker-lifecycle events, operator actions) go through here so persistence
    (``task_logs``) and broadcast (``TerminalOutputMsg``) can never drift apart.

    Pass an existing ``db`` session (e.g. a WS handler's per-connection
    ``ctx.db``) to participate in that session; otherwise a short-lived one is
    opened and closed here.
    """
    from ws_types import TerminalOutputMsg

    now = datetime.now(UTC)
    resolved_worker_id = worker_id
    owns_db = db is None
    if owns_db:
        db = await get_db()
    try:
        if resolved_worker_id is None and agent_id:
            result = await db.exec(select(col(Agent.worker_id)).where(col(Agent.id) == agent_id))
            resolved_worker_id = result.one_or_none()
        await broadcast_msg(
            guild_id,
            TerminalOutputMsg(
                agentId=agent_id,
                workerId=resolved_worker_id,
                taskId=task_id,
                line=line,
                timestamp=now.isoformat(),
                detail=detail if detail else None,
                level=level if level else None,
            ),
        )
        if line:
            db.add(
                TaskLog(
                    task_id=task_id or None,
                    timestamp=now,
                    line=line,
                    worker_id=resolved_worker_id,
                    agent_id=agent_id,
                    data=json.dumps(detail) if detail else None,
                    level=level,
                )
            )
            await db.commit()
    finally:
        if owns_db:
            await db.close()
