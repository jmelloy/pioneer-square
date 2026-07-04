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
    connections,
    foreman_connections,
    pending_claude_auth,
    send_ws_message,
)
from fastapi import WebSocket
from lock_service import LockService
from models import Agent, Message, Task, TaskEvent, TaskLog, User, Worker
from pydantic import ValidationError
from sqlalchemy import delete, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from util.tasks import spawn
from utils import worker_display_name
from ws_types import (
    KNOWN_INBOUND_TYPES,
    AgentJoinedMsg,
    AgentStateMsg,
    AnswerMsg,
    ChatMsg,
    ClaudeAuthRequiredMsg,
    ForemanDisconnectMsg,
    ForemanEvictedMsg,
    ForemanRegisteredMsg,
    ForemanTriggerMsg,
    IceCandidateMsg,
    NeedsInputMsg,
    OfferMsg,
    PongMsg,
    TaskAssignedMsg,
    TaskCompleteMsg,
    TaskFollowupDoneMsg,
    TaskUpdateMsg,
    TerminalOutputMsg,
    WorkerAuthResponseMsg,
    parse_inbound_message,
)

from foreman import (
    maybe_post_plan_comment,
    reset_foreman_poll,
    resume_foreman_poll,
    run_foreman_ai,
    suppress_foreman_poll,
)

logger = logging.getLogger(__name__)

_TERMINAL_STATES = ("done", "failed", "cancelled", "error")


# ---------------------------------------------------------------------------
# Helpers (pure DB lookups; live here because main.py + ws_handlers both use
# them and importing back into main.py from this module is the natural edge).
# ---------------------------------------------------------------------------


def _parse_pr_url(pr_url: str | None) -> tuple[int | None, str | None]:
    """Extract ``(pr_number, "owner/repo")`` from a GitHub PR URL.

    Accepts both web URLs (``https://github.com/o/r/pull/42``) and API URLs
    (``https://api.github.com/repos/o/r/pulls/42``). Returns ``(None, None)``
    on anything that doesn't look like a PR URL — the caller stamps both
    columns to NULL in that case so we don't link a stale (number, repo) to
    a task whose PR was retracted.
    """
    if not pr_url or not isinstance(pr_url, str):
        return None, None
    parts = pr_url.rstrip("/").split("/")
    try:
        # ``…/{owner}/{repo}/pull/{n}`` or ``…/repos/{owner}/{repo}/pulls/{n}``
        if "pull" in parts:
            idx = parts.index("pull")
        elif "pulls" in parts:
            idx = parts.index("pulls")
        else:
            return None, None
        owner = parts[idx - 2]
        repo = parts[idx - 1]
        number = int(parts[idx + 1])
    except (ValueError, IndexError):
        return None, None
    if not owner or not repo:
        return None, None
    return number, f"{owner}/{repo}"


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
# Phase 2: External-foreman dispatch helper
# ---------------------------------------------------------------------------


# Trigger events that concern a single task's review loop. When the embedded
# foreman handles these (and a task_id is present), it runs in an isolated
# per-task child context. All other events stay on the whole-guild parent
# context. See docs/foreman-per-task-context.md.
_CHILD_FOREMAN_EVENTS = frozenset({"task-complete", "followup-done", "needs-input", "task-error"})


