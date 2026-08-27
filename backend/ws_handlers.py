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

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import discord_notifier
from events import (
    agent_owner_lock,
    agent_owners,
    broadcast,
    broadcast_msg,
    foreman_connections,
    send_ws_message,
)
from fastapi import WebSocket
from foreman.classify import is_human_event
from foreman.github_url_parser import parse_github_urls
from foreman.proxy import fail_pending_for_websocket, resolve_foreman_api_response
from foreman.runner import ensure_poll_loop, reset_foreman_poll, run_foreman_ai
from foreman.thread_service import ensure_conversation_thread
from foreman.tools import maybe_post_plan_comment
from lock_service import LockService
from models import Agent, Message, Task, TaskEvent, TaskLog, User, Worker
from pydantic import ValidationError
from sqlalchemy import delete, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from util.tasks import spawn
from utils import worker_display_name
from worker_lifecycle import signal_stale_worker_on_join
from ws_types import (
    KNOWN_INBOUND_TYPES,
    AgentJoinedMsg,
    AgentStateMsg,
    AnswerMsg,
    ChatMsg,
    ForemanDisconnectMsg,
    ForemanEvictedMsg,
    ForemanRegisteredMsg,
    IceCandidateMsg,
    NeedsInputMsg,
    OfferMsg,
    PongMsg,
    TaskAssignedMsg,
    TaskCompleteMsg,
    TaskFollowupDoneMsg,
    TaskRejectedMsg,
    TaskUpdateMsg,
    TerminalOutputMsg,
    parse_inbound_message,
)

logger = logging.getLogger(__name__)

_TERMINAL_STATES = ("done", "failed", "cancelled", "error")

# Terminal-output "levels" (see worker.py LEVEL_* constants) whose lines are
# actual agent/Claude output worth mirroring into a task's Discord stream
# thread. ``None`` is the default for task-scoped emits (LEVEL_INFO); worker /
# auth / claude-framing lines are deliberately excluded as low-value noise.
_DISCORD_STREAM_LEVELS = frozenset({None, "info", "thinking"})


# ---------------------------------------------------------------------------
# Helpers (pure DB lookups; live here because main.py + ws_handlers both use
# them and importing back into main.py from this module is the natural edge).
# ---------------------------------------------------------------------------


def _parse_pr_url(pr_url: str | None) -> tuple[int | None, str | None]:
    """Extract ``(pr_number, "owner/repo")`` from a GitHub PR URL.

    Delegates to the canonical parser (``foreman.github_url_parser``) for the
    standard web URL form (``https://github.com/o/r/pull/42``). Also accepts
    API URLs (``https://api.github.com/repos/o/r/pulls/42``), which the
    canonical parser doesn't cover. Returns ``(None, None)`` on anything that
    doesn't look like a PR URL — the caller stamps both columns to NULL in
    that case so we don't link a stale (number, repo) to a task whose PR was
    retracted.
    """
    if not pr_url or not isinstance(pr_url, str):
        return None, None

    refs = [ref for ref in parse_github_urls(pr_url) if ref.ref_type == "pull"]
    if refs:
        return refs[0].number, refs[0].slug

    parts = pr_url.rstrip("/").split("/")
    try:
        # ``…/repos/{owner}/{repo}/pulls/{n}`` (API form; not handled above)
        idx = parts.index("pulls")
        owner = parts[idx - 2]
        repo = parts[idx - 1]
        number = int(parts[idx + 1])
    except (ValueError, IndexError):
        return None, None
    if not owner or not repo:
        return None, None
    return number, f"{owner}/{repo}"


def _format_queued_followup(index: int, payload: dict) -> str:
    """Render one queued pending-followup payload for a foreman trigger message.

    Includes tool/model/provider overrides (when present) so the foreman can
    reissue an equivalent send_followup call instead of losing the override.
    """
    overrides = ", ".join(
        f"{key}={payload[key]}" for key in ("tool", "model", "provider") if payload.get(key)
    )
    suffix = f" [{overrides}]" if overrides else ""
    return f"  {index + 1}. {payload.get('instructions', '')}{suffix}"


