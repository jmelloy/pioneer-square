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

# Workers currently waiting for a Claude auth code: guild_id -> {worker_id: url}
pending_claude_auth: dict[str, dict[str, str]] = {}

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


async def emit_terminal_line(guild_id: str, agent_id: str, line: str):
    """Broadcast and persist a terminal output line."""
    from ws_types import TerminalOutputMsg

    now = datetime.now(UTC)
    await broadcast_msg(
        guild_id,
        TerminalOutputMsg(agentId=agent_id, line=line, timestamp=now.isoformat()),
    )
    if line:
        db = await get_db()
        try:
            result = await db.exec(select(col(Agent.worker_id)).where(col(Agent.id) == agent_id))
            worker_id_for_log = result.one_or_none()
            db.add(
                TaskLog(
                    task_id=None,
                    timestamp=now,
                    line=line,
                    worker_id=worker_id_for_log,
                    agent_id=agent_id,
                )
            )
            await db.commit()
        finally:
            await db.close()
