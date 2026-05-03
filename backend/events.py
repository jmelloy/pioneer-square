"""WebSocket connection state and broadcast utilities.

Extracted here so both main.py and the foreman package can import
broadcast/emit_terminal_line without circular dependencies.
"""

import asyncio
import logging
from datetime import UTC, datetime

from database import get_db
from fastapi import WebSocket
from models import Agent, TaskLog
from sqlalchemy import select

logger = logging.getLogger(__name__)

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
    dead = []
    for ws in connections[guild_id]:
        if ws is exclude:
            continue
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connections[guild_id].remove(ws)


async def emit_terminal_line(guild_id: str, agent_id: str, line: str):
    """Broadcast and persist a terminal output line."""
    now = datetime.now(UTC).isoformat()
    await broadcast(
        guild_id,
        {
            "type": "terminal-output",
            "agentId": agent_id,
            "line": line,
            "timestamp": now,
        },
    )
    if line:
        db = await get_db()
        try:
            result = await db.execute(select(Agent.worker_id).where(Agent.id == agent_id))
            worker_id_for_log = result.scalar_one_or_none()
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
