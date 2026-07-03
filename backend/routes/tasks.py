"""Task lifecycle routes: list, logs, follow-up, finalize, cancel, redirect.

Soft-delete TTL handling lives here too — ``DEFAULT_FINALIZE_TTL``,
``FinalizeBody``, and ``_resolve_finalize_deleted_at`` are exported for
direct unit testing in ``tests/test_soft_delete_tasks.py``.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from time import time as _monotime

from auth_deps import get_guild_pk, require_member
from database import get_db_dep
from events import broadcast_msg
from fastapi import APIRouter, Depends, HTTPException
from lock_service import LockService
from models import GithubToken, Guild, GuildMember, Task, TaskLog, live_tasks_filter
from pydantic import BaseModel
from sqlalchemy import update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from util.tasks import spawn
from ws_types import TaskCancelMsg, TaskFinalizeMsg, TaskRedirectMsg, TaskUpdateMsg

from foreman import run_foreman_ai

router = APIRouter()


# Default soft-delete window for finalized tasks when the caller does not
# specify one. 3 days lets the operator review recent work in the UI before
# it disappears.
DEFAULT_FINALIZE_TTL = timedelta(days=3)


class FollowupCreate(BaseModel):
    instructions: str


class FinalizeBody(BaseModel):
    # Optional ISO-8601 timestamp at which to soft-delete this task.
    deleted_at: str | None = None
    # Optional convenience: seconds from now until soft-delete. If both fields
    # are set, deleted_at wins.
    expires_in_seconds: int | None = None


class RedirectCreate(BaseModel):
    instructions: str


def _resolve_finalize_deleted_at(body: FinalizeBody | None) -> datetime:
    """Return a UTC datetime for the task's soft-delete instant."""
    if body and body.deleted_at:
        try:
            parsed = datetime.fromisoformat(body.deleted_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid deleted_at: {exc}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    if body and body.expires_in_seconds is not None:
        if body.expires_in_seconds < 0:
            raise HTTPException(status_code=400, detail="expires_in_seconds must be >= 0")
        return datetime.now(UTC) + timedelta(seconds=body.expires_in_seconds)
    return datetime.now(UTC) + DEFAULT_FINALIZE_TTL


@router.get("/guilds/{guild_id}/tasks")
async def list_guild_tasks(
    guild_id: str,
    issue_number: int | None = None,
    issue_repo: str | None = None,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """List all tasks for a guild, most recent first.

    Optionally filter by ``issue_number`` and/or ``issue_repo`` to return only
    tasks linked to a specific GitHub issue.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    stmt = (
        select(
            col(Task.id),
            col(Task.worker_id),
            col(Task.name),
            col(Task.description),
            col(Task.tool),
            col(Task.state),
            col(Task.phase),
            col(Task.parent_task_id),
            col(Task.branch),
            col(Task.pr_url),
            col(Task.issue_number),
            col(Task.issue_repo),
            col(Task.created_at),
            col(Task.deleted_at),
        )
        .where(col(Task.guild_id) == guild_pk, live_tasks_filter())
        .order_by(col(Task.created_at).desc())
    )
    if issue_number is not None:
        stmt = stmt.where(col(Task.issue_number) == issue_number)
    if issue_repo is not None:
        stmt = stmt.where(col(Task.issue_repo) == issue_repo)
    stmt = stmt.limit(100)
    result = await db.exec(stmt)
    return [dict(r._mapping) for r in result.all()]


@router.get("/guilds/{guild_id}/tasks/{task_id}/logs")
async def get_task_logs(
    guild_id: str,
    task_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Get all saved log lines for a task."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(col(Task.id)).where(
            col(Task.id) == task_id,
            col(Task.guild_id) == guild_pk,
            live_tasks_filter(),
        )
    )
    if not result.one_or_none():
        raise HTTPException(status_code=404, detail="Task not found")
    result = await db.exec(
        select(
            col(TaskLog.timestamp),
            col(TaskLog.line),
            col(TaskLog.worker_id),
            col(TaskLog.agent_id),
            col(TaskLog.data),
            col(TaskLog.level),
        )
        .where(col(TaskLog.task_id) == task_id)
        .order_by(col(TaskLog.id).asc())
    )
    rows = []
    for r in result.all():
        row = dict(r._mapping)
        raw = row.pop("data", None)
        if raw:
            try:
                row["detail"] = json.loads(raw)
            except Exception:
                pass
        rows.append(row)
    return rows


@router.get("/guilds/{guild_id}/logs")
async def get_guild_logs(
    guild_id: str,
    worker_id: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Get task_logs filtered by worker_id, agent_id, or task_id."""
    if not (worker_id or agent_id or task_id):
        raise HTTPException(status_code=400, detail="Specify worker_id, agent_id, or task_id")
    result = await db.exec(select(col(Guild.slug)).where(col(Guild.slug) == guild_id))
    if not result.one_or_none():
        raise HTTPException(status_code=404, detail="Guild not found")
    stmt = select(
        col(TaskLog.timestamp),
        col(TaskLog.line),
        col(TaskLog.worker_id),
        col(TaskLog.agent_id),
        col(TaskLog.task_id),
        col(TaskLog.data),
        col(TaskLog.level),
    )
    if task_id:
        stmt = stmt.where(col(TaskLog.task_id) == task_id)
    elif worker_id:
        stmt = stmt.where(col(TaskLog.worker_id) == worker_id, col(TaskLog.task_id).is_(None))
    else:
        stmt = stmt.where(col(TaskLog.agent_id) == agent_id)
    stmt = stmt.order_by(col(TaskLog.id).asc())
    result = await db.exec(stmt)
    rows = []
    for r in result.all():
        row = dict(r._mapping)
        raw = row.pop("data", None)
        if raw:
            try:
                row["detail"] = json.loads(raw)
            except Exception:
                pass
        rows.append(row)
    return rows


@router.post("/guilds/{guild_id}/tasks/{task_id}/followup")
async def create_task_followup(
    guild_id: str,
    task_id: str,
    data: FollowupCreate,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Route a user-supplied follow-up to the foreman.

    Workers no longer hold tasks open for follow-ups — the foreman owns task
    lifecycle and decides which idle worker resumes the branch (the original
    worker reuses its worktree if it's still idle; otherwise any idle worker
    pulls the branch from GitHub). All follow-ups go through the foreman so
    the same routing logic applies in every case.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(col(Task.worker_id), col(Task.state), col(Task.branch)).where(
            col(Task.id) == task_id, col(Task.guild_id) == guild_pk
        )
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    _worker_id, state, branch = row

    branch_ctx = f" on branch `{branch}`" if branch else ""
    spawn(
        run_foreman_ai(
            guild_id,
            f"[user-followup] User requested follow-up on task {task_id}{branch_ctx} "
            f'(currently {state}): "{data.instructions}". '
            "Call send_followup to dispatch this work — it will pick the "
            "original worker if idle, otherwise any idle worker pulls the "
            "branch from GitHub.",
            user_id=github_user_id,
            task_id=task_id,
            child=True,
        ),
        name=f"foreman.user-followup:{task_id}",
    )
    return {"status": "queued_for_foreman", "taskId": task_id}


@router.post("/guilds/{guild_id}/tasks/{task_id}/finalize")
async def finalize_task_endpoint(
    guild_id: str,
    task_id: str,
    body: FinalizeBody | None = None,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Signal a worker to finalize a task — no more follow-ups.

    The optional body may carry ``deleted_at`` (ISO-8601) or
    ``expires_in_seconds`` to set the task's soft-delete window. If neither is
    set, the task is soft-deleted ``DEFAULT_FINALIZE_TTL`` from now.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(col(Task.worker_id)).where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
    )
    worker_id = result.one_or_none()
    if not worker_id:
        raise HTTPException(status_code=404, detail="Task not found")
    deleted_at = _resolve_finalize_deleted_at(body)
    await db.exec(
        update(Task).where(col(Task.id) == task_id).values(state="done", deleted_at=deleted_at)
    )
    await db.commit()
    await broadcast_msg(guild_id, TaskFinalizeMsg(workerId=worker_id, taskId=task_id))
    await broadcast_msg(
        guild_id,
        TaskUpdateMsg(
            taskId=task_id,
            state="done",
            deletedAt=deleted_at.isoformat(),
        ),
    )
    # Return raw datetime — FastAPI's jsonable_encoder handles ISO-8601 serialisation.
    return {"status": "finalized", "taskId": task_id, "deletedAt": deleted_at}


@router.post("/guilds/{guild_id}/tasks/{task_id}/cancel")
async def cancel_task_endpoint(
    guild_id: str,
    task_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Cancel a running or pending task — terminates the worker's Claude subprocess."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(col(Task.worker_id), col(Task.state)).where(
            col(Task.id) == task_id, col(Task.guild_id) == guild_pk
        )
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    worker_id, state = row
    if state in ("done", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Task is already {state}")
    deleted_at = datetime.now(UTC) + DEFAULT_FINALIZE_TTL
    await db.exec(
        update(Task).where(col(Task.id) == task_id).values(state="cancelled", deleted_at=deleted_at)
    )
    await LockService(db).release(f"task:{task_id}")
    await db.commit()
    await broadcast_msg(guild_id, TaskCancelMsg(workerId=worker_id, taskId=task_id))
    await broadcast_msg(
        guild_id,
        TaskUpdateMsg(taskId=task_id, state="cancelled", deletedAt=deleted_at.isoformat()),
    )
    return {"status": "cancelled", "taskId": task_id}


# ---------------------------------------------------------------------------
# Task tree endpoint helpers
# ---------------------------------------------------------------------------

_CLOSES_RE = re.compile(r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE)
_GH_PR_URL_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")

# Simple in-process TTL caches (key → (value, expires_at))
_TREE_CACHE_TTL = 600.0  # 10 minutes
_gh_issue_cache: dict[str, tuple[dict, float]] = {}
_gh_pr_body_cache: dict[str, tuple[str | None, float]] = {}


_CACHE_MAX_SIZE = 1000


def _gh_fetch_issue(repo: str, number: int, token: str) -> dict | None:
    """Fetch GitHub issue title/state. Cached for _TREE_CACHE_TTL seconds."""
    key = f"{repo}#{number}"
    cached = _gh_issue_cache.get(key)
    if cached and cached[1] > _monotime():
        return cached[0]
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues/{number}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "pioneer-square",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        result: dict = {
            "title": data.get("title") or f"#{number}",
            "state": data.get("state") or "open",
        }
        if len(_gh_issue_cache) >= _CACHE_MAX_SIZE:
            _gh_issue_cache.clear()
        _gh_issue_cache[key] = (result, _monotime() + _TREE_CACHE_TTL)
        return result
    except Exception:
        return None


def _gh_fetch_pr_body(repo: str, pr_number: int, token: str) -> str | None:
    """Fetch a GitHub PR body for closes-reference extraction. Cached."""
    key = f"{repo}#pr{pr_number}"
    cached = _gh_pr_body_cache.get(key)
    if cached and cached[1] > _monotime():
        return cached[0]
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "pioneer-square",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        body: str | None = data.get("body") or ""
        if len(_gh_pr_body_cache) >= _CACHE_MAX_SIZE:
            _gh_pr_body_cache.clear()
        _gh_pr_body_cache[key] = (body, _monotime() + _TREE_CACHE_TTL)
        return body
    except Exception:
        if len(_gh_pr_body_cache) >= _CACHE_MAX_SIZE:
            _gh_pr_body_cache.clear()
        _gh_pr_body_cache[key] = (None, _monotime() + _TREE_CACHE_TTL)
        return None


def _nest_tasks(tasks: list[dict]) -> list[dict]:
    """Build a parent/child tree from a flat task list using parent_task_id."""
    by_id = {t["id"]: dict(t, children=[]) for t in tasks}
    roots: list[dict] = []
    for t in by_id.values():
        parent_id = t.get("parent_task_id")
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(t)
        else:
            roots.append(t)
    return roots


@router.get("/guilds/{guild_id}/tasks/tree")
async def get_guild_tasks_tree(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Return tasks grouped hierarchically under their GitHub issues/PRs.

    Top-level nodes are GitHub issues (identified by issue_number+issue_repo on
    the task).  Review tasks whose PR body contains a ``closes #N`` reference are
    also parented to that issue.  Tasks with parent_task_id are nested under their
    parent task within the group.  Remaining tasks go in ``ungrouped``.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    result = await db.exec(
        select(
            col(Task.id),
            col(Task.worker_id),
            col(Task.name),
            col(Task.description),
            col(Task.phase),
            col(Task.state),
            col(Task.parent_task_id),
            col(Task.branch),
            col(Task.pr_url),
            col(Task.pr_number),
            col(Task.pr_repo),
            col(Task.issue_number),
            col(Task.issue_repo),
            col(Task.issue_state),
            col(Task.issue_title),
            col(Task.created_at),
            col(Task.deleted_at),
        )
        .where(col(Task.guild_id) == guild_pk, live_tasks_filter())
        .order_by(col(Task.created_at).asc())
        .limit(200)
    )
    raw_tasks = [dict(r._mapping) for r in result.all()]

    # Fetch GitHub token from the guild owner only to avoid privilege escalation —
    # a regular member's personal token must not be used to access guild resources.
    token_row = await db.exec(
        select(col(GithubToken.access_token))
        .join(GuildMember, col(GuildMember.user_id) == col(GithubToken.github_user_id))
        .where(col(GuildMember.guild_id) == guild_pk, col(GuildMember.role) == "owner")
        .limit(1)
    )
    gh_token: str | None = row[0] if (row := token_row.one_or_none()) else None

    # --- Build task_id → (issue_repo, issue_number) mapping ---

    task_issue: dict[str, tuple[str, int]] = {}

    # Pass 1: tasks with explicit issue linkage
    pr_tasks: list[tuple[str, str, int]] = []
    for task in raw_tasks:
        if task["issue_number"] and task["issue_repo"]:
            task_issue[task["id"]] = (task["issue_repo"], task["issue_number"])
        elif task["pr_url"] and gh_token:
            # Use stored pr_repo/pr_number when available; fall back to URL parse
            if task["pr_repo"] and task["pr_number"]:
                pr_tasks.append((task["id"], task["pr_repo"], task["pr_number"]))
            else:
                m = _GH_PR_URL_RE.match(task["pr_url"])
                if m:
                    pr_tasks.append((task["id"], m.group(1), int(m.group(2))))

    # Pass 2: resolve PR bodies to find closing issue references
    if pr_tasks and gh_token:
        bodies = await asyncio.gather(
            *[
                asyncio.to_thread(_gh_fetch_pr_body, repo, pr_num, gh_token)
                for _, repo, pr_num in pr_tasks
            ]
        )
        for (task_id, repo, _), body in zip(pr_tasks, bodies, strict=False):
            if body:
                m = _CLOSES_RE.search(body)
                if m:
                    task_issue[task_id] = (repo, int(m.group(1)))

    # Pass 3: propagate issue assignment through parent_task_id chains
    changed = True
    max_iters = len(raw_tasks)
    iters = 0
    while changed and iters < max_iters:
        changed = False
        iters += 1
        for task in raw_tasks:
            if task["id"] in task_issue:
                continue
            pid = task.get("parent_task_id")
            if pid and pid in task_issue:
                task_issue[task["id"]] = task_issue[pid]
                changed = True

    # --- Group tasks ---
    issue_groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    ungrouped: list[dict] = []
    for task in raw_tasks:
        key = task_issue.get(task["id"])
        if key:
            issue_groups[key].append(task)
        else:
            ungrouped.append(task)

    # --- Build DB-cached issue state/title fallback ---
    # Each task in an issue group stores the last-known GitHub issue state and
    # title so we can return real values even when the GitHub API is unreachable
    # or the guild owner has no token.  Any non-null value in the group is authoritative.
    db_issue_states: dict[tuple[str, int], str] = {}
    db_issue_titles: dict[tuple[str, int], str] = {}
    for key, group_tasks in issue_groups.items():
        for task in group_tasks:
            if task.get("issue_state") and key not in db_issue_states:
                db_issue_states[key] = task["issue_state"]
            if task.get("issue_title") and key not in db_issue_titles:
                db_issue_titles[key] = task["issue_title"]
            if key in db_issue_states and key in db_issue_titles:
                break

    # --- Fetch issue metadata in parallel ---
    issue_info: dict[tuple[str, int], dict] = {}
    # Track which keys were resolved via the GitHub API so we can write them back.
    api_resolved: dict[tuple[str, int], dict] = {}
    if issue_groups and gh_token:
        keys = list(issue_groups.keys())
        infos = await asyncio.gather(
            *[asyncio.to_thread(_gh_fetch_issue, repo, num, gh_token) for repo, num in keys]
        )
        for key, info in zip(keys, infos, strict=False):
            if info:
                issue_info[key] = info
                api_resolved[key] = info
            else:
                db_state = db_issue_states.get(key, "open")
                db_title = db_issue_titles.get(key, f"#{key[1]}")
                issue_info[key] = {"title": db_title, "state": db_state}
    else:
        for key in issue_groups:
            db_state = db_issue_states.get(key, "open")
            db_title = db_issue_titles.get(key, f"#{key[1]}")
            issue_info[key] = {"title": db_title, "state": db_state}

    # Write back state+title fetched from GitHub API to the tasks table so future
    # requests can fall back to the DB when the API is unavailable.
    if api_resolved:
        tasks_to_update: list[tuple[str, str, str]] = []  # (task_id, state, title)
        for key, info in api_resolved.items():
            state = info["state"]
            title = info["title"]
            for task in issue_groups[key]:
                if task.get("issue_state") != state or task.get("issue_title") != title:
                    tasks_to_update.append((task["id"], state, title))
        if tasks_to_update:
            # Group by (state, title) to minimize UPDATE statements
            updates_by_values: dict[tuple[str, str], list[str]] = {}
            for task_id, state, title in tasks_to_update:
                updates_by_values.setdefault((state, title), []).append(task_id)
            for (state, title), task_ids in updates_by_values.items():
                await db.exec(
                    update(Task)
                    .where(col(Task.id).in_(task_ids))
                    .values(issue_state=state, issue_title=title)
                )
            await db.commit()

    # --- Build response ---
    nodes = []
    for (repo, num), group_tasks in issue_groups.items():
        info = issue_info.get(
            (repo, num),
            {
                "title": db_issue_titles.get((repo, num), f"#{num}"),
                "state": db_issue_states.get((repo, num), "open"),
            },
        )
        nodes.append(
            {
                "type": "issue",
                "issue_number": num,
                "issue_repo": repo,
                "title": info["title"],
                "state": info["state"],
                "tasks": _nest_tasks(group_tasks),
            }
        )

    # Sort: open issues first, then by issue number descending (newest first)
    nodes.sort(key=lambda n: (0 if n["state"] == "open" else 1, -n["issue_number"]))

    return {"nodes": nodes, "ungrouped": _nest_tasks(ungrouped)}


@router.post("/guilds/{guild_id}/tasks/{task_id}/redirect")
async def redirect_task_endpoint(
    guild_id: str,
    task_id: str,
    data: RedirectCreate,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Redirect a running task: SIGTERM the Claude subprocess and resume it with new instructions."""
    instructions = data.instructions.strip()
    if not instructions:
        raise HTTPException(status_code=400, detail="instructions required")
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(col(Task.worker_id), col(Task.state)).where(
            col(Task.id) == task_id, col(Task.guild_id) == guild_pk
        )
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    worker_id, state = row
    if state in ("done", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Task is already {state}")
    await db.exec(update(Task).where(col(Task.id) == task_id).values(state="working"))
    await db.commit()
    await broadcast_msg(
        guild_id, TaskRedirectMsg(workerId=worker_id, taskId=task_id, instructions=instructions)
    )
    await broadcast_msg(guild_id, TaskUpdateMsg(taskId=task_id, state="working"))
    return {"status": "redirected", "taskId": task_id}