async def _trigger_foreman(
    guild_id: str,
    event: str,
    human_message: str,
    *,
    user_id: str | None = None,
    task_id: str | None = None,
    task_name: str = "foreman.unknown",
) -> None:
    """Send a ``foreman-trigger`` WS message to an external foreman if one is
    connected for this guild; otherwise fall back to the embedded foreman.

    The ``foreman-trigger`` payload carries all context the standalone process
    needs to call its own ``run_foreman_ai()``:

    .. code-block:: json

        {
          "type": "foreman-trigger",
          "event": "<event-type>",
          "guildId": "<guild_id>",
          "humanMessage": "<message>",
          "userId": "<user_id>",   // omitted when None
          "taskId": "<task_id>"    // omitted when None
        }

    If the send fails (broken socket) the foreman is evicted and the embedded
    fallback fires so the trigger is never lost.

    ``event`` mirrors the plan's trigger-type vocabulary:
    ``chat``, ``task-complete``, ``followup-done``, ``needs-input``,
    ``claude-auth``, ``periodic-check``, ``worker-online``,
    ``worker-offline``.
    """
    ws = foreman_connections.get(guild_id)
    if ws is not None:
        msg = ForemanTriggerMsg(
            event=event,
            guildId=guild_id,
            humanMessage=human_message,
            userId=user_id or None,  # coerce empty string to None
            taskId=task_id or None,  # coerce empty string to None
        )
        try:
            await send_ws_message(ws, msg)
            logger.debug(
                "guild=%s foreman-trigger dispatched to external foreman: event=%s",
                guild_id,
                event,
            )
            return
        except Exception:
            logger.warning(
                "guild=%s external foreman WS broken (event=%s), falling back to embedded",
                guild_id,
                event,
            )
            foreman_connections.pop(guild_id, None)
            resume_foreman_poll(guild_id)
    # Embedded fallback. Task-specific review events run in an isolated per-task
    # child context (see docs/foreman-per-task-context.md); cross-cutting events
    # (chat, worker lifecycle, periodic-check, claude-auth) stay on the parent
    # whole-guild context.
    child = bool(task_id) and event in _CHILD_FOREMAN_EVENTS
    spawn(
        run_foreman_ai(guild_id, human_message, user_id=user_id, task_id=task_id, child=child),
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
    is_external_foreman = agent_type == "foreman" and data.get("external") is True
    # Validate that the worker actually belongs to this guild before proceeding.
    # A misconfigured or misbehaving worker could connect to the wrong guild's
    # WebSocket; without this check it would create agent rows and broadcast
    # events into a guild it doesn't belong to.
    if agent_type == "worker" and worker_id:
        member_res = await ctx.db.exec(
            select(col(Worker.id)).where(
                col(Worker.id) == worker_id,
                col(Worker.guild_id) == ctx.guild_pk,
            )
        )
        if member_res.one_or_none() is None:
            logger.warning(
                "join: worker %s is not a member of guild %s — ignoring",
                worker_id,
                ctx.guild_id,
            )
            return
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
    # Only an explicit external foreman client may claim the foreman seat —
    # the legacy browser join still carries agentType="foreman" but must NOT
    # be registered in foreman_connections, or every chat trigger gets routed
    # to the browser (which has no handler) and the embedded foreman AI never
    # runs. The external client signals its intent with `external: true`.
    if agent_type == "foreman" and data.get("external") is True:
        # Register as the active external foreman for this guild.
        # Evict any previously connected foreman so there is never more than
        # one active at a time (prevents duplicate task mutations / triggers).
        existing_ws = foreman_connections.get(ctx.guild_id)
        if existing_ws is not None and existing_ws is not ctx.websocket:
            logger.info(
                "guild=%s evicting previous external foreman (new foreman connected)",
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
        suppress_foreman_poll(ctx.guild_id)
        logger.info("guild=%s external foreman registered: agentId=%s", ctx.guild_id, agent_id)
        # Acknowledge registration so the foreman knows it is the active one.
        await send_ws_message(
            ctx.websocket,
            ForemanRegisteredMsg(guildId=ctx.guild_id, agentId=agent_id),
        )
    elif agent_type == "worker":
        reset_foreman_poll(ctx.guild_id)
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
                        tool=pt.tool or "claude",
                        model=pt.model,
                        provider=pt.provider,
                        issueNumber=pt.issue_number,
                        issueRepo=pt.issue_repo,
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
    reset_foreman_poll(ctx.guild_id)


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
    ctx.db.add(
        Message(
            guild_id=ctx.guild_pk or 0,  # guild_pk is set during connection setup
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            message_type="chat",
            created_at=created_at,
            user_id=ctx.ws_user_id if from_agent == "user" else None,
        )
    )
    await ctx.db.commit()
    await broadcast_msg(
        ctx.guild_id,
        ChatMsg(
            **{"from": from_agent},
            to=to_agent,
            content=content,
            createdAt=created_at.isoformat(),
            userId=ctx.ws_user_id if ctx.ws_user_id and from_agent == "user" else None,
        ),
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
        await broadcast_msg(
            ctx.guild_id,
            WorkerAuthResponseMsg(workerId=pending_worker_id, code=content),
        )
    else:
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
    user_ident = data.get("user")
    provider = data.get("provider") or None
    tool = data.get("tool") or None
    update_vals: dict = {
        "repos": json.dumps(repos),
        "tools": json.dumps(tools),
        "provider": provider,
        "tool": tool,
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
    await _trigger_foreman(
        ctx.guild_id,
        "worker-online",
        f"[worker-online] worker_id={worker_id} repos={repos_str} agent_count={agent_count}{tools_suffix}{provider_suffix}",
        task_name=f"foreman.worker-online:{worker_id}",
    )


async def handle_worker_disconnect(ctx: WSContext, data: dict) -> None:
    worker_id = data.get("workerId")
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
                f"[worker-offline] worker_id={worker_id} reason=shutdown",
                task_name=f"foreman.worker-offline:{worker_id}",
            )
        else:
            logger.warning(
                "worker-disconnect: worker %s is not a member of guild %s — skipping foreman trigger",
                worker_id,
                ctx.guild_id,
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
        reset_foreman_poll(ctx.guild_id)
    # Drain any pending-followup events queued while the task was locked, and
    # notify the foreman so it can decide how to handle them.
    if update_values.get("state") == "error" and task_id:
        queued_payloads: list[dict] = []
        result = await ctx.db.exec(
            select(TaskEvent.id, TaskEvent.payload_json)
            .where(TaskEvent.task_id == task_id, TaskEvent.event_type == "pending-followup")
            .order_by(TaskEvent.id)
        )
        rows = result.all()
        if rows:
            event_ids = [r[0] for r in rows]
            queued_payloads = [json.loads(r[1]) for r in rows]
            await ctx.db.exec(delete(TaskEvent).where(TaskEvent.id.in_(event_ids)))
            await ctx.db.commit()
        worker_id_upd = data.get("workerId", "a worker")
        task_uid = await _task_user_id(ctx.db, task_id)
        if queued_payloads:
            queued_summary = "\n".join(
                f"  {i + 1}. {p.get('instructions', '')}" for i, p in enumerate(queued_payloads)
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
            human_msg = (
                f"[task-error] Worker {worker_id_upd} reported task {task_id} as errored. "
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
    if task_id:
        # Persist pr_url regardless of current state — the prior task-update may
        # have already moved the task to awaiting-review, so the state guard below
        # would be a no-op, but pr_url still needs to be written.
        if pr_url:
            pr_number_val, pr_repo_val = _parse_pr_url(pr_url)
            await ctx.db.exec(
                update(Task)
                .where(col(Task.id) == task_id)
                .values(pr_url=pr_url, pr_number=pr_number_val, pr_repo=pr_repo_val)
            )
        await ctx.db.exec(
            update(Task)
            .where(col(Task.id) == task_id, col(Task.state) == "working")
            .values(state="awaiting-review")
        )
        await LockService(ctx.db).release(f"task:{task_id}")
        await ctx.db.commit()
    await broadcast_msg(ctx.guild_id, TaskCompleteMsg.model_validate(data), exclude=ctx.websocket)
    if task_id:
        spawn(
            maybe_post_plan_comment(ctx.guild_id, task_id, last_text),
            name=f"foreman.plan-comment:{task_id}",
        )
        _pr_repo_disc, _pr_num_disc = _parse_pr_url(pr_url) if pr_url else (None, None)
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
                ps_guild_slug=ctx.guild_id,
            ),
            name=f"discord.task-complete:{task_id}",
        )
    if not task_id:
        return

    task_uid = await _task_user_id(ctx.db, task_id)
    pr_line = f" PR: {pr_url}." if pr_url else ""
    last_text_snippet = f' Last output: "{last_text[:200]}".' if last_text else ""
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
    if task_id:
        pr_url_fud = data.get("prUrl", "")
        pr_update: dict = {}
        if pr_url_fud:
            pr_number_val, pr_repo_val = _parse_pr_url(pr_url_fud)
            pr_update = {"pr_url": pr_url_fud, "pr_number": pr_number_val, "pr_repo": pr_repo_val}
        # Move to awaiting-review unless task is already terminal.
        await ctx.db.exec(
            update(Task)
            .where(col(Task.id) == task_id, col(Task.state).not_in(_TERMINAL_STATES))
            .values(**pr_update, state="awaiting-review")
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
        event_ids = [r[0] for r in rows]
        queued_payloads = [json.loads(r[1]) for r in rows]
        await ctx.db.exec(delete(TaskEvent).where(col(TaskEvent.id).in_(event_ids)))
        await ctx.db.commit()

    task_uid = await _task_user_id(ctx.db, task_id)

    if stop_reason == "max_turns":
        last_text_snippet = f' Last output: "{last_text_fud[:200]}".' if last_text_fud else ""
        human_msg = (
            f"[followup-done/max-turns] Worker {worker_id_msg} follow-up for task {task_id} "
            f"hit Claude's max-turns limit before finishing. Partial work committed.{last_text_snippet} "
            "Call send_followup with a continuation prompt to resume, or call finalize_task if "
            "the partial work is sufficient."
        )
    elif queued_payloads:
        queued_summary = "\n".join(
            f"  {i + 1}. {p.get('instructions', '')}" for i, p in enumerate(queued_payloads)
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


async def handle_claude_auth_required(ctx: WSContext, data: dict) -> None:
    worker_id = data.get("workerId", "a worker")
    auth_url = data.get("url", "")
    logger.info(
        "claude-auth-required from %s in guild %s url=%s",
        worker_id,
        ctx.guild_id,
        auth_url[:80],
    )
    await broadcast_msg(
        ctx.guild_id, ClaudeAuthRequiredMsg.model_validate(data), exclude=ctx.websocket
    )
    pending_claude_auth.setdefault(ctx.guild_id, {})[worker_id] = auth_url
    logger.info(
        "pending_claude_auth now has %d entries for guild %s",
        len(pending_claude_auth.get(ctx.guild_id, {})),
        ctx.guild_id,
    )
    await _trigger_foreman(
        ctx.guild_id,
        "claude-auth",
        f"Worker {worker_id} needs Claude authentication. "
        f"Auth URL: {auth_url}. "
        "A human must visit this URL, complete authentication, then paste the "
        "resulting code into the auth panel that has appeared in the chat UI "
        "(or type it into the Foreman Comms input). The worker is waiting.",
        task_name=f"foreman.claude-auth:{worker_id}",
    )
    reset_foreman_poll(ctx.guild_id)


async def handle_foreman_disconnect(ctx: WSContext, data: dict) -> None:
    """External foreman announcing a graceful shutdown.

    Removes the guild's foreman_connections entry so subsequent trigger events
    fall back to the embedded foreman immediately rather than waiting for a
    failed send to discover the socket is gone.
    """
    if foreman_connections.get(ctx.guild_id) is ctx.websocket:
        foreman_connections.pop(ctx.guild_id, None)
        resume_foreman_poll(ctx.guild_id)
        logger.info(
            "guild=%s external foreman disconnected gracefully",
            ctx.guild_id,
        )
    await broadcast_msg(
        ctx.guild_id,
        ForemanDisconnectMsg(guildId=ctx.guild_id),
        exclude=ctx.websocket,
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
    await broadcast_msg(ctx.guild_id, WorkerAuthResponseMsg.model_validate(data))


async def handle_foreman_broadcast(ctx: WSContext, data: dict) -> None:
    """External foreman relays a broadcast payload to all guild connections.

    The standalone foreman/main.py cannot call broadcast() directly (its
    connections dict is empty — only the backend process holds live WS
    connections).  Instead it sends a foreman-broadcast envelope; this handler
    extracts the payload and fans it out to every frontend client in the guild.
    """
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return
    await broadcast(ctx.guild_id, payload, exclude=ctx.websocket)


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
    "task-complete": handle_task_complete,
    "task-followup-done": handle_task_followup_done,
    "needs-input": handle_needs_input,
    "claude-auth-required": handle_claude_auth_required,
    "foreman-disconnect": handle_foreman_disconnect,
    "foreman-broadcast": handle_foreman_broadcast,
    "worker-auth-response": handle_worker_auth_response,
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
