"""The single per-guild WebSocket endpoint.

Each authenticated browser tab and each worker process opens one connection
to ``/ws/{guild_id}``.  Inbound frames are dispatched into ``ws_handlers``
keyed by the ``type`` field; outbound broadcasts go through ``events.broadcast``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import anyio
import ws_handlers
from database import get_db
from events import (
    agent_owner_lock,
    agent_owners,
    broadcast_msg,
    connections,
    foreman_connections,
    pending_worker_probes,
)
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from models import Agent, Guild, UserSession, Worker
from sqlalchemy import update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from ws_types import AgentStateMsg

router = APIRouter()
logger = logging.getLogger(__name__)


async def _touch_agent(
    db, guild_pk: int | None, agent_id: str | None, worker_id: str | None = None
) -> None:
    """Refresh ``last_seen`` for the sender named by an inbound WS frame.

    Agent-scoped frames refresh that agent and its parent worker. Worker-scoped
    frames refresh only the worker row; they do not imply that sibling agent
    slots emitted anything.
    """
    if not guild_pk:
        return
    if not agent_id and not worker_id:
        return
    now = datetime.now(UTC)
    if agent_id:
        if worker_id is None:
            res = await db.exec(
                select(col(Agent.worker_id)).where(
                    col(Agent.id) == agent_id, col(Agent.guild_id) == guild_pk
                )
            )
            worker_id = res.one_or_none()
        await db.exec(
            update(Agent)
            .where(col(Agent.id) == agent_id, col(Agent.guild_id) == guild_pk)
            .values(last_seen=now)
        )
    if worker_id:
        await db.exec(
            update(Worker)
            .where(col(Worker.id) == worker_id, col(Worker.guild_id) == guild_pk)
            .values(last_seen=now)
        )
        pending_worker_probes.pop((guild_pk, worker_id), None)
    await db.commit()


@router.websocket("/ws/{guild_id}")
async def websocket_endpoint(websocket: WebSocket, guild_id: str):
    await websocket.accept()
    if guild_id not in connections:
        connections[guild_id] = []
    connections[guild_id].append(websocket)

    # Shield the setup awaits so a WS close that arrives before setup completes
    # does not deliver CancelledError here — it is deferred to the first
    # checkpoint inside the message loop where it is properly caught.
    _guild_pk: int | None = None
    ws_user_id: str | None = None
    db: AsyncSession = None  # type: ignore[assignment]  -- initialized inside the shield below
    with anyio.CancelScope(shield=True):
        _gp_db = await get_db()
        try:
            _gp_res = await _gp_db.exec(select(col(Guild.id)).where(col(Guild.slug) == guild_id))
            _guild_pk = _gp_res.one_or_none()
        except Exception:
            logger.exception("WS guild id lookup failed for guild %s", guild_id)
        finally:
            try:
                await _gp_db.close()
            except Exception:
                logger.debug("WS _gp_db close error during guild id lookup", exc_info=True)

        # Identify the browser user from the optional ?token= query param.
        # Workers don't pass a token; ws_user_id stays None for them.
        _token = websocket.query_params.get("token")
        if _token:
            _auth_db = await get_db()
            try:
                _res = await _auth_db.exec(
                    select(col(UserSession.github_user_id)).where(col(UserSession.token) == _token)
                )
                ws_user_id = _res.one_or_none()
            except Exception:
                logger.exception("WS token lookup failed (token treated as anonymous)")
            finally:
                await _auth_db.close()

        # Open the main session inside the shield so all setup awaits are
        # protected; deferred cancellation fires at receive_json() instead.
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

            # Refresh last_seen for the inbound frame's sender. Worker-scoped
            # messages only touch Worker.last_seen; agent-scoped messages touch
            # that agent and the parent worker.
            await _touch_agent(db, ctx.guild_pk, data.get("agentId"), data.get("workerId"))

            await ws_handlers.dispatch(ctx, data)

            # Safety net: a handler that returns with an open (uncommitted)
            # transaction — e.g. an early `return` after a SELECT — would pin
            # this connection's pooled DB connection for the socket's entire
            # lifetime, eventually exhausting the pool. Committed work is
            # already flushed, so this rollback is a no-op for well-behaved
            # handlers and releases the connection for leaky ones.
            await db.rollback()

    except WebSocketDisconnect:
        if guild_id in connections and websocket in connections[guild_id]:
            connections[guild_id].remove(websocket)
    except asyncio.CancelledError:
        # Deferred cancellation from the shielded setup phase (WS closed before
        # setup completed).  Clean up the connection slot and let the finally
        # block handle the rest; do NOT re-raise so sibling connections are not
        # cascade-cancelled by the anyio task group.
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
        # raises Cancelled inside the asyncpg layer, which then propagates
        # as an unhandled exception from this task and causes the anyio task
        # group to cancel sibling connections.
        with anyio.CancelScope(shield=True):
            try:
                # If this socket was the active external foreman, evict it so
                # subsequent trigger events fall back to the embedded foreman.
                if foreman_connections.get(guild_id) is websocket:
                    foreman_connections.pop(guild_id, None)
                    logger.info("guild=%s external foreman WS closed (socket disconnect)", guild_id)

                # Only mark agents offline if this WS is still the current owner.
                # The per-guild lock pairs with handle_join's ownership write so a
                # reconnect's just-installed agent can't be stamped offline by the
                # previous socket's cleanup running concurrently.
                async with agent_owner_lock(guild_id):
                    stale_agents = [
                        aid for aid in joined_agents if agent_owners.get(aid) is websocket
                    ]
                    # Release ownership first so a racing join/reconnect sees a
                    # clean slate even if the DB operations below fail.
                    for agent_id in stale_agents:
                        agent_owners.pop(agent_id, None)
                    # Look up the worker_ids for all stale agents *before* we
                    # modify their rows, so we can update the Worker table with
                    # the correct IDs (worker IDs are "w-…"; agent IDs are
                    # "a-…" — using agent_id as a Worker.id filter never
                    # matched and left zombie workers stuck in "online").
                    stale_worker_ids: set[str] = set()
                    try:
                        if stale_agents:
                            wid_rows = (
                                await db.exec(
                                    select(col(Agent.worker_id)).where(
                                        col(Agent.id).in_(stale_agents),
                                        col(Agent.guild_id) == _guild_pk,
                                        col(Agent.worker_id).isnot(None),
                                    )
                                )
                            ).all()
                            stale_worker_ids = set(wid_rows)
                        for agent_id in stale_agents:
                            await db.exec(
                                update(Agent)
                                .where(col(Agent.id) == agent_id, col(Agent.guild_id) == _guild_pk)
                                .values(state="offline", activity=None, current_task_id=None)
                            )
                        # Mirror into workers table using the real worker IDs.
                        for wid in stale_worker_ids:
                            await db.exec(
                                update(Worker)
                                .where(col(Worker.id) == wid, col(Worker.guild_id) == _guild_pk)
                                .values(state="offline")
                            )
                        if stale_agents:
                            await db.commit()
                    except Exception:
                        # The DB connection may have been closed by a
                        # CancelledError delivered during a handler (e.g.
                        # when a close frame races a db.commit() inside the
                        # message loop).  Log and continue so the broadcast
                        # and foreman-trigger below still run.
                        logger.warning(
                            "WS teardown: DB error for guild %s — stale agents/workers"
                            " may remain online in DB until sweeper runs",
                            guild_id,
                            exc_info=True,
                        )
                for agent_id in stale_agents:
                    await broadcast_msg(
                        guild_id,
                        AgentStateMsg(agentId=agent_id, state="offline"),
                    )
                # Batch all stale-worker notifications into a single foreman
                # trigger so a simultaneous mass-disconnect doesn't stampede
                # the foreman with N concurrent embedded-foreman tasks.
                # Skip workers that already sent reason=shutdown via a graceful
                # worker-disconnect message to avoid double-firing.
                abrupt_worker_ids = stale_worker_ids - ctx.gracefully_disconnected_workers
                if abrupt_worker_ids:
                    offline_lines = "\n".join(
                        f"[worker-offline] worker_id={wid} reason=disconnect"
                        for wid in sorted(abrupt_worker_ids)
                    )
                    await ws_handlers._trigger_foreman(
                        guild_id,
                        "worker-offline",
                        offline_lines,
                        task_name="foreman.worker-offline:disconnect-batch",
                    )
            finally:
                # If a cancellation was delivered inside a handler (e.g.
                # handle_worker_disconnect) the underlying asyncpg connection
                # may already be closed, making the implicit rollback inside
                # db.close() raise ValueError("Connection closed").  Swallow
                # that so the teardown error doesn't propagate to the test
                # client or the caller.
                try:
                    await db.close()
                except Exception:
                    logger.debug("WS db session close error during teardown", exc_info=True)
