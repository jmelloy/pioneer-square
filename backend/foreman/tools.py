"""Foreman tool definitions, GitHub API helpers, and tool-call executor."""

import asyncio
import json
import logging
import random
import string
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from database import get_db
from events import broadcast, emit_terminal_line
from models import Agent, GithubToken, Guild, Task, TaskLog, Worker
from sqlalchemy import select, update

# Default soft-delete window (seconds) when finalize_task is called without
# an explicit expiry. Mirrors backend.main.DEFAULT_FINALIZE_TTL.
DEFAULT_FINALIZE_TTL_SECONDS = 3 * 24 * 60 * 60  # 3 days


def _resolve_finalize_deleted_at(inp: dict) -> tuple[str, str | None]:
    """Compute the soft-delete instant for a finalize_task tool call.

    Returns ``(deleted_at_iso, error)`` — error is non-None when the inputs
    were malformed. Honours an explicit ``deleted_at`` first, then
    ``expires_in_seconds``, otherwise falls back to the default 3-day window.
    """
    raw = inp.get("deleted_at")
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError as exc:
            return "", f"Invalid deleted_at: {exc}"
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat(), None
    seconds = inp.get("expires_in_seconds")
    if seconds is not None:
        try:
            secs = int(seconds)
        except (TypeError, ValueError):
            return "", f"Invalid expires_in_seconds: {seconds!r}"
        if secs < 0:
            return "", "expires_in_seconds must be >= 0"
        return (datetime.now(UTC) + timedelta(seconds=secs)).isoformat(), None
    default = datetime.now(UTC) + timedelta(seconds=DEFAULT_FINALIZE_TTL_SECONDS)
    return default.isoformat(), None


