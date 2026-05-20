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

from events import (
    agent_owner_lock,
    agent_owners,
    broadcast,
    connections,
    foreman_connections,
    pending_claude_auth,
)
from fastapi import WebSocket
from models import Agent, Message, Task, TaskEvent, TaskLog, User, Worker
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from util.tasks import spawn
from utils import worker_display_name

from foreman import maybe_post_plan_comment, reset_foreman_poll, run_foreman_ai

logger = logging.getLogger(__name__)

_TERMINAL_STATES = ("done", "failed", "cancelled")


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
# Phase 2: External-foreman dispatch helper
# ---------------------------------------------------------------------------


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
    ``claude-auth``, ``periodic-check``.
    """
    ws = foreman_connections.get(guild_id)
    if ws is not None:
        payload: dict = {
            "type": "foreman-trigger",
            "event": event,
            "guildId": guild_id,
            "humanMessage": human_message,
        }
        if user_id:
            payload["userId"] = user_id
        if task_id:
            payload["taskId"] = task_id
        try:
            await ws.send_json(payload)
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
    # Embedded fallback — identical to the pre-Phase-2 behaviour.
    spawn(
        run_foreman_ai(guild_id, human_message, user_id=user_id),
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
    db: object  # AsyncSession; typed as object to avoid SQLAlchemy import dance
    joined_agents: set[str] = field(default_factory=set)
    ws_user_id: str | None = None
    guild_pk: int | None = None


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
    stmt = pg_insert(Agent).values(
        id=agent_id,
        guild_pk=ctx.guild_pk,
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
            "guild_pk": stmt.excluded.guild_pk,
            "worker_id": stmt.excluded.worker_id,
            "name": stmt.excluded.name,
            "type": stmt.excluded.type,
            "state": stmt.excluded.state,
            "current_task_id": stmt.excluded.current_task_id,
            "joined_at": stmt.excluded.joined_at,
            "last_seen": stmt.excluded.last_seen,
        },
    )
    await ctx.db.execute(stmt)
    if agent_type == "worker" and worker_id:
        await ctx.db.execute(
            update(Worker)
            .where(Worker.id == worker_id, Worker.guild_pk == ctx.guild_pk)
            .values(state="online", last_seen=joined_at)
        )
    await ctx.db.commit()
    if agent_id:
        ctx.joined_agents.add(agent_id)
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
                await existing_ws.send_json(
                    {
                        "type": "foreman-evicted",
                        "guildId": ctx.guild_id,
                        "reason": "superseded by new foreman connection",
                    }
                )
            except Exception:
                pass
        foreman_connections[ctx.guild_id] = ctx.websocket
        logger.info("guild=%s external foreman registered: agentId=%s", ctx.guild_id, agent_id)
        # Acknowledge registration so the foreman knows it is the active one.
        await ctx.websocket.send_json(
            {
                "type": "foreman-registered",
                "guildId": ctx.guild_id,
                "agentId": agent_id,
            }
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
            result = await ctx.db.execute(
                select(Task).where(
                    Task.guild_pk == ctx.guild_pk,
                    Task.worker_id == worker_id,
                    Task.state.in_(["pending", "working"]),
                )
            )
            pending_tasks = result.scalars().all()
            for pt in pending_tasks:
                logger.info(
                    "join: replaying task-assigned for pending task %s to worker %s",
                    pt.id,
                    worker_id,
                )
                await ctx.websocket.send_json(
                    {
                        "type": "task-assigned",
                        "workerId": worker_id,
                        "taskId": pt.id,
                        "name": pt.name or "",
                        "description": pt.description or "",
                        "tool": pt.tool or "claude",
                        "issueNumber": pt.issue_number,
                        "issueRepo": pt.issue_repo,
                    }
                )
    broadcast_payload: dict = {
        "type": "agent-joined",
        "agentId": agent_id,
        "agentName": agent_name,
        "agentType": agent_type,
        "workerId": worker_id,
        "state": "idle",
        "joinedAt": joined_at,
    }
    if worker_id:
        r = await ctx.db.execute(
            select(Worker.name).where(Worker.id == worker_id, Worker.guild_pk == ctx.guild_pk)
        )
        stored_name = r.scalar_one_or_none()
        broadcast_payload["workerName"] = stored_name or worker_display_name(worker_id)
    await broadcast(ctx.guild_id, broadcast_payload)


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
    await ctx.db.execute(
        update(Agent)
        .where(Agent.id == agent_id, Agent.guild_pk == ctx.guild_pk)
        .values(**update_vals)
    )
    await ctx.db.commit()
    msg: dict = {"type": "agent-state", "agentId": agent_id, "state": state}
    if worker_id:
        msg["workerId"] = worker_id
    if "activity" in update_vals:
        msg["activity"] = update_vals["activity"]
    if "current_task_id" in update_vals:
        msg["taskId"] = update_vals["current_task_id"]
    await broadcast(ctx.guild_id, msg)
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
    created_at = datetime.now(UTC).isoformat()
    ctx.db.add(
        Message(
            guild_pk=ctx.guild_pk,
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
    created_at = datetime.now(UTC).isoformat()
    worker_id_for_log = msg_worker_id
    if worker_id_for_log is None and msg_agent_id:
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
        .where(Worker.id == worker_id, Worker.guild_pk == ctx.guild_pk)
        .values(**update_vals)
    )
    await ctx.db.commit()


async def handle_worker_disconnect(ctx: WSContext, data: dict) -> None:
    worker_id = data.get("workerId")
    for agent_id in ctx.joined_agents:
        await ctx.db.execute(
            update(Agent)
            .where(Agent.id == agent_id, Agent.guild_pk == ctx.guild_pk)
            .values(state="offline", activity=None, current_task_id=None)
        )
    if worker_id:
        await ctx.db.execute(
            update(Worker)
            .where(Worker.id == worker_id, Worker.guild_pk == ctx.guild_pk)
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
    # When the worker reports a PR URL, derive pr_number + pr_repo so github
    # webhook deliveries can be linked back to this task without fragile URL
    # substring matching at receive time.
    if "prUrl" in data:
        pr_number, pr_repo = _parse_pr_url(data.get("prUrl"))
        update_values["pr_number"] = pr_number
        update_values["pr_repo"] = pr_repo
    if update_values:
        if update_values.get("state") in _TERMINAL_STATES:
            update_values["locked_at"] = None
            update_values["lock_holder"] = None
        await ctx.db.execute(update(Task).where(Task.id == task_id).values(**update_values))
        await ctx.db.commit()
    await broadcast(ctx.guild_id, data, exclude=ctx.websocket)
    if "state" in update_values:
        reset_foreman_poll(ctx.guild_id)


async def handle_task_complete(ctx: WSContext, data: dict) -> None:
    task_id = data.get("taskId")
    worker_id_msg = data.get("workerId", "")
    desc = data.get("description", "")
    branch = data.get("branch", "")
    pr_url = data.get("prUrl", "")
    last_text = data.get("lastText", "")
    if task_id:
        # Persist pr_url regardless of current state — the prior task-update may
        # have already moved the task to awaiting-review, so the state guard below
        # would be a no-op, but pr_url still needs to be written.
        if pr_url:
            pr_number_val, pr_repo_val = _parse_pr_url(pr_url)
            await ctx.db.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(pr_url=pr_url, pr_number=pr_number_val, pr_repo=pr_repo_val)
            )
        await ctx.db.execute(
            update(Task)
            .where(Task.id == task_id, Task.state == "working")
            .values(state="awaiting-review")
        )
        await ctx.db.commit()
    await broadcast(ctx.guild_id, data, exclude=ctx.websocket)
    if task_id:
        spawn(
            maybe_post_plan_comment(ctx.guild_id, task_id, last_text),
            name=f"foreman.plan-comment:{task_id}",
        )
    if not task_id:
        return

    task_uid = await _task_user_id(ctx.db, task_id)
    pr_line = f" PR: {pr_url}." if pr_url else ""
    await _trigger_foreman(
        ctx.guild_id,
        "task-complete",
        f"[task-complete] Worker {worker_id_msg} finished task {task_id}: "
        f'"{desc[:80]}" — branch: {branch}.{pr_line} '
        "The worker has returned to its idle pool; the task is parked in "
        "awaiting-review for human review. "
        "Default behaviour: leave PR-bearing tasks open so reviewers can "
        "comment — call send_followup if a comment or CI failure asks for "
        "an iteration on the same branch (any idle worker can pick it up). "
        "Only call finalize_task when the work is genuinely closed (PR "
        "merged, task abandoned, or it was an ephemeral/automation task).",
        user_id=task_uid,
        task_id=task_id,
        task_name=f"foreman.task-complete:{task_id}",
    )
    reset_foreman_poll(ctx.guild_id)


async def handle_task_followup_done(ctx: WSContext, data: dict) -> None:
    task_id = data.get("taskId")
    worker_id_msg = data.get("workerId", "")
    if task_id:
        pr_url_fud = data.get("prUrl", "")
        lock_release: dict = {"locked_at": None, "lock_holder": None}
        if pr_url_fud:
            pr_number_val, pr_repo_val = _parse_pr_url(pr_url_fud)
            lock_release.update(
                {"pr_url": pr_url_fud, "pr_number": pr_number_val, "pr_repo": pr_repo_val}
            )
        # Move to awaiting-review and release lock unless task is already terminal.
        await ctx.db.execute(
            update(Task)
            .where(Task.id == task_id, Task.state.not_in(_TERMINAL_STATES))
            .values(**lock_release, state="awaiting-review")
        )
        await ctx.db.execute(
            update(Task)
            .where(Task.id == task_id, Task.state.in_(_TERMINAL_STATES))
            .values(**lock_release)
        )
        await ctx.db.commit()
    await broadcast(ctx.guild_id, data, exclude=ctx.websocket)
    if not task_id:
        return

    # Collect and drain any follow-up requests that were queued while the task
    # was locked, then re-trigger the foreman with their instructions so it can
    # decide whether to dispatch them.
    queued_payloads: list[dict] = []
    result = await ctx.db.execute(
        select(TaskEvent.id, TaskEvent.payload_json)
        .where(TaskEvent.task_id == task_id, TaskEvent.event_type == "pending-followup")
        .order_by(TaskEvent.id)
    )
    rows = result.all()
    if rows:
        event_ids = [r[0] for r in rows]
        queued_payloads = [json.loads(r[1]) for r in rows]
        await ctx.db.execute(delete(TaskEvent).where(TaskEvent.id.in_(event_ids)))
        await ctx.db.commit()

    task_uid = await _task_user_id(ctx.db, task_id)

    if queued_payloads:
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
    await broadcast(ctx.guild_id, data, exclude=ctx.websocket)
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
        logger.info(
            "guild=%s external foreman disconnected gracefully",
            ctx.guild_id,
        )
    await broadcast(
        ctx.guild_id,
        {"type": "foreman-disconnect", "guildId": ctx.guild_id},
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
    await broadcast(ctx.guild_id, data)


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
    "foreman-disconnect": handle_foreman_disconnect,
    "foreman-broadcast": handle_foreman_broadcast,
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
