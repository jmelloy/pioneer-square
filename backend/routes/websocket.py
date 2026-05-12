"""The single per-guild WebSocket endpoint.

Each authenticated browser tab and each worker process opens one connection
to ``/ws/{guild_id}``.  Inbound frames are dispatched into ``ws_handlers``
keyed by the ``type`` field; outbound broadcasts go through ``events.broadcast``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import anyio
import ws_handlers
from database import get_db
from events import agent_owner_lock, agent_owners, broadcast, connections
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from models import Agent, Guild, UserSession, Worker
from sqlalchemy import select, update

router = APIRouter()
logger = logging.getLogger(__name__)


async def _touch_agent(
    db, guild_pk: int | None, agent_id: str | None, worker_id: str | None = None
) -> None:
    """Refresh ``last_seen`` for the agent (and its worker) so the sweeper
    knows the connection is still alive. Called for every inbound WS frame.

    If *worker_id* isn't passed we look it up from the agent row so messages
    that only carry an ``agentId`` (most of them) still keep the worker fresh.
    Once we know the worker, we also refresh every other agent owned by it —
    a single worker process owns all its slots over one socket, so any
    inbound frame proves the whole worker is alive.
    """
    if not guild_pk:
        return
    if not agent_id and not worker_id:
        return
    now = datetime.now(UTC).isoformat()
    if agent_id and worker_id is None:
        res = await db.execute(select(Agent.worker_id).where(Agent.id == agent_id))
        worker_id = res.scalar_one_or_none()
    if worker_id:
        await db.execute(
            update(Worker)
            .where(Worker.id == worker_id, Worker.guild_pk == guild_pk)
            .values(last_seen=now)
        )
        await db.execute(
            update(Agent)
            .where(Agent.worker_id == worker_id, Agent.guild_pk == guild_pk)
            .values(last_seen=now)
        )
    elif agent_id:
        await db.execute(
            update(Agent)
            .where(Agent.id == agent_id, Agent.guild_pk == guild_pk)
            .values(last_seen=now)
        )
    await db.commit()


@router.websocket("/ws/{guild_id}")
async def websocket_endpoint(websocket: WebSocket, guild_id: str):
    await websocket.accept()
    if guild_id not in connections:
        connections[guild_id] = []
    connections[guild_id].append(websocket)

    # Look up the guild's integer PK once for the lifetime of this connection.
    _guild_pk: int | None = None
    _gp_db = await get_db()
    try:
        _gp_res = await _gp_db.execute(select(Guild.id).where(Guild.guild_id == guild_id))
        _guild_pk = _gp_res.scalar_one_or_none()
    except Exception:
        logger.exception("WS guild_pk lookup failed for guild %s", guild_id)
    finally:
        await _gp_db.close()

    # Identify the browser user from the optional ?token= query param.
    # Workers don't pass a token; ws_user_id stays None for them.
    ws_user_id: str | None = None
    _token = websocket.query_params.get("token")
    if _token:
        _auth_db = await get_db()
        try:
            _res = await _auth_db.execute(
                select(UserSession.github_user_id).where(UserSession.token == _token)
            )
            ws_user_id = _res.scalar_one_or_none()
        except Exception:
            logger.exception("WS token lookup failed (token treated as anonymous)")
        finally:
            await _auth_db.close()

    db = await get_db()
    ctx = ws_handlers.WSContext(
        websocket=websocket,
        guild_id=guild_id,
        guild_pk=_guild_pk,
        db=db,
        ws_user_id=ws_user_id,
    )
    joined_agents = ctx.joined_agents  # alias for the disconnect cleanup below
    try:
        while True:
            data = await websocket.receive_json()

            # Refresh last_seen for any inbound frame so the sweeper knows
            # this worker is still alive. Cheap no-op for browser users
            # (they don't carry an agentId/workerId).
            await _touch_agent(db, ctx.guild_pk, data.get("agentId"), data.get("workerId"))

            await ws_handlers.dispatch(ctx, data)

    except WebSocketDisconnect:
        if guild_id in connections and websocket in connections[guild_id]:
            connections[guild_id].remove(websocket)
    except Exception:
        logger.exception("WS handler crashed for guild %s", guild_id)
        if guild_id in connections and websocket in connections[guild_id]:
            connections[guild_id].remove(websocket)
    finally:
        # Shield the cleanup from anyio cancel-scope cancellation so that
        # db operations and db.close() always run to completion.  Without
        # the shield, a cancelled cancel scope (e.g. TestClient teardown)
        # raises Cancelled inside the aiosqlite layer, which then propagates
        # as an unhandled exception from this task and causes the anyio task
        # group to cancel sibling connections.
        with anyio.CancelScope(shield=True):
            try:
                # Only mark agents offline if this WS is still the current owner.
                # The per-guild lock pairs with handle_join's ownership write so a
                # reconnect's just-installed agent can't be stamped offline by the
                # previous socket's cleanup running concurrently.
                async with agent_owner_lock(guild_id):
                    stale_agents = [
                        aid for aid in joined_agents if agent_owners.get(aid) is websocket
                    ]
                    for agent_id in stale_agents:
                        agent_owners.pop(agent_id, None)
                        await db.execute(
                            update(Agent)
                            .where(Agent.id == agent_id, Agent.guild_pk == _guild_pk)
                            .values(state="offline")
                        )
                        # Mirror into workers table so foreman sees the worker as offline.
                        await db.execute(
                            update(Worker)
                            .where(Worker.id == agent_id, Worker.guild_pk == _guild_pk)
                            .values(state="offline")
                        )
                    if stale_agents:
                        await db.commit()
                for agent_id in stale_agents:
                    await broadcast(
                        guild_id,
                        {
                            "type": "agent-state",
                            "agentId": agent_id,
                            "state": "offline",
                        },
                    )
            finally:
                # If a cancellation was delivered inside a handler (e.g.
                # handle_worker_disconnect) the underlying aiosqlite connection
                # may already be closed, making the implicit rollback inside
                # db.close() raise ValueError("Connection closed").  Swallow
                # that so the teardown error doesn't propagate to the test
                # client or the caller.
                try:
                    await db.close()
                except Exception:
                    logger.debug("WS db session close error during teardown", exc_info=True)