def _load_event_payload(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _format_last_output(text: str, max_chars: int = 4000) -> str:
    """Truncate worker output before it's embedded in a Foreman trigger message.

    Foreman messages are delivered over the WebSocket and fed into the LLM's
    context, so we still cap length — just far more generously than the old
    200-char slice, which was cutting off Discord message content.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "... [truncated]"


async def _resolve_user_identifier(db, identifier: str) -> str | None:
    """Look up a User by id (numeric github id as text) or github_login.

    Returns the canonical ``users.id`` or ``None`` if no match.
    """
    ident = (identifier or "").strip()
    if not ident:
        return None
    res = await db.exec(
        select(col(User.id)).where((col(User.id) == ident) | (col(User.github_login) == ident))
    )
    return res.one_or_none()


async def _task_user_id(db, task_id: str | None) -> str | None:
    """Return the github_user_id who initiated *task_id*, or None if unknown.

    Used by worker-event handlers to route the resulting foreman conversation
    back to the originating user instead of always falling through to the guild
    owner. Returns None for legacy tasks (no user_id stamped) or unknown IDs;
    ``run_foreman_ai`` handles the None fallback itself.
    """
    if not task_id:
        return None
    res = await db.exec(select(col(Task.user_id)).where(col(Task.id) == task_id))
    return res.one_or_none()


# ---------------------------------------------------------------------------
# Foreman trigger dispatch helper
# ---------------------------------------------------------------------------


async def _trigger_foreman(
    guild_id: str,
    event: str,
    human_message: str,
    *,
    user_id: str | None = None,
    task_id: str | None = None,
    task_name: str = "foreman.unknown",
    reply_channel_id: str | None = None,
) -> None:
    """Dispatch a trigger into the embedded Foreman runner.

    The standalone process no longer receives trigger events. It is only an API
    proxy used by ``backend.foreman.runner`` at the LLM-call boundary.

    ``event`` mirrors the trigger-type vocabulary:
    ``chat``, ``task-complete``, ``followup-done``, ``needs-input``,
    ``claude-auth``, ``periodic-check``, ``worker-online``,
    ``worker-offline``.

    ``reply_channel_id`` pins the Discord destination for this run's narration
    to a specific channel — set by ``discord/router.py`` so a reply to an
    @-mention lands back where it was asked. None (every other caller) keeps
    the default routing in ``discord_notifier.notify_foreman_chat``.
    """
    # See foreman.classify for the human/automated event classification shared
    # with routes.tasks.create_task_followup's REST follow-up path.
    is_human = is_human_event(event)

    # Foreman-owned thread lifecycle (#1167): a brand-new human message with
    # no task_id yet is the start (or continuation) of a conversation — the
    # Foreman creates/reuses that conversation's Thread here, as a side
    # effect of handling the message, never something Discord or the
    # frontend originates. Worker-driven events (task-complete, etc.) already
    # carry an existing task_id whose Thread was stamped at task-creation
    # time (see foreman.tools' create_task/assign_task), so nothing to do here.
    if is_human and task_id is None and user_id:
        await ensure_conversation_thread(guild_id, user_id, human_message)

    spawn(
        run_foreman_ai(
            guild_id,
            human_message,
            user_id=user_id,
            task_id=task_id,
            is_human=is_human,
            reply_channel_id=reply_channel_id,
            trigger=event,
        ),
        name=task_name,
    )


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
    db: AsyncSession
    joined_agents: set[str] = field(default_factory=set)
    gracefully_disconnected_workers: set[str] = field(default_factory=set)
    registered_worker_ids: set[str] = field(default_factory=set)
    ws_user_id: str | None = None
    guild_pk: int | None = None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_ping(ctx: WSContext, data: dict) -> None:
    """Generic application-level ping used by tests and legacy clients."""
    await send_ws_message(ctx.websocket, PongMsg(timestamp=datetime.now(UTC).isoformat()))


async def handle_worker_pong(ctx: WSContext, data: dict) -> None:
    """Worker liveness probe reply.

    ``routes.websocket._touch_agent`` already refreshed Worker.last_seen before
    dispatch. The explicit handler keeps this protocol message from falling
    through to legacy broadcast behaviour.
    """


async def handle_join(ctx: WSContext, data: dict) -> None:
    agent_id = data.get("agentId")
    agent_name = data.get("agentName", "Unknown")
    agent_type = data.get("agentType", "worker")
    worker_id = data.get("workerId")
    joined_at = datetime.now(UTC)
    is_external_foreman = agent_type == "foreman" and bool(data.get("external"))
    # Validate that the worker actually belongs to this guild before proceeding.
    # A misconfigured or misbehaving worker could connect to the wrong guild's
    # WebSocket; without this check it would create agent rows and broadcast
    # events into a guild it doesn't belong to.
    worker_spawned_version: str | None = None
    worker_started_at = None
    if agent_type == "worker" and worker_id:
        member_res = await ctx.db.exec(
            select(col(Worker.id), col(Worker.spawned_version), col(Worker.started_at)).where(
                col(Worker.id) == worker_id,
                col(Worker.guild_id) == ctx.guild_pk,
            )
        )
        member_row = member_res.one_or_none()
        if member_row is None:
            logger.warning(
                "join: worker %s is not a member of guild %s — ignoring",
                worker_id,
                ctx.guild_id,
            )
            return
        worker_spawned_version = member_row[1]
        worker_started_at = member_row[2]
        # Guard against two live connections claiming the same worker_id (e.g. a
        # container that reconnects with a fresh agent_id while its previous
        # socket is still lingering after a restart). Evict the older agent so
        # exactly one registration is authoritative instead of both racing for
        # task dispatch.
        #
        # A worker process runs a fixed pool of concurrent agent slots that all
        # share one worker_id and join together over this same socket (see
        # models.Agent.current_task_id docstring). Those siblings must not be
        # evicted just because they were registered moments earlier — only an
        # agent owned by a *different* socket is genuinely stale.
        dup_res = await ctx.db.exec(
            select(col(Agent.id)).where(
                col(Agent.worker_id) == worker_id,
                col(Agent.guild_id) == ctx.guild_pk,
                col(Agent.id) != agent_id,
                col(Agent.state) != "offline",
            )
        )
        candidate_dup_ids = dup_res.all()
        async with agent_owner_lock(ctx.guild_id):
            duplicate_agent_ids = [
                dup_id
                for dup_id in candidate_dup_ids
                if agent_owners.get(dup_id) is not ctx.websocket
            ]
        if duplicate_agent_ids:
            logger.warning(
                "join: duplicate registration for worker_id=%s — evicting stale agent(s) "
                "%s in favor of new agent %s",
                worker_id,
                duplicate_agent_ids,
                agent_id,
            )
            await ctx.db.exec(
                update(Agent)
                .where(
                    col(Agent.id).in_(duplicate_agent_ids),
                    col(Agent.guild_id) == ctx.guild_pk,
                )
                .values(state="offline", activity=None, current_task_id=None)
            )
            await ctx.db.commit()
            async with agent_owner_lock(ctx.guild_id):
                stale_sockets = [
                    (dup_id, agent_owners.pop(dup_id, None)) for dup_id in duplicate_agent_ids
                ]
            for dup_id, stale_ws in stale_sockets:
                await broadcast_msg(ctx.guild_id, AgentStateMsg(agentId=dup_id, state="offline"))
                if stale_ws is not None and stale_ws is not ctx.websocket:
                    try:
                        await stale_ws.close(code=1000, reason="superseded by new registration")
                    except Exception:
                        logger.debug("join: failed to close superseded socket for agent %s", dup_id)
    if not is_external_foreman:
        stmt = pg_insert(Agent).values(
            id=agent_id,
            guild_id=ctx.guild_pk,
            worker_id=worker_id,
            name=agent_name,
            type=agent_type,
            state="idle",
            current_task_id=None,
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
                "current_task_id": stmt.excluded.current_task_id,
                "joined_at": stmt.excluded.joined_at,
                "last_seen": stmt.excluded.last_seen,
            },
        )
        await ctx.db.exec(stmt)
        if agent_type == "worker" and worker_id:
            await ctx.db.exec(
                update(Worker)
                .where(col(Worker.id) == worker_id, col(Worker.guild_id) == ctx.guild_pk)
                .values(state="online", last_seen=joined_at)
            )
        await ctx.db.commit()
    if agent_id:
        ctx.joined_agents.add(agent_id)
        if agent_type == "worker" and worker_id:
            ctx.registered_worker_ids.add(worker_id)
        # Take the per-guild ownership lock so a concurrent disconnect-cleanup
        # for the previous socket can't read a stale ``agent_owners`` entry,
        # decide it owns the agent, and stamp the just-joined agent offline.
        async with agent_owner_lock(ctx.guild_id):
            agent_owners[agent_id] = ctx.websocket
    # A backend-spawned worker holding code from a previous backend version is
    # told to shut down as it (re)connects: it finishes any in-progress task and
    # exits, the idle reaper force-kills it if it never complies, and the
    # foreman respawns on demand from the guild's spawn defaults.
    if agent_type == "worker" and worker_id:
        await signal_stale_worker_on_join(
            ctx.guild_id, worker_id, worker_spawned_version, worker_started_at
        )
    # Only an explicit external Foreman API proxy may claim the proxy seat —
    # the legacy browser join still carries agentType="foreman" but must NOT
    # be registered in foreman_connections. The external client signals its
    # intent with `external: true`.
    if agent_type == "foreman" and bool(data.get("external")):
        # Register as the active external API proxy for this guild.
        # Evict any previously connected proxy so there is never more than one
        # active provider-call endpoint at a time.
        existing_ws = foreman_connections.get(ctx.guild_id)
        if existing_ws is not None and existing_ws is not ctx.websocket:
            logger.info(
                "guild=%s evicting previous external foreman API proxy (new proxy connected)",
                ctx.guild_id,
            )
            try:
                await send_ws_message(
                    existing_ws,
                    ForemanEvictedMsg(
                        guildId=ctx.guild_id,
                        reason="superseded by new foreman connection",
                    ),
                )
            except Exception:
                pass
        foreman_connections[ctx.guild_id] = ctx.websocket
        logger.info(
            "guild=%s external foreman API proxy registered: agentId=%s", ctx.guild_id, agent_id
        )
        # Acknowledge registration so the proxy knows it is the active one.
        await send_ws_message(
            ctx.websocket,
            ForemanRegisteredMsg(guildId=ctx.guild_id, agentId=agent_id),
        )
    elif agent_type == "worker":
        # Worker connect is automated churn — ensure the loop, don't reset backoff.
        ensure_poll_loop(ctx.guild_id)
        # Replay any pending tasks already assigned to this worker so they
        # aren't lost if the backend sent task-assigned while the socket was
        # down. The worker's HTTP _fetch_pending_tasks() covers the same gap,
        # but this server-push path is faster (no polling interval required).
        # Done before the agent-joined broadcast so all DB work finishes before
        # observers see the join event; this prevents anyio cancel-scope races
        # in tests where a peer WS closes on receiving the broadcast.
        if worker_id:
            result = await ctx.db.exec(
                select(Task).where(
                    col(Task.guild_id) == ctx.guild_pk,
                    col(Task.worker_id) == worker_id,
                    col(Task.state).in_(["pending", "working"]),
                )
            )
            pending_tasks = result.all()
            for pt in pending_tasks:
                logger.info(
                    "join: replaying task-assigned for pending task %s to worker %s",
                    pt.id,
                    worker_id,
                )
                await send_ws_message(
                    ctx.websocket,
                    TaskAssignedMsg(
                        workerId=worker_id,
                        taskId=pt.id,
                        name=pt.name or "",
                        description=pt.description or "",
                        tool=pt.tool or "pi",
                        model=pt.model,
                        modelTier=pt.model_tier,
                        provider=pt.provider,
                        phase=pt.phase,
                        parentTaskId=pt.parent_task_id,
                        issueNumber=pt.issue_number,
                        issueRepo=pt.issue_repo,
                        prNumber=pt.pr_number,
                        prRepo=pt.pr_repo,
                    ),
                )
    joined_msg = AgentJoinedMsg(
        agentId=agent_id,
        agentName=agent_name,
        agentType=agent_type,
        workerId=worker_id,
        joinedAt=joined_at.isoformat(),
    )
    if worker_id:
        r = await ctx.db.exec(
            select(col(Worker.name)).where(
                col(Worker.id) == worker_id, col(Worker.guild_id) == ctx.guild_pk
            )
        )
        stored_name = r.one_or_none()
        joined_msg.workerName = stored_name or worker_display_name(worker_id)
    await broadcast_msg(ctx.guild_id, joined_msg)


# States that indicate the agent is no longer actively running a task and
# should trigger a lock release.  "done" and "cancelled" are intentionally
# excluded: those transitions go through handle_task_complete /
# handle_task_followup_done / cancel_task_endpoint which already release the
# lock.  Including them here risks a race where this handler fires *after* the
# task has already been moved to a terminal state (done, cancelled, failed) and
# overwrites it with "awaiting-review".  The Task.state == "working" guard
# below is the primary safety net; the restricted set is defence-in-depth.
_LOCK_RELEASE_AGENT_STATES = frozenset({"idle", "offline", "error", "timeout"})


async def handle_agent_state(ctx: WSContext, data: dict) -> None:
    agent_id = data.get("agentId")
    worker_id = data.get("workerId")
    state = data.get("state", "idle")
    activity = data.get("activity")
    update_vals: dict = {"state": state}
    if activity is not None:
        update_vals["activity"] = activity
    elif state in ("idle", "offline"):
        update_vals["activity"] = None
    # taskId is the slot's current_task_id. Idle/offline agents are not
    # executing anything, so we always clear the column for those states even
    # if the worker forgot to send taskId=null.
    if "taskId" in data:
        update_vals["current_task_id"] = data.get("taskId")
    elif state in ("idle", "offline"):
        update_vals["current_task_id"] = None

    # When the agent transitions to a non-working state, release the task lock
    # so the task doesn't stay stuck in "working" indefinitely.  We must read
    # current_task_id from the DB *before* the update clears it.
    task_id_to_release: str | None = None
    agent_worker_id_for_release: str | None = None
    if state in _LOCK_RELEASE_AGENT_STATES and agent_id:
        row = await ctx.db.exec(
            select(col(Agent.current_task_id), col(Agent.worker_id)).where(
                col(Agent.id) == agent_id, col(Agent.guild_id) == ctx.guild_pk
            )
        )
        agent_row = row.one_or_none()
        if agent_row is not None:
            task_id_to_release = agent_row[0]
            agent_worker_id_for_release = agent_row[1]

    await ctx.db.exec(
        update(Agent)
        .where(col(Agent.id) == agent_id, col(Agent.guild_id) == ctx.guild_pk)
        .values(**update_vals)
    )

    if task_id_to_release and agent_worker_id_for_release:
        # Guard: only transition the task when the agent's worker is the one
        # assigned to it.  Prevents a stale current_task_id from releasing a
        # lock that belongs to a different worker's active execution.
        res = await ctx.db.exec(
            update(Task)
            .where(
                col(Task.id) == task_id_to_release,
                col(Task.state) == "working",
                col(Task.worker_id) == agent_worker_id_for_release,
            )
            .values(state="awaiting-review")
        )
        if getattr(res, "rowcount", 0):
            await LockService(ctx.db).release(f"task:{task_id_to_release}")

    await ctx.db.commit()
    await broadcast_msg(
        ctx.guild_id,
        AgentStateMsg(
            agentId=agent_id,
            state=state,
            workerId=worker_id if worker_id else None,
            activity=update_vals.get("activity"),
            taskId=update_vals.get("current_task_id"),
        ),
    )
    # Agent state update is automated churn — ensure the loop, don't reset backoff.
    ensure_poll_loop(ctx.guild_id)


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
    created_at = datetime.now(UTC)

    # Resolve (and, for the human -> foreman turn that starts/continues a
    # conversation, create) the Thread this message belongs to *before*
    # persisting it, so the row is threaded from the moment it lands — mirrors
    # the get-or-create call ``_trigger_foreman`` makes further down for
    # routing purposes; both share the same idempotent entry point.
    thread_id: str | None = None
    if from_agent == "user" and to_agent == "foreman" and content:
        if not ctx.ws_user_id:
            logger.warning("guild=%s unauthenticated user->foreman WS chat rejected", ctx.guild_id)
            await send_ws_message(
                ctx.websocket,
                ChatMsg.model_validate(
                    {
                        "from": "system",
                        "to": "user",
                        "content": "Not authenticated — reconnect or sign in again before messaging the Foreman.",
                        "createdAt": created_at.isoformat(),
                    }
                ),
            )
            return
        thread = await ensure_conversation_thread(ctx.guild_id, ctx.ws_user_id, content)
        thread_id = thread.id if thread else None

    ctx.db.add(
        Message(
            guild_id=ctx.guild_pk or 0,  # guild_pk is set during connection setup
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            message_type="chat",
            created_at=created_at,
            user_id=ctx.ws_user_id if from_agent == "user" else None,
            thread_id=thread_id,
        )
    )
    await ctx.db.commit()
    await broadcast_msg(
        ctx.guild_id,
        ChatMsg.model_validate(
            {
                "from": from_agent,
                "to": to_agent,
                "content": content,
                "createdAt": created_at.isoformat(),
                "userId": ctx.ws_user_id if from_agent == "user" else None,
                "threadId": thread_id,
            }
        ),
    )
    if not (from_agent == "user" and to_agent == "foreman" and content):
        return

    await _trigger_foreman(
        ctx.guild_id,
        "chat",
        content,
        user_id=ctx.ws_user_id,
        task_name=f"foreman.chat:{ctx.guild_id}",
    )
    reset_foreman_poll(ctx.guild_id)


async def handle_terminal_output(ctx: WSContext, data: dict) -> None:
    msg_agent_id = data.get("agentId")
    msg_worker_id = data.get("workerId")
    line = data.get("line", "")
    task_id = data.get("taskId")
    detail = data.get("detail")
    level = data.get("level")
    created_at = datetime.now(UTC)
    worker_id_for_log = msg_worker_id
    if worker_id_for_log is None and msg_agent_id:
        result = await ctx.db.exec(
            select(col(Agent.worker_id)).where(col(Agent.id) == msg_agent_id)
        )
        worker_id_for_log = result.one_or_none()
    if line:
        ctx.db.add(
            TaskLog(
                task_id=task_id or None,
                timestamp=created_at,
                line=line,
                worker_id=worker_id_for_log,
                agent_id=msg_agent_id,
                data=json.dumps(detail) if detail else None,
                level=level,
            )
        )
        await ctx.db.commit()
    await broadcast_msg(
        ctx.guild_id,
        TerminalOutputMsg(
            agentId=msg_agent_id,
            workerId=worker_id_for_log,
            taskId=task_id,
            line=line,
            timestamp=created_at.isoformat(),
            detail=detail if detail else None,
            level=level if level else None,
        ),
    )
    # Mirror agent/Claude output into the task's own Discord stream thread as a
    # low-priority feed (opt-in via DISCORD_STREAM_TASKS). Buffered internally,
    # so this call stays off the network path and is a no-op when disabled.
    if task_id and line and level in _DISCORD_STREAM_LEVELS:
        await discord_notifier.notify_task_stream(ctx.guild_id, task_id, line, detail)


async def handle_worker_register(ctx: WSContext, data: dict) -> None:
    worker_id = data.get("workerId")
    if not worker_id:
        return
    # Guard: only process worker-register from workers that belong to this guild.
    member_res = await ctx.db.exec(
        select(col(Worker.id)).where(
            col(Worker.id) == worker_id,
            col(Worker.guild_id) == ctx.guild_pk,
        )
    )
    if member_res.one_or_none() is None:
        logger.warning(
            "worker-register: worker %s is not a member of guild %s — ignoring",
            worker_id,
            ctx.guild_id,
        )
        return
    repos = data.get("repos") or []
    tools = data.get("tools") or []
    models = data.get("models") or {}
    user_ident = data.get("user")
    provider = data.get("provider") or None
    tool = data.get("tool") or None
    hostname = data.get("hostname") or None
    update_vals: dict = {
        "repos": json.dumps(repos),
        "tools": json.dumps(tools),
        "available_models": models if isinstance(models, dict) else {},
        "provider": provider,
        "tool": tool,
        "hostname": hostname,
    }
    if user_ident:
        resolved = await _resolve_user_identifier(ctx.db, user_ident)
        if resolved:
            update_vals["user_id"] = resolved
    await ctx.db.exec(
        update(Worker)
        .where(col(Worker.id) == worker_id, col(Worker.guild_id) == ctx.guild_pk)
        .values(**update_vals)
    )
    await ctx.db.commit()
    repos_str = ",".join(repos) if repos else ""
    tools_str = ",".join(tools) if tools else ""
    # Query the DB for agents belonging to this specific worker so the count
    # is accurate even when multiple workers share the same WS connection or
    # agents from previous sessions are still tracked in joined_agents.
    count_res = await ctx.db.exec(
        select(func.count(col(Agent.id))).where(
            col(Agent.worker_id) == worker_id,
            col(Agent.guild_id) == ctx.guild_pk,
            col(Agent.state) != "offline",
        )
    )
    agent_count = count_res.one()
    tools_suffix = f" tools={tools_str}" if tools_str else ""
    provider_suffix = f" provider={provider}" if provider else ""
    model_suffix = ""
    if isinstance(models, dict) and models:
        counts = {
            tool_name: len(rows) for tool_name, rows in models.items() if isinstance(rows, list)
        }
        if counts:
            model_suffix = " models=" + ",".join(f"{k}:{v}" for k, v in sorted(counts.items()))
    await _trigger_foreman(
        ctx.guild_id,
        "worker-online",
        f"[worker-online] worker_id={worker_id} repos={repos_str} agent_count={agent_count}{tools_suffix}{provider_suffix}{model_suffix}",
        task_name=f"foreman.worker-online:{worker_id}",
    )


async def handle_worker_disconnect(ctx: WSContext, data: dict) -> None:
    worker_id = data.get("workerId")
    reason = data.get("reason") or "shutdown"
    for agent_id in ctx.joined_agents:
        await ctx.db.exec(
            update(Agent)
            .where(col(Agent.id) == agent_id, col(Agent.guild_id) == ctx.guild_pk)
            .values(state="offline", activity=None, current_task_id=None)
        )
    if worker_id:
        await ctx.db.exec(
            update(Worker)
            .where(col(Worker.id) == worker_id, col(Worker.guild_id) == ctx.guild_pk)
            .values(state="offline")
        )
    if ctx.joined_agents or worker_id:
        await ctx.db.commit()
    for agent_id in ctx.joined_agents:
        await broadcast_msg(
            ctx.guild_id,
            AgentStateMsg(agentId=agent_id, state="offline"),
        )
    if worker_id:
        # Always mark as gracefully disconnected so the finally block does not
        # redundantly emit reason=disconnect for a worker that sent worker-disconnect.
        ctx.gracefully_disconnected_workers.add(worker_id)
        # Only notify the foreman if the worker was approved for this guild
        # during handle_join (avoids a redundant DB round-trip and is immune
        # to session state after the commit above).
        if worker_id in ctx.registered_worker_ids:
            await _trigger_foreman(
                ctx.guild_id,
                "worker-offline",
                f"[worker-offline] worker_id={worker_id} reason={reason}",
                task_name=f"foreman.worker-offline:{worker_id}",
            )
        else:
            logger.warning(
                "worker-disconnect: worker %s is not a member of guild %s — skipping foreman trigger",
                worker_id,
                ctx.guild_id,
            )
    # Worker disconnect is automated churn — ensure the loop, don't reset backoff.
    ensure_poll_loop(ctx.guild_id)


async def handle_task_rejected(ctx: WSContext, data: dict) -> None:
    """Handle a worker refusing an assignment because it has no free slot.

    This is a defensive backstop for races/stale state. The foreman should only
    assign to workers with idle agent capacity, but if an assignment reaches a
    full worker, the worker must not silently backlog it locally.
    """
    task_id = data.get("taskId")
    worker_id = data.get("workerId")
    reason = data.get("reason") or "worker has no idle agent slot"
    if not task_id or not worker_id:
        return
    result = await ctx.db.exec(
        select(Task).where(
            col(Task.id) == task_id,
            col(Task.guild_id) == ctx.guild_pk,
            col(Task.worker_id) == worker_id,
            col(Task.state).in_(["pending", "working"]),
        )
    )
    task = result.one_or_none()
    if task is None:
        logger.warning(
            "task-rejected: ignoring stale rejection task=%s worker=%s guild=%s reason=%s",
            task_id,
            worker_id,
            ctx.guild_id,
            reason,
        )
        return
    await ctx.db.exec(
        update(Task)
        .where(col(Task.id) == task_id, col(Task.guild_id) == ctx.guild_pk)
        .values(worker_id=None, state="pending")
    )
    ctx.db.add(
        TaskLog(
            task_id=task_id,
            timestamp=datetime.now(UTC),
            line=f"Worker {worker_id} rejected assignment: {reason}",
            worker_id=worker_id,
            level="worker",
        )
    )
    await LockService(ctx.db).release(f"task:{task_id}")
    await ctx.db.commit()
    await broadcast_msg(
        ctx.guild_id,
        TaskUpdateMsg(taskId=task_id, state="pending", workerId=None),
        exclude=ctx.websocket,
    )
    await broadcast_msg(
        ctx.guild_id,
        TaskRejectedMsg(taskId=task_id, workerId=worker_id, reason=reason),
        exclude=ctx.websocket,
    )
    task_uid = await _task_user_id(ctx.db, task_id)
    await _trigger_foreman(
        ctx.guild_id,
        "task-rejected",
        f"[task-rejected] Worker {worker_id} rejected task {task_id}: {reason}. "
        "The task was returned to pending/unassigned. Pick another idle worker or spawn one.",
        user_id=task_uid,
        task_id=task_id,
        task_name=f"foreman.task-rejected:{task_id}",
    )
    reset_foreman_poll(ctx.guild_id)


async def handle_task_update(ctx: WSContext, data: dict) -> None:
    task_id = data.get("taskId")
    if not task_id:
        return
    update_values: dict = {}
    for src, col_name in (
        ("state", "state"),
        ("branch", "branch"),
        ("worktreePath", "worktree_path"),
        ("prUrl", "pr_url"),
    ):
        if src in data:
            update_values[col_name] = data[src]
    # When the worker reports a PR URL, derive pr_number + pr_repo so github
    # webhook deliveries can be linked back to this task without fragile URL
    # substring matching at receive time.
    if "prUrl" in data:
        pr_number, pr_repo = _parse_pr_url(data.get("prUrl"))
        update_values["pr_number"] = pr_number
        update_values["pr_repo"] = pr_repo
    if update_values:
        await ctx.db.exec(update(Task).where(col(Task.id) == task_id).values(**update_values))
        if update_values.get("state") in _TERMINAL_STATES:
            await LockService(ctx.db).release(f"task:{task_id}")
        await ctx.db.commit()
    await broadcast_msg(ctx.guild_id, TaskUpdateMsg.model_validate(data), exclude=ctx.websocket)
    if "state" in update_values:
        # Worker-driven task state update is automated — ensure the loop, no reset.
        ensure_poll_loop(ctx.guild_id)
    # Free the task's Discord stream buffer once it reaches a terminal state
    # (failed/cancelled/error), draining any tail output first.
    if update_values.get("state") in _TERMINAL_STATES:
        spawn(
            discord_notifier.flush_task_stream(task_id),
            name=f"discord.stream-flush:{task_id}",
        )
    # Worker-reported terminal failures (bad tool config, unresolvable PR
    # branch, push errors, an agent run that didn't succeed, etc.) bypass the
    # foreman's finalize_task/cancel_task tools entirely — the worker sets
    # state directly via this task-update message. Unlike the success path
    # (handle_task_complete's "task-complete" notification below), nothing
    # else notifies Discord for these, so they were silently dropped (#920,
    # #1171). Mirrors task-complete's routing: post into the task's existing
    # thread when one exists, else the flat channel. "error" (a worker's
    # agent run that finished without succeeding — max-turns, push failure,
    # no commits, etc.) is included here as well as being handed to the
    # foreman below (#1171) — the foreman may take a while to act, or decide
    # to retry, so the operator still gets an immediate heads-up.
    if update_values.get("state") in ("failed", "cancelled", "error"):
        state_label = update_values["state"]
        worker_id_msg = data.get("workerId", "")
        stop_reason_msg = data.get("stopReason", "")
        last_text_msg = data.get("lastText", "")
        pr_url_msg = data.get("prUrl", "")
        task_row = (
            await ctx.db.exec(
                select(Task.name, Task.description, Task.issue_repo, Task.issue_number).where(
                    col(Task.id) == task_id
                )
            )
        ).one_or_none()
        task_label = ((task_row[0] or task_row[1] or task_id) if task_row else task_id)[:80]
        issue_repo = task_row[2] if task_row else None
        issue_number = task_row[3] if task_row else None

        description_parts = [
            f"Worker `{worker_id_msg}` reported task `{task_id}` ({task_label}) as {state_label}."
            if worker_id_msg
            else f"Task `{task_id}` ({task_label}) was marked {state_label}."
        ]
        if stop_reason_msg:
            description_parts.append(f"Stop reason: {stop_reason_msg}.")
        if last_text_msg:
            description_parts.append(f'Last output: "{_format_last_output(last_text_msg, 500)}"')
        if update_values.get("branch"):
            description_parts.append(f"Branch: {update_values['branch']}.")
        if pr_url_msg:
            description_parts.append(f"PR: {pr_url_msg}")
        if issue_repo and issue_number is not None:
            description_parts.append(f"Issue: {issue_repo}#{issue_number}")

        spawn(
            discord_notifier.notify_existing_thread(
                f"task-{state_label}",
                title=f"Task {state_label}: {task_label}",
                description=" ".join(description_parts)[:2000],
                issue_repo=issue_repo,
                issue_number=issue_number,
                url=pr_url_msg or None,
                task_id=task_id,
            ),
            name=f"discord.task-{state_label}:{task_id}",
        )
    # Drain any pending-followup events queued while the task was locked, and
    # notify the foreman so it can decide how to handle them.
    if update_values.get("state") == "error" and task_id:
        queued_payloads: list[dict] = []
        result = await ctx.db.exec(
            select(col(TaskEvent.id), col(TaskEvent.payload_json))
            .where(TaskEvent.task_id == task_id, TaskEvent.event_type == "pending-followup")
            .order_by(col(TaskEvent.id))
        )
        rows = result.all()
        if rows:
            event_ids = [r[0] for r in rows if r[0] is not None]
            queued_payloads = [_load_event_payload(r[1]) for r in rows]
            await ctx.db.exec(delete(TaskEvent).where(col(TaskEvent.id).in_(event_ids)))
            await ctx.db.commit()
        worker_id_upd = data.get("workerId", "a worker")
        task_uid = await _task_user_id(ctx.db, task_id)
        if queued_payloads:
            queued_summary = "\n".join(
                _format_queued_followup(i, p) for i, p in enumerate(queued_payloads)
            )
            human_msg = (
                f"[task-error] Worker {worker_id_upd} reported task {task_id} as errored. "
                f"While the task was locked, {len(queued_payloads)} follow-up request(s) were queued:\n"
                f"{queued_summary}\n"
                "The queued follow-ups were NOT dispatched because the task errored. "
                "Decide: call send_followup to retry, or call finalize_task with "
                "outcome='failed' to mark it failed."
            )
        else:
            stop_reason_upd = data.get("stopReason", "")
            last_text_upd = data.get("lastText", "")
            detail = f" Stop reason: {stop_reason_upd}." if stop_reason_upd else ""
            if last_text_upd:
                detail += f' Last output: "{_format_last_output(last_text_upd)}"'
            human_msg = (
                f"[task-error] Worker {worker_id_upd} reported task {task_id} as errored.{detail} "
                "Decide: call send_followup to retry the task, or call finalize_task with "
                "outcome='failed' to mark it failed."
            )
        await _trigger_foreman(
            ctx.guild_id,
            "task-error",
            human_msg,
            user_id=task_uid,
            task_id=task_id,
            task_name=f"foreman.task-error:{task_id}",
        )
        await ctx.db.commit()


async def handle_task_complete(ctx: WSContext, data: dict) -> None:
    task_id = data.get("taskId")
    worker_id_msg = data.get("workerId", "")
    desc = data.get("description", "")
    branch = data.get("branch", "")
    pr_url = data.get("prUrl", "")
    last_text = data.get("lastText", "")
    stop_reason = data.get("stopReason", "success")
    session_id = data.get("sessionId") or None
    if task_id:
        # Persist pr_url and session_id regardless of current state — the prior
        # task-update may have already moved the task to awaiting-review, so
        # the state guard below would be a no-op, but these still need writing.
        if session_id:
            await ctx.db.exec(
                update(Task).where(col(Task.id) == task_id).values(claude_session_id=session_id)
            )
        if pr_url:
            pr_number_val, pr_repo_val = _parse_pr_url(pr_url)
            await ctx.db.exec(
                update(Task)
                .where(col(Task.id) == task_id)
                .values(pr_url=pr_url, pr_number=pr_number_val, pr_repo=pr_repo_val)
            )
        reported_state = data.get("state")
        next_state = (
            reported_state
            if reported_state in {"awaiting-review", "awaiting-foreman-review"}
            else ("awaiting-review" if pr_url else "awaiting-foreman-review")
        )
        await ctx.db.exec(
            update(Task)
            .where(col(Task.id) == task_id, col(Task.state) == "working")
            .values(state=next_state)
        )
        await LockService(ctx.db).release(f"task:{task_id}")
        await ctx.db.commit()
    await broadcast_msg(ctx.guild_id, TaskCompleteMsg.model_validate(data), exclude=ctx.websocket)
    if task_id:
        spawn(
            maybe_post_plan_comment(ctx.guild_id, task_id, last_text),
            name=f"foreman.plan-comment:{task_id}",
        )
        # Drain the task's Discord stream buffer so any tail output lands
        # promptly and the in-memory buffer is freed.
        spawn(
            discord_notifier.flush_task_stream(task_id),
            name=f"discord.stream-flush:{task_id}",
        )
        _pr_num_disc, _pr_repo_disc = _parse_pr_url(pr_url) if pr_url else (None, None)
        spawn(
            discord_notifier.notify_event(
                "task-complete",
                title=f"Task complete: {desc[:80] or task_id}",
                description=f"Worker `{worker_id_msg}` finished task `{task_id}`."
                + (f" Branch: {branch}." if branch else "")
                + (f" PR: {pr_url}" if pr_url else ""),
                url=pr_url or None,
                issue_repo=_pr_repo_disc,
                issue_number=_pr_num_disc,
                kind="pr",
                ps_guild_slug=ctx.guild_id,
                task_id=task_id,
            ),
            name=f"discord.task-complete:{task_id}",
        )
    if not task_id:
        return

    task_uid = await _task_user_id(ctx.db, task_id)
    pr_line = f" PR: {pr_url}." if pr_url else ""
    last_text_snippet = f' Last output: "{_format_last_output(last_text)}".' if last_text else ""
    if pr_url:
        # PR exists: lifecycle is driven by GitHub webhooks, not the foreman.
        # The task will be auto-finalized on merge or auto-failed on close without merge.
        if stop_reason == "max_turns":
            foreman_message = (
                f"[task-complete/max-turns] Worker {worker_id_msg} task {task_id}: "
                f'"{desc[:80]}" — branch: {branch}.{pr_line} '
                f"Claude hit its max-turns limit before finishing. Partial work committed.{last_text_snippet} "
                "IMPORTANT: DO NOT call finalize_task — the task will be automatically "
                "finalized when the PR is merged (or marked failed if the PR is closed without "
                "merging). Use send_followup to continue work on the same branch/worktree."
            )
        else:
            foreman_message = (
                f"[task-complete] Worker {worker_id_msg} finished task {task_id}: "
                f'"{desc[:80]}" — branch: {branch}.{pr_line} '
                "IMPORTANT: DO NOT call finalize_task now. The task will be automatically "
                "finalized when the PR is merged (or automatically marked failed if the PR "
                "is closed without merging). Only call send_followup if CI fails or reviewers "
                "request changes."
            )
    elif stop_reason == "max_turns":
        foreman_message = (
            f"[task-complete/max-turns] Worker {worker_id_msg} task {task_id}: "
            f'"{desc[:80]}" — branch: {branch}. '
            f"Claude hit its max-turns limit and stopped before finishing. "
            f"Partial work has been committed and the branch pushed.{last_text_snippet} "
            "Call send_followup with a continuation prompt so the worker can resume on the "
            "same branch/worktree. Only call finalize_task if the partial work is sufficient "
            "or the task should be abandoned."
        )
    else:
        foreman_message = (
            f"[task-complete] Worker {worker_id_msg} finished task {task_id}: "
            f'"{desc[:80]}" — branch: {branch}. '
            "No PR was opened. Call send_followup for more work, or finalize_task to close "
            "this task (use outcome='failed' if the task did not succeed)."
        )
    await _trigger_foreman(
        ctx.guild_id,
        "task-complete",
        foreman_message,
        user_id=task_uid,
        task_id=task_id,
        task_name=f"foreman.task-complete:{task_id}",
    )
    reset_foreman_poll(ctx.guild_id)


async def handle_task_followup_done(ctx: WSContext, data: dict) -> None:
    task_id = data.get("taskId")
    worker_id_msg = data.get("workerId", "")
    stop_reason = data.get("stopReason", "success")
    last_text_fud = data.get("lastText", "")
    session_id_fud = data.get("sessionId") or None
    if task_id:
        if session_id_fud:
            await ctx.db.exec(
                update(Task).where(col(Task.id) == task_id).values(claude_session_id=session_id_fud)
            )
        pr_url_fud = data.get("prUrl", "")
        pr_update: dict = {}
        if pr_url_fud:
            pr_number_val, pr_repo_val = _parse_pr_url(pr_url_fud)
            pr_update = {"pr_url": pr_url_fud, "pr_number": pr_number_val, "pr_repo": pr_repo_val}
        reported_state_fud = data.get("state")
        next_state_fud = (
            reported_state_fud
            if reported_state_fud in {"awaiting-review", "awaiting-foreman-review"}
            else ("awaiting-review" if pr_url_fud else "awaiting-foreman-review")
        )
        # Move to the worker-reported post-run state unless task is already terminal.
        await ctx.db.exec(
            update(Task)
            .where(col(Task.id) == task_id, col(Task.state).not_in(_TERMINAL_STATES))
            .values(**pr_update, state=next_state_fud)
        )
        if pr_update:
            await ctx.db.exec(
                update(Task)
                .where(col(Task.id) == task_id, col(Task.state).in_(_TERMINAL_STATES))
                .values(**pr_update)
            )
        await LockService(ctx.db).release(f"task:{task_id}")
        await ctx.db.commit()
    await broadcast_msg(
        ctx.guild_id, TaskFollowupDoneMsg.model_validate(data), exclude=ctx.websocket
    )
    if not task_id:
        return

    # Drain the task's Discord stream buffer so this follow-up's tail output
    # lands promptly; a later follow-up re-creates the buffer on demand.
    spawn(
        discord_notifier.flush_task_stream(task_id),
        name=f"discord.stream-flush:{task_id}",
    )

    # Collect and drain any follow-up requests that were queued while the task
    # was locked, then re-trigger the foreman with their instructions so it can
    # decide whether to dispatch them.
    queued_payloads: list[dict] = []
    result = await ctx.db.exec(
        select(col(TaskEvent.id), col(TaskEvent.payload_json))
        .where(col(TaskEvent.task_id) == task_id, col(TaskEvent.event_type) == "pending-followup")
        .order_by(col(TaskEvent.id))
    )
    rows = result.all()
    if rows:
        event_ids = [r[0] for r in rows if r[0] is not None]
        queued_payloads = [_load_event_payload(r[1]) for r in rows]
        await ctx.db.exec(delete(TaskEvent).where(col(TaskEvent.id).in_(event_ids)))
        await ctx.db.commit()

    task_uid = await _task_user_id(ctx.db, task_id)

    if stop_reason == "max_turns":
        last_text_snippet = (
            f' Last output: "{_format_last_output(last_text_fud)}".' if last_text_fud else ""
        )
        human_msg = (
            f"[followup-done/max-turns] Worker {worker_id_msg} follow-up for task {task_id} "
            f"hit Claude's max-turns limit before finishing. Partial work committed.{last_text_snippet} "
            "Call send_followup with a continuation prompt to resume, or call finalize_task if "
            "the partial work is sufficient."
        )
    elif queued_payloads:
        queued_summary = "\n".join(
            _format_queued_followup(i, p) for i, p in enumerate(queued_payloads)
        )
        human_msg = (
            f"[followup-done] Worker {worker_id_msg} completed a follow-up for task {task_id}. "
            f"While the task was locked, {len(queued_payloads)} follow-up request(s) were queued:\n"
            f"{queued_summary}\n"
            "Review the queued instructions and call send_followup with the relevant ones "
            "(or a combined version), or call finalize_task if the work is done."
        )
    else:
        human_msg = (
            f"[followup-done] Worker {worker_id_msg} completed a follow-up for task {task_id}. "
            "Decide: call send_followup for more work, or call finalize_task to mark it done."
        )

    await _trigger_foreman(
        ctx.guild_id,
        "followup-done",
        human_msg,
        user_id=task_uid,
        task_id=task_id,
        task_name=f"foreman.followup-done:{task_id}",
    )
    reset_foreman_poll(ctx.guild_id)


async def handle_needs_input(ctx: WSContext, data: dict) -> None:
    await broadcast_msg(ctx.guild_id, NeedsInputMsg.model_validate(data), exclude=ctx.websocket)
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
    await _trigger_foreman(
        ctx.guild_id,
        "needs-input",
        escalation,
        user_id=task_uid,
        task_id=task_id or None,
        task_name=f"foreman.needs-input:{task_id or 'unknown'}",
    )
    reset_foreman_poll(ctx.guild_id)


async def handle_foreman_disconnect(ctx: WSContext, data: dict) -> None:
    """External Foreman API proxy announcing a graceful shutdown.

    Removes the guild's foreman_connections entry and fails any pending API
    requests owned by this socket immediately.
    """
    if foreman_connections.get(ctx.guild_id) is ctx.websocket:
        foreman_connections.pop(ctx.guild_id, None)
        fail_pending_for_websocket(
            ctx.websocket,
            f"external foreman API proxy disconnected gracefully for guild {ctx.guild_id}",
        )
        logger.info(
            "guild=%s external foreman API proxy disconnected gracefully",
            ctx.guild_id,
        )
    await broadcast_msg(
        ctx.guild_id,
        ForemanDisconnectMsg(guildId=ctx.guild_id),
        exclude=ctx.websocket,
    )


async def handle_foreman_api_response(ctx: WSContext, data: dict) -> None:
    """Resolve one pending LLM API request from the external proxy."""
    if foreman_connections.get(ctx.guild_id) is not ctx.websocket:
        logger.warning(
            "Ignoring foreman-api-response from non-active proxy for guild=%s",
            ctx.guild_id,
        )
        return
    resolve_foreman_api_response(data)


async def handle_webrtc_signal(ctx: WSContext, data: dict) -> None:
    """offer/answer/ice-candidate — forward to all peers in the guild."""
    rtc_type = data.get("type", "offer")
    cls = {"offer": OfferMsg, "answer": AnswerMsg, "ice-candidate": IceCandidateMsg}.get(
        rtc_type, OfferMsg
    )
    await broadcast_msg(ctx.guild_id, cls.model_validate(data), exclude=ctx.websocket)


HANDLERS: dict[str, Any] = {
    "ping": handle_ping,
    "join": handle_join,
    "agent-state": handle_agent_state,
    "chat": handle_chat,
    "terminal-output": handle_terminal_output,
    "worker-register": handle_worker_register,
    "worker-disconnect": handle_worker_disconnect,
    "worker-pong": handle_worker_pong,
    "task-update": handle_task_update,
    "task-rejected": handle_task_rejected,
    "task-complete": handle_task_complete,
    "task-followup-done": handle_task_followup_done,
    "needs-input": handle_needs_input,
    "foreman-disconnect": handle_foreman_disconnect,
    "foreman-api-response": handle_foreman_api_response,
    "offer": handle_webrtc_signal,
    "answer": handle_webrtc_signal,
    "ice-candidate": handle_webrtc_signal,
}


async def dispatch(ctx: WSContext, data: dict) -> None:
    """Route an inbound WS message to its handler.

    Known types are validated with Pydantic before the handler is called;
    malformed frames are dropped with a warning. Unknown types fall through
    to a generic broadcast (legacy pass-through behaviour).
    """
    msg_type = data.get("type") or ""
    handler = HANDLERS.get(msg_type)
    if handler is None:
        await broadcast(ctx.guild_id, data)
        return
    if msg_type in KNOWN_INBOUND_TYPES:
        try:
            parse_inbound_message(data)
        except ValidationError as exc:
            logger.warning(
                "guild=%s dropping malformed inbound WS message type=%s: %s",
                ctx.guild_id,
                msg_type,
                exc,
            )
            return
    await handler(ctx, data)