FOREMAN_TOOLS = [
    {
        "name": "create_task",
        "description": (
            "Create a named foreman task. Call this before assigning worker tasks — it gives the "
            "work a human-readable name visible in the sidebar and returns a task_id to reference "
            "in assign_task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short human-readable name (≤60 chars), e.g. 'Implement OAuth login'.",
                },
                "description": {
                    "type": "string",
                    "description": "Full description of the work to be done.",
                },
                "phase": {
                    "type": "string",
                    "enum": ["plan", "execute", "review"],
                    "description": "Starting phase. Default: execute.",
                },
            },
            "required": ["name", "description"],
        },
    },
    {
        "name": "assign_task",
        "description": (
            "Queue a coding task for a worker agent. The worker creates a git worktree, "
            "runs the chosen coding agent on the description, then pushes the branch. "
            "Pass task_id (from create_task) to assign that existing task to a worker instead "
            "of creating a duplicate — this is the preferred flow."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "worker_id": {
                    "type": "string",
                    "description": "Worker agent ID (e.g. w-abc123). Must be idle.",
                },
                "description": {
                    "type": "string",
                    "description": "Detailed, self-contained task description the coding agent receives.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID returned by create_task. When provided, assigns that "
                    "existing task to the worker instead of creating a new row.",
                },
                "name": {
                    "type": "string",
                    "description": "Short task name shown in the sidebar (≤60 chars).",
                },
                "tool": {
                    "type": "string",
                    "enum": ["claude", "codex", "pi"],
                    "description": "Coding agent to use. Default: claude.",
                },
                "parent_task_id": {
                    "type": "string",
                    "description": "Foreman task ID this worker task belongs to (optional, ignored if task_id provided).",
                },
                "phase": {
                    "type": "string",
                    "enum": ["plan", "execute", "review", "followup"],
                    "description": "Phase of work.",
                },
                "issue_number": {
                    "type": "integer",
                    "description": "GitHub issue to close (optional).",
                },
                "issue_repo": {
                    "type": "string",
                    "description": "owner/repo for the issue (optional).",
                },
            },
            "required": ["worker_id", "description"],
        },
    },
    {
        "name": "send_followup",
        "description": (
            "Send follow-up instructions to a worker for a task that just completed. "
            "The worker executes these in the same git worktree on the same branch — "
            "ideal for 'update tests', 'fix type errors', 'add docs', etc. "
            "Call after receiving a task-complete notification."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID (t-xxxxxx)."},
                "instructions": {
                    "type": "string",
                    "description": "Follow-up instructions to execute in the existing worktree.",
                },
            },
            "required": ["task_id", "instructions"],
        },
    },
    {
        "name": "finalize_task",
        "description": (
            "Mark a task complete with no further follow-up needed. "
            "Call after reviewing a completed task when no additional work is required. "
            "Tasks are soft-deleted after their expiry window so the table doesn't "
            "accumulate cruft. Pick the window by task type:\n"
            "  - Ephemeral tasks (periodic-check, status-poll, automated health "
            "checks): expires_in_seconds = 1200 (20 minutes)\n"
            "  - Code tasks (execute / review / followup phases): omit the field "
            "to use the default 3 days, or pass expires_in_seconds = 259200\n"
            "  - Error / failed tasks: expires_in_seconds = 86400 (1 day)\n"
            "Pass deleted_at instead if you need an exact ISO-8601 timestamp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to finalize."},
                "expires_in_seconds": {
                    "type": "integer",
                    "description": (
                        "Seconds from now until the task is soft-deleted. "
                        "Defaults to 259200 (3 days) when omitted."
                    ),
                },
                "deleted_at": {
                    "type": "string",
                    "description": (
                        "ISO-8601 UTC timestamp at which the task is soft-deleted. "
                        "Takes precedence over expires_in_seconds when both are set."
                    ),
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "message_worker",
        "description": "Send a message to a worker's terminal — for mid-task context injection.",
        "input_schema": {
            "type": "object",
            "properties": {
                "worker_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["worker_id", "message"],
        },
    },
    {
        "name": "redirect_task",
        "description": (
            "Redirect a running task mid-execution with new instructions. "
            "Terminates the current Claude subprocess, then immediately resumes it in the same "
            "session — Claude keeps full context of what it was doing and acts on the new "
            "instructions instead. For tasks awaiting review, acts as a follow-up. "
            "Use this to course-correct without losing progress."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID (t-xxxxxx) to redirect."},
                "instructions": {
                    "type": "string",
                    "description": "New instructions. Claude will see its full prior history.",
                },
            },
            "required": ["task_id", "instructions"],
        },
    },
    {
        "name": "cancel_task",
        "description": (
            "Cancel a running or pending task. Use when a task is going in the wrong direction, "
            "is stuck, or is no longer needed. The worker terminates its Claude subprocess "
            "immediately and releases the agent slot. Cannot cancel tasks that are already done or failed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID (t-xxxxxx) to cancel."},
                "reason": {"type": "string", "description": "Optional reason for cancellation."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "shutdown_worker",
        "description": (
            "Send a shutdown signal to a worker agent, causing it to gracefully stop. "
            "Idle agents exit immediately; busy agents finish their current task and skip "
            "the follow-up window. The worker process disconnects and transitions to offline. "
            "Use when a worker is misbehaving, the operator is winding down, or a host needs "
            "to be freed up — prefer cancel_task for stopping a single bad task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "worker_id": {
                    "type": "string",
                    "description": "Worker agent ID (e.g. w-abc123).",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason for shutdown.",
                },
            },
            "required": ["worker_id"],
        },
    },
    {
        "name": "list_github_issues",
        "description": (
            "List GitHub issues for a repo. Use this to discover work that needs to be done."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo, e.g. 'acme/backend'"},
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Default: open",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max issues to return (default 20, max 50)",
                },
            },
            "required": ["repo"],
        },
    },
    {
        "name": "get_github_issue",
        "description": "Get full details of a single GitHub issue including its body and comments.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "issue_number": {"type": "integer"},
            },
            "required": ["repo", "issue_number"],
        },
    },
    {
        "name": "list_github_prs",
        "description": "List pull requests for a repo — useful for reviewing completed worker branches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Default: open",
                },
            },
            "required": ["repo"],
        },
    },
    {
        "name": "claim_github_issue",
        "description": (
            "Assign a GitHub issue to the authenticated user (claim it for this guild's operator). "
            "Call this when picking up an issue to work on, before assigning a worker task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "issue_number": {"type": "integer"},
            },
            "required": ["repo", "issue_number"],
        },
    },
    {
        "name": "get_task_status",
        "description": (
            "Get the current status of a task: state, phase, assigned worker and active agent state, "
            "and the last log lines. Use this to verify a task is progressing and to diagnose stalls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID (t-xxxxxx)."},
                "log_lines": {
                    "type": "integer",
                    "description": "Number of recent log lines to return (default 10, max 50).",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "create_github_issue",
        "description": (
            "Create a new GitHub issue to track work before assigning it to a worker. "
            "Search first with search_github_issues to avoid duplicates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "title": {"type": "string", "description": "Issue title."},
                "body": {"type": "string", "description": "Issue body in markdown."},
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Label names to apply (labels must already exist in the repo).",
                },
            },
            "required": ["repo", "title", "body"],
        },
    },
    {
        "name": "search_github_issues",
        "description": (
            "Search GitHub issues and PRs by keyword within a repo. "
            "Call this before create_github_issue to check whether an issue already exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo to restrict search to."},
                "query": {"type": "string", "description": "Search keywords."},
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Filter by state. Default: open.",
                },
            },
            "required": ["repo", "query"],
        },
    },
]


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _gh_api(path: str, token: str) -> object:
    """GET a GitHub API path and return parsed JSON."""
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _gh_api_post(path: str, token: str, payload: dict, method: str = "POST") -> object:
    """POST/PATCH a GitHub API path with a JSON body and return parsed JSON."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


async def _guild_github_token(guild_id: str) -> tuple[str, str] | None:
    """Return (access_token, github_username) for this guild, or None."""
    db = await get_db()
    try:
        result = await db.execute(
            select(GithubToken.access_token, GithubToken.github_username)
            .join(Guild, Guild.github_user_id == GithubToken.github_user_id)
            .where(Guild.id == guild_id)
        )
        row = result.first()
        return (row.access_token, row.github_username) if row else None
    finally:
        await db.close()


async def maybe_post_plan_comment(guild_id: str, task_id: str, last_text: str) -> None:
    """Post plan output as a GitHub issue comment when a plan-phase task completes."""
    logger = logging.getLogger(__name__)
    try:
        db = await get_db()
        try:
            result = await db.execute(
                select(Task.phase, Task.issue_number, Task.issue_repo).where(Task.id == task_id)
            )
            row = result.first()
        finally:
            await db.close()

        if not row or row.phase != "plan":
            return
        issue_number = row.issue_number
        issue_repo = row.issue_repo
        if not issue_number or not issue_repo:
            return
        if not last_text:
            logger.warning("plan comment: task %s has no output to post", task_id)
            return

        creds = await _guild_github_token(guild_id)
        if not creds:
            logger.warning("plan comment: no GitHub token for guild %s", guild_id)
            return
        token, _ = creds

        body = f"## \U0001f4cb Plan from task `{task_id}`\n\n{last_text}"
        await asyncio.to_thread(
            _gh_api_post,
            f"/repos/{issue_repo}/issues/{issue_number}/comments",
            token,
            {"body": body},
        )
        logger.info("plan comment posted to %s#%s for task %s", issue_repo, issue_number, task_id)
    except Exception as exc:
        logger.warning("plan comment failed for task %s: %s", task_id, exc)


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------


async def exec_tools(guild_id: str, tool_uses: list, user_id: str | None = None) -> list:
    """Execute tool calls from the foreman AI and return tool-result blocks.

    Independent tool calls in the same batch run concurrently — each opens its
    own DB session and the GitHub helpers already hop to a thread pool, so
    parallelism is safe and reduces user-visible latency when Claude emits
    several tools in one turn (a common case for read-only lookups).
    Results are returned in the same order as *tool_uses* to match the
    Anthropic API's tool_result contract.

    *user_id* identifies the human whose foreman session is running. It's
    stamped onto any tasks created by ``create_task`` / ``assign_task`` so
    worker-driven events later route back to the same user thread.
    """
    coros = [_exec_one_tool(guild_id, tu, user_id) for tu in tool_uses]
    return list(await asyncio.gather(*coros))


async def _exec_one_tool(guild_id: str, tu, user_id: str | None = None) -> dict:
    """Execute a single tool call and return its tool_result block."""
    inp = tu.input
    result_text = ""
    is_error = False
    try:
        db = await get_db()
        try:
            if tu.name == "create_task":
                name = (inp.get("name") or "")[:80]
                desc = inp.get("description", name)
                phase = inp.get("phase", "execute")
                task_id = "t-" + "".join(
                    random.choices(string.ascii_lowercase + string.digits, k=6)
                )
                created_at = datetime.now(UTC).isoformat()
                db.add(
                    Task(
                        id=task_id,
                        worker_id="foreman",
                        guild_id=guild_id,
                        name=name,
                        description=desc,
                        tool="claude",
                        state="pending",
                        phase=phase,
                        created_at=created_at,
                        user_id=user_id,
                    )
                )
                await db.commit()
                await broadcast(
                    guild_id,
                    {
                        "type": "task-created",
                        "taskId": task_id,
                        "name": name,
                        "description": desc,
                        "phase": phase,
                        "state": "pending",
                        "createdAt": created_at,
                    },
                )
                result_text = (
                    f"Task {task_id} created: '{name}'. Reference this task_id in assign_task."
                )

            elif tu.name == "assign_task":
                wid = inp["worker_id"]
                desc = inp.get("description", "")
                phase = inp.get("phase", "execute")
                tool = inp.get("tool", "claude")
                existing_task_id = inp.get("task_id")
                worker_result = await db.execute(
                    select(Worker.id).where(Worker.id == wid, Worker.guild_id == guild_id)
                )
                worker_row = worker_result.scalar_one_or_none()
                if not worker_row:
                    result_text = f"Worker {wid} not found — task NOT queued."
                elif existing_task_id:
                    name_override = inp.get("name")
                    update_values: dict = {
                        "worker_id": wid,
                        "description": desc,
                        "tool": tool,
                        "phase": phase,
                        "state": "pending",
                    }
                    if name_override:
                        update_values["name"] = name_override
                    if inp.get("issue_number") is not None:
                        update_values["issue_number"] = inp["issue_number"]
                    if inp.get("issue_repo"):
                        update_values["issue_repo"] = inp["issue_repo"]
                    await db.execute(
                        update(Task)
                        .where(Task.id == existing_task_id, Task.guild_id == guild_id)
                        .values(**update_values)
                    )
                    await db.commit()
                    name_result = await db.execute(
                        select(Task.name).where(Task.id == existing_task_id)
                    )
                    task_name = name_result.scalar_one_or_none() or desc[:60]
                    task_id = existing_task_id
                    await broadcast(
                        guild_id,
                        {
                            "type": "task-assigned",
                            "workerId": wid,
                            "taskId": task_id,
                            "name": task_name,
                            "description": desc,
                            "tool": tool,
                            "phase": phase,
                            "issueNumber": inp.get("issue_number"),
                            "issueRepo": inp.get("issue_repo"),
                        },
                    )
                    result_text = f"Task {task_id} assigned to {wid}."
                else:
                    name = inp.get("name") or desc[:60]
                    parent_task_id = inp.get("parent_task_id")
                    task_id = "t-" + "".join(
                        random.choices(string.ascii_lowercase + string.digits, k=6)
                    )
                    created_at = datetime.now(UTC).isoformat()
                    db.add(
                        Task(
                            id=task_id,
                            worker_id=wid,
                            guild_id=guild_id,
                            name=name,
                            description=desc,
                            tool=tool,
                            issue_number=inp.get("issue_number"),
                            issue_repo=inp.get("issue_repo"),
                            state="pending",
                            phase=phase,
                            parent_task_id=parent_task_id,
                            created_at=created_at,
                            user_id=user_id,
                        )
                    )
                    await db.commit()
                    await broadcast(
                        guild_id,
                        {
                            "type": "task-assigned",
                            "workerId": wid,
                            "taskId": task_id,
                            "name": name,
                            "description": desc,
                            "tool": tool,
                            "phase": phase,
                            "parentTaskId": parent_task_id,
                            "issueNumber": inp.get("issue_number"),
                            "issueRepo": inp.get("issue_repo"),
                        },
                    )
                    result_text = f"Task {task_id} queued for {wid}."

            elif tu.name == "send_followup":
                task_id = inp["task_id"]
                instructions = inp["instructions"]
                result = await db.execute(
                    select(Task.worker_id).where(Task.id == task_id, Task.guild_id == guild_id)
                )
                worker_id_val = result.scalar_one_or_none()
                if not worker_id_val:
                    result_text = f"Task {task_id} not found."
                else:
                    await db.execute(
                        update(Task)
                        .where(Task.id == task_id)
                        .values(state="working", phase="followup")
                    )
                    await db.commit()
                    await broadcast(
                        guild_id,
                        {
                            "type": "task-followup",
                            "workerId": worker_id_val,
                            "taskId": task_id,
                            "instructions": instructions,
                        },
                    )
                    result_text = f"Follow-up sent to {worker_id_val} for task {task_id}."

            elif tu.name == "finalize_task":
                task_id = inp["task_id"]
                deleted_at, err = _resolve_finalize_deleted_at(inp)
                if err:
                    result_text = err
                    is_error = True
                else:
                    result = await db.execute(
                        select(Task.worker_id).where(Task.id == task_id, Task.guild_id == guild_id)
                    )
                    worker_id_val = result.scalar_one_or_none()
                    if not worker_id_val:
                        result_text = f"Task {task_id} not found."
                    else:
                        finished_at = datetime.now(UTC).isoformat()
                        await db.execute(
                            update(Task)
                            .where(Task.id == task_id)
                            .values(
                                state="done",
                                finished_at=finished_at,
                                deleted_at=deleted_at,
                            )
                        )
                        await db.commit()
                        await broadcast(
                            guild_id,
                            {
                                "type": "task-finalize",
                                "workerId": worker_id_val,
                                "taskId": task_id,
                            },
                        )
                        await broadcast(
                            guild_id,
                            {
                                "type": "task-update",
                                "taskId": task_id,
                                "state": "done",
                                "finishedAt": finished_at,
                                "deletedAt": deleted_at,
                            },
                        )
                        result_text = f"Task {task_id} finalized; soft-delete at {deleted_at}."

            elif tu.name == "message_worker":
                wid = inp["worker_id"]
                msg = inp["message"]
                await emit_terminal_line(guild_id, wid, f"[foreman] {msg}")
                await broadcast(
                    guild_id,
                    {
                        "type": "worker-message",
                        "workerId": wid,
                        "message": msg,
                    },
                )
                result_text = f"Message delivered to {wid}."

            elif tu.name == "redirect_task":
                task_id = inp["task_id"]
                instructions = inp["instructions"]
                result = await db.execute(
                    select(Task.worker_id, Task.state).where(
                        Task.id == task_id, Task.guild_id == guild_id
                    )
                )
                row = result.one_or_none()
                if not row:
                    result_text = f"Task {task_id} not found."
                else:
                    worker_id_val, state = row
                    if state in ("done", "failed", "cancelled"):
                        result_text = f"Task {task_id} is {state} — cannot redirect."
                    else:
                        await db.execute(
                            update(Task).where(Task.id == task_id).values(state="working")
                        )
                        await db.commit()
                        await broadcast(
                            guild_id,
                            {
                                "type": "task-redirect",
                                "workerId": worker_id_val,
                                "taskId": task_id,
                                "instructions": instructions,
                            },
                        )
                        await broadcast(
                            guild_id,
                            {
                                "type": "task-update",
                                "taskId": task_id,
                                "state": "working",
                            },
                        )
                        result_text = f"Redirect sent to {worker_id_val} for task {task_id}."

            elif tu.name == "cancel_task":
                task_id = inp["task_id"]
                reason = inp.get("reason", "")
                result = await db.execute(
                    select(Task.worker_id, Task.state).where(
                        Task.id == task_id, Task.guild_id == guild_id
                    )
                )
                row = result.one_or_none()
                if not row:
                    result_text = f"Task {task_id} not found."
                else:
                    worker_id_val, state = row
                    if state in ("done", "failed", "cancelled"):
                        result_text = f"Task {task_id} is already {state}."
                    else:
                        finished_at = datetime.now(UTC).isoformat()
                        await db.execute(
                            update(Task)
                            .where(Task.id == task_id)
                            .values(state="cancelled", finished_at=finished_at)
                        )
                        await db.commit()
                        await broadcast(
                            guild_id,
                            {
                                "type": "task-cancel",
                                "workerId": worker_id_val,
                                "taskId": task_id,
                            },
                        )
                        await broadcast(
                            guild_id,
                            {
                                "type": "task-update",
                                "taskId": task_id,
                                "state": "cancelled",
                                "finishedAt": finished_at,
                            },
                        )
                        result_text = f"Task {task_id} cancelled." + (
                            f" Reason: {reason}" if reason else ""
                        )

            elif tu.name == "shutdown_worker":
                wid = inp["worker_id"]
                reason = inp.get("reason", "")
                worker_result = await db.execute(
                    select(Worker.id).where(Worker.id == wid, Worker.guild_id == guild_id)
                )
                if worker_result.scalar_one_or_none() is None:
                    result_text = f"Worker {wid} not found."
                else:
                    message: dict = {"type": "worker-shutdown", "workerId": wid}
                    if reason:
                        message["reason"] = reason
                    await broadcast(guild_id, message)
                    result_text = f"Shutdown signal sent to {wid}." + (
                        f" Reason: {reason}" if reason else ""
                    )

            elif tu.name == "get_task_status":
                task_id = inp["task_id"]
                limit = min(int(inp.get("log_lines", 10)), 50)
                task_result = await db.execute(
                    select(Task).where(Task.id == task_id, Task.guild_id == guild_id)
                )
                task = task_result.scalar_one_or_none()
                if not task:
                    result_text = f"Task {task_id} not found."
                else:
                    agent_info = None
                    if task.worker_id and task.worker_id != "foreman":
                        agent_result = await db.execute(
                            select(Agent.id, Agent.state)
                            .where(Agent.worker_id == task.worker_id, Agent.state != "offline")
                            .limit(1)
                        )
                        agent_row = agent_result.one_or_none()
                        if agent_row:
                            agent_info = {"agent_id": agent_row[0], "agent_state": agent_row[1]}
                    logs_result = await db.execute(
                        select(TaskLog.timestamp, TaskLog.line)
                        .where(TaskLog.task_id == task_id)
                        .order_by(TaskLog.id.desc())
                        .limit(limit)
                    )
                    log_rows = list(reversed(logs_result.fetchall()))
                    result_text = json.dumps(
                        {
                            "id": task.id,
                            "name": task.name,
                            "state": task.state,
                            "phase": task.phase,
                            "worker_id": task.worker_id,
                            "agent": agent_info,
                            "branch": task.branch,
                            "pr_url": task.pr_url,
                            "created_at": task.created_at,
                            "finished_at": task.finished_at,
                            "recent_logs": [{"time": r[0], "line": r[1]} for r in log_rows],
                        }
                    )
        finally:
            await db.close()

        # GitHub tools — use guild's OAuth token
        if tu.name in (
            "list_github_issues",
            "get_github_issue",
            "list_github_prs",
            "claim_github_issue",
            "create_github_issue",
            "search_github_issues",
        ):
            creds = await _guild_github_token(guild_id)
            if not creds:
                result_text = (
                    "No GitHub token found for this guild — user must connect GitHub first."
                )
                is_error = True
            else:
                token, username = creds
                try:
                    if tu.name == "list_github_issues":
                        repo = inp["repo"]
                        state = inp.get("state", "open")
                        limit = min(int(inp.get("limit", 20)), 50)
                        issues = await asyncio.to_thread(
                            _gh_api,
                            f"/repos/{repo}/issues?state={state}&per_page={limit}",
                            token,
                        )
                        trimmed = [
                            {
                                "number": i["number"],
                                "title": i["title"],
                                "state": i["state"],
                                "labels": [l["name"] for l in i.get("labels", [])],
                                "assignees": [a["login"] for a in i.get("assignees", [])],
                                "created_at": i["created_at"],
                            }
                            for i in issues
                            if "pull_request" not in i
                        ]
                        result_text = json.dumps(trimmed)

                    elif tu.name == "get_github_issue":
                        repo = inp["repo"]
                        num = int(inp["issue_number"])
                        issue = await asyncio.to_thread(
                            _gh_api, f"/repos/{repo}/issues/{num}", token
                        )
                        comments_raw = await asyncio.to_thread(
                            _gh_api, f"/repos/{repo}/issues/{num}/comments?per_page=20", token
                        )
                        result_text = json.dumps(
                            {
                                "number": issue["number"],
                                "title": issue["title"],
                                "state": issue["state"],
                                "body": (issue.get("body") or "")[:2000],
                                "labels": [l["name"] for l in issue.get("labels", [])],
                                "comments": [
                                    {
                                        "author": c["user"]["login"],
                                        "body": (c.get("body") or "")[:500],
                                    }
                                    for c in comments_raw
                                ],
                            }
                        )

                    elif tu.name == "list_github_prs":
                        repo = inp["repo"]
                        state = inp.get("state", "open")
                        prs = await asyncio.to_thread(
                            _gh_api, f"/repos/{repo}/pulls?state={state}&per_page=20", token
                        )
                        result_text = json.dumps(
                            [
                                {
                                    "number": p["number"],
                                    "title": p["title"],
                                    "state": p["state"],
                                    "head": p["head"]["ref"],
                                    "draft": p.get("draft", False),
                                }
                                for p in prs
                            ]
                        )

                    elif tu.name == "claim_github_issue":
                        repo = inp["repo"]
                        num = int(inp["issue_number"])
                        await asyncio.to_thread(
                            _gh_api_post,
                            f"/repos/{repo}/issues/{num}/assignees",
                            token,
                            {"assignees": [username]},
                        )
                        result_text = f"Issue #{num} in {repo} assigned to {username}."

                    elif tu.name == "create_github_issue":
                        repo = inp["repo"]
                        payload: dict = {"title": inp["title"], "body": inp.get("body", "")}
                        if inp.get("labels"):
                            payload["labels"] = inp["labels"]
                        issue = await asyncio.to_thread(
                            _gh_api_post, f"/repos/{repo}/issues", token, payload
                        )
                        result_text = json.dumps(
                            {
                                "number": issue["number"],
                                "url": issue["html_url"],
                                "title": issue["title"],
                            }
                        )

                    elif tu.name == "search_github_issues":
                        repo = inp["repo"]
                        query = inp["query"]
                        state = inp.get("state", "open")
                        state_q = "" if state == "all" else f"+state:{state}"
                        search_url = (
                            f"/search/issues?q={urllib.parse.quote(query)}"
                            f"+repo:{repo}{state_q}&per_page=10&sort=created&order=desc"
                        )
                        data = await asyncio.to_thread(_gh_api, search_url, token)
                        items = data.get("items", []) if isinstance(data, dict) else data
                        result_text = json.dumps(
                            [
                                {
                                    "number": i["number"],
                                    "title": i["title"],
                                    "state": i["state"],
                                    "url": i["html_url"],
                                    "labels": [l["name"] for l in i.get("labels", [])],
                                }
                                for i in items
                            ]
                        )

                except urllib.error.HTTPError as exc:
                    result_text = f"GitHub API error: {exc.code} {exc.reason}"
                    is_error = True
                except Exception as exc:
                    result_text = f"GitHub error: {exc}"
                    is_error = True

    except Exception as exc:
        result_text = f"Tool {tu.name} failed: {exc}"
        is_error = True

    block: dict = {"type": "tool_result", "tool_use_id": tu.id, "content": result_text}
    if is_error:
        block["is_error"] = True
    return block
