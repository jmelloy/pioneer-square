"""Foreman tool definitions (embedded), GitHub API helpers, and tool-call executor.

FOREMAN_TOOLS is imported from foreman.tools_schema — the single source of truth
for the schema sent to the LLM or API proxy. This module owns backend-side tool
execution and all GitHub/DB helpers.
"""

import asyncio
import contextvars
import json
import logging
import math
import os
import random
import re
import secrets
import string
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

import discord_notifier
import worker_runtime
from database import get_db
from db import github_cache
from events import broadcast, broadcast_msg, emit_terminal_line
from foreman.constants import _TERMINAL_STATES
from foreman.message_utils import _json_default, truncate_tool_result
from foreman.tools_schema import (
    FOREMAN_TOOLS,  # noqa: F401 — re-exported for test compatibility
)
from github_app_auth import get_app_installation_token, get_app_slug
from lock_service import LockService
from models import (
    Agent,
    GithubToken,
    Guild,
    GuildKey,
    GuildMember,
    Task,
    TaskEvent,
    TaskLog,
    User,
    Worker,
    finalize_soft_delete_at,
)
from sqlalchemy import delete, literal, update
from sqlmodel import col, select
from util.tasks import spawn
from utils import build_spawn_worker_env, worker_display_name
from ws_types import (
    TaskAssignedMsg,
    TaskCancelMsg,
    TaskCreatedMsg,
    TaskFinalizeMsg,
    TaskFollowupMsg,
    TaskRedirectMsg,
    TaskUpdateMsg,
    WorkerMessageMsg,
    WorkerShutdownMsg,
    WorkerStateMsg,
)

logger = logging.getLogger(__name__)

# Hard cap on any single blocking external-service call (GitHub API, Docker, A2A
# agent).  Wrapping asyncio.to_thread with asyncio.wait_for(timeout=...) means a
# hung upstream can stall at most this many seconds instead of freezing the whole
# event loop indefinitely.
EXTERNAL_CALL_TIMEOUT = 30

# Per-tool-call collector for GitHub API response metadata (request IDs, status codes).
# Each _exec_one_tool invocation sets a fresh list via _api_calls_ctx.set([]) using the
# token-reset pattern so concurrent coroutines never share a list.  asyncio.to_thread
# copies the context so thread-pool calls can also append to the same list.
_api_calls_ctx: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "_api_calls_ctx", default=None
)


def _record_api_call(path: str, status: int, headers: Any) -> None:
    """Append one HTTP-call record to the per-tool-call collector (no-op if unset)."""
    calls = _api_calls_ctx.get(None)
    if calls is None:
        return
    entry: dict = {"path": path, "status": status, "ts": datetime.now(UTC).isoformat()}
    if headers:
        rid = headers.get("x-request-id")
        ghrid = headers.get("x-github-request-id")
        if rid:
            entry["x_request_id"] = rid
        if ghrid:
            entry["x_github_request_id"] = ghrid
    calls.append(entry)


async def _to_thread(fn, /, *args, **kwargs):
    """Run a blocking callable in a thread pool with a global timeout.

    WARNING: asyncio cancellation/timeout does NOT interrupt the underlying thread.
    If fn has side effects (e.g. spawning containers, open sockets), callers must
    handle cleanup in an except block — the thread will continue running to
    completion even after a TimeoutError is raised to the caller.
    """
    return await asyncio.wait_for(
        asyncio.to_thread(fn, *args, **kwargs),
        timeout=EXTERNAL_CALL_TIMEOUT,
    )


async def _guild_foreman_config(db, guild_pk: int | None) -> dict:
    """Read a guild's foreman_config via the in-scope session (not a fresh one,
    so it stays inside the caller's transaction and test fixtures apply)."""
    if guild_pk is None:
        return {}
    res = await db.exec(select(col(Guild.foreman_config)).where(col(Guild.id) == guild_pk))
    cfg = res.one_or_none()
    return cfg if isinstance(cfg, dict) else {}


def _apply_pi_defaults(
    tool: str | None,
    provider: str | None,
    model: str | None,
    guild_cfg: dict | None,
) -> tuple[str | None, str | None]:
    """Fill pi's provider/model from the guild's foreman config when a dispatch
    left them unset. Pi is provider-agnostic and has no built-in default that
    authenticates, so without this a pi task launches with no --provider/--model
    and fails per-task (see #1040). Only applies to the pi tool; an explicit
    provider/model from the foreman's tool call always wins.
    """
    if tool != "pi":
        return provider, model
    cfg = guild_cfg or {}
    if provider is None:
        provider = cfg.get("pi_default_provider") or None
    if model is None:
        model = cfg.get("pi_default_model") or None
    return provider, model


async def finalize_closed_issue(
    db, guild_pk: int, guild_id: str, issue_repo: str, issue_number: int
) -> list[str]:
    """Clean up tasks linked to a GitHub issue that just closed.

    Shared by the periodic closed-issue sweep (``foreman.runner._sweep_closed_issues``)
    and the ``issues`` webhook handler (``routes.webhooks.github_webhook``). Legacy
    ``phase='issue'`` root rows are finalized outright — a single conditional
    ``UPDATE ... RETURNING`` keeps the terminal-state guard TOCTOU-safe against a
    concurrent finalize. Every already-terminal task linked to the issue gets its
    soft-delete stamped; non-terminal linked tasks are never force-closed (a human
    decides whether to cancel in-flight work) and only logged. Posts one pre-close
    verification comment summarising linked-PR merge status when anything was
    finalized. Returns the ids of the tasks that were finalized or swept.
    """
    deleted_at = datetime.now(UTC)
    issue_filter = (
        col(Task.guild_id) == guild_pk,
        col(Task.issue_repo) == issue_repo,
        col(Task.issue_number) == issue_number,
    )

    # Legacy phase='issue' roots: synthetic rows owned by the issue itself.
    upd = await db.exec(
        update(Task)
        .where(
            *issue_filter, col(Task.phase) == "issue", ~col(Task.state).in_(list(_TERMINAL_STATES))
        )
        .values(state="done", deleted_at=deleted_at)
        .returning(col(Task.id), col(Task.worker_id))
    )
    roots = list(upd.all())
    for root_id, _ in roots:
        await LockService(db).release(f"task:{root_id}")
        await db.exec(delete(TaskEvent).where(col(TaskEvent.task_id) == root_id))

    linked_result = await db.exec(select(Task).where(*issue_filter))
    linked = list(linked_result.all())
    swept_ids = await cascade_soft_delete_terminal_descendants(db, linked, deleted_at)
    await db.commit()

    for root_id, worker_id in roots:
        await broadcast_msg(guild_id, TaskFinalizeMsg(workerId=worker_id, taskId=root_id))
        await broadcast_msg(
            guild_id,
            TaskUpdateMsg(taskId=root_id, state="done", deletedAt=deleted_at.isoformat()),
        )

    open_tasks = [t.id for t in linked if t.state not in _TERMINAL_STATES and t.phase != "issue"]
    if open_tasks:
        logger.warning(
            "guild=%s issue %s#%s closed but %d non-terminal linked task(s) remain "
            "open: %s — leaving them as-is",
            guild_id,
            issue_repo,
            issue_number,
            len(open_tasks),
            ", ".join(open_tasks),
        )

    finalized = [root_id for root_id, _ in roots] + swept_ids
    if finalized:
        await post_issue_close_summary_comment(guild_id, issue_repo, issue_number, linked)
    return finalized


# Guards find_descendant_tasks against an (expected-impossible) cyclic
# parent_task_id chain so a bad row can never spin the lookup forever. Mirrors
# discord_notifier._MAX_PARENT_CHAIN_DEPTH, which walks the same chain upward.
_MAX_DESCENDANT_CHAIN_DEPTH = 20


async def find_descendant_tasks(db, root_task_id: str) -> list[Task]:
    """Return every task reachable from *root_task_id* via the ``parent_task_id`` chain.

    Walks the tree downward (plan/execute/review/followup, and anything chained
    off of those) in a single recursive CTE rather than one round-trip per level.
    """
    anchor = select(col(Task.id).label("id"), literal(0).label("depth")).where(
        col(Task.parent_task_id) == root_task_id
    )
    chain = anchor.cte(name="descendant_tasks", recursive=True)
    recursive_step = (
        select(col(Task.id).label("id"), (chain.c.depth + 1).label("depth"))
        .join(chain, col(Task.parent_task_id) == chain.c.id)
        .where(chain.c.depth < _MAX_DESCENDANT_CHAIN_DEPTH - 1)
    )
    chain = chain.union_all(recursive_step)

    id_result = await db.exec(select(chain.c.id))
    ids = list(id_result.all())
    if not ids:
        return []
    task_result = await db.exec(select(Task).where(col(Task.id).in_(ids)))
    return list(task_result.all())


async def cascade_soft_delete_terminal_descendants(
    db, descendants: list[Task], deleted_at: datetime
) -> list[str]:
    """Soft-delete every already-terminal descendant task, leaving open ones alone.

    Only tasks already in a terminal state (done/failed/cancelled/error — see
    ``_TERMINAL_STATES``) and not yet soft-deleted are touched. In-progress or
    pending descendants are never force-closed here; a human decides whether to
    cancel in-flight work — ``finalize_closed_issue`` logs them instead). Returns
    the ids that were updated.
    """
    terminal_ids = [
        t.id for t in descendants if t.state in _TERMINAL_STATES and t.deleted_at is None
    ]
    if terminal_ids:
        await db.exec(
            update(Task).where(col(Task.id).in_(terminal_ids)).values(deleted_at=deleted_at)
        )
    return terminal_ids


async def post_issue_close_summary_comment(
    guild_id: str, issue_repo: str, issue_number: int, descendants: list[Task]
) -> None:
    """Post a pre-close verification comment summarising child-PR merge status.

    Fetches merge status for every descendant task with a PR attached, then posts
    one summary comment to the GitHub issue (e.g. "all 3 child PRs merged" or a
    list of the ones that aren't). Best effort: never raises — a GitHub API
    failure here must not block finalizing the root task.
    """
    try:
        creds = await _guild_github_token(guild_id)
        if not creds:
            logger.warning(
                "issue close summary: no GitHub token for guild=%s %s#%s",
                guild_id,
                issue_repo,
                issue_number,
            )
            return
        token, _ = creds

        with_pr = [t for t in descendants if t.pr_url and t.pr_repo and t.pr_number]
        lines: list[str] = []
        unmerged = 0
        for t in with_pr:
            try:
                status = await fetch_pr_status(t.pr_repo, t.pr_number, token)
                merged = bool(status.get("merged"))
            except Exception:
                merged = False
                logger.warning(
                    "issue close summary: failed to fetch PR status for task=%s",
                    t.id,
                    exc_info=True,
                )
            if not merged:
                unmerged += 1
            mark = "✅" if merged else "❌"
            lines.append(
                f"- {mark} task `{t.id}` — {t.pr_url} ({'merged' if merged else 'not merged'})"
            )

        if with_pr:
            header = (
                f"Closing issue — all {len(with_pr)} child PR(s) merged ✅"
                if unmerged == 0
                else f"Closing issue — {unmerged} of {len(with_pr)} child PR(s) not merged ⚠️"
            )
            body = header + "\n\n" + "\n".join(lines)
        else:
            body = "Closing issue — no child PRs to verify."

        await _to_thread(
            _gh_api_post,
            f"/repos/{issue_repo}/issues/{issue_number}/comments",
            token,
            {"body": body},
        )
        logger.info("issue close summary posted to %s#%s", issue_repo, issue_number)
    except Exception:
        logger.warning(
            "issue close summary failed for %s#%s", issue_repo, issue_number, exc_info=True
        )


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _gh_api(path: str, token: str) -> Any:
    """GET a GitHub API path and return parsed JSON."""
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            _record_api_call(path, resp.status, resp.headers)
            return data
    except urllib.error.HTTPError as exc:
        _record_api_call(path, exc.code, exc.headers)
        raise


def _gh_api_post(path: str, token: str, payload: dict, method: str = "POST") -> Any:
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
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            _record_api_call(path, resp.status, resp.headers)
            return data
    except urllib.error.HTTPError as exc:
        _record_api_call(path, exc.code, exc.headers)
        raise


def _gh_api_diff(path: str, token: str) -> str:
    """GET a GitHub API path and return the raw unified diff text."""
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.diff",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            _record_api_call(path, resp.status, resp.headers)
            return text
    except urllib.error.HTTPError as exc:
        _record_api_call(path, exc.code, exc.headers)
        raise


def _gh_graphql(token: str, query: str, variables: dict) -> dict:
    """Execute a GitHub GraphQL query/mutation and return the parsed response."""
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            _record_api_call("/graphql", resp.status, resp.headers)
            return data
    except urllib.error.HTTPError as exc:
        _record_api_call("/graphql", exc.code, exc.headers)
        raise


def _parse_review_from_claude(text: str) -> dict:
    """Extract a review JSON object from Claude's response.

    Claude may wrap JSON in markdown code fences; this function strips them.
    Falls back to a minimal object if parsing fails entirely.
    """
    # Strip markdown fences if present
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass
    # Try the whole string
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Last resort: return a plain summary with no inline comments
    return {"summary": stripped[:2000], "comments": []}


async def _guild_github_token(guild_id: str, user_id: str | None = None) -> tuple[str, str] | None:
    """Return (access_token, github_username) to act as for this guild, or None.

    When *user_id* is given, prefer that member's own GitHub token so actions
    (issue claims, PR reviews, etc.) attribute to the acting human rather than
    the guild owner. For background/automated operations (*user_id* is None),
    prefer the GitHub App installation token when configured — so automated
    comments/reviews are attributed to the App identity — and fall back to
    the guild owner's token when the App isn't configured or has no token on
    file.
    """
    from auth_deps import get_guild_pk

    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            return None

        if user_id is not None:
            result = await db.exec(
                select(col(GithubToken.access_token), col(GithubToken.github_username))
                .join(GuildMember, col(GuildMember.user_id) == col(GithubToken.github_user_id))
                .where(
                    col(GuildMember.guild_id) == guild_pk,
                    col(GithubToken.github_user_id) == user_id,
                )
                .limit(1)
            )
            row = result.first()
            if row:
                return (row.access_token, row.github_username)
        else:
            inst_res = await db.exec(
                select(col(Guild.github_app_installation_id)).where(col(Guild.slug) == guild_id)
            )
            installation_id = inst_res.first()
            app_token = get_app_installation_token(installation_id)
            if app_token:
                return (app_token, get_app_slug())

        result = await db.exec(
            select(col(GithubToken.access_token), col(GithubToken.github_username))
            .join(GuildMember, col(GuildMember.user_id) == col(GithubToken.github_user_id))
            .where(col(GuildMember.guild_id) == guild_pk, col(GuildMember.role) == "owner")
            .limit(1)
        )
        row = result.first()
        return (row.access_token, row.github_username) if row else None
    finally:
        await db.close()


async def fetch_issue_state(repo: str, issue_number: int, token: str, db: Any | None = None) -> str:
    """Return the current lifecycle state (``"open"`` or ``"closed"``) of a GitHub issue.

    Used by the periodic closed-issue sweep (foreman.runner._sweep_closed_issues)
    to detect issues closed outside the webhook path (e.g. a missed delivery).
    When *db* is given, also upserts the fetched payload into the ``github_issues``
    cache so the tasks/tree UI stays in sync even when the webhook delivery
    that would normally do this was missed.
    """
    issue = await _to_thread(_gh_api, f"/repos/{repo}/issues/{issue_number}", token)
    if db is not None:
        await github_cache.upsert_issue(db, repo, issue)
    return issue.get("state", "open")


async def fetch_pr_status(repo: str, pr_number: int, token: str, db: Any | None = None) -> dict:
    """Fetch merge state, reviews, and check-runs for one PR.

    Shared by the ``get_pr_status`` tool and the periodic-check poll loop
    (foreman.runner._poll_loop), which proactively refreshes PR status for
    every non-terminal task with an open PR on each poll cycle. When *db* is
    given, also upserts the fetched payload into the ``github_pull_requests``
    cache so the tasks/tree UI stays in sync even when the webhook delivery
    that would normally do this was missed.
    """
    pr = await _to_thread(_gh_api, f"/repos/{repo}/pulls/{pr_number}", token)
    if db is not None:
        await github_cache.upsert_pr(db, repo, pr)
    reviews_raw = await _to_thread(
        _gh_api,
        f"/repos/{repo}/pulls/{pr_number}/reviews?per_page=20",
        token,
    )
    head_sha = (pr.get("head") or {}).get("sha")
    check_runs: list = []
    if head_sha:
        crs = await _to_thread(
            _gh_api,
            f"/repos/{repo}/commits/{head_sha}/check-runs?per_page=30",
            token,
        )
        if isinstance(crs, dict):
            check_runs = crs.get("check_runs", []) or []
    return {
        "number": pr["number"],
        "state": pr["state"],
        "merged": pr.get("merged", False),
        "mergeable": pr.get("mergeable"),
        "draft": pr.get("draft", False),
        "head_sha": head_sha,
        "reviews": [
            {
                "user": (r.get("user") or {}).get("login"),
                "state": r.get("state"),
                "body": (r.get("body") or "")[:300],
                "submitted_at": r.get("submitted_at"),
            }
            for r in reviews_raw
        ],
        "checks": [
            {
                "name": cr.get("name"),
                "status": cr.get("status"),
                "conclusion": cr.get("conclusion"),
                "summary": ((cr.get("output") or {}).get("summary") or "")[:300],
            }
            for cr in check_runs
        ],
    }


async def _guild_private_key_pem(guild_id: str) -> str | None:
    """Return the Ed25519 private key PEM for the guild, or None if not found."""
    from auth_deps import get_guild_pk

    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            return None
        result = await db.exec(
            select(col(GuildKey.private_key_pem)).where(col(GuildKey.guild_id) == guild_pk)
        )
        return result.one_or_none()
    finally:
        await db.close()


async def _select_followup_worker(
    db,
    *,
    guild_id: str,
    guild_pk: int | None = None,
    original_worker_id: str | None,
    preferred_worker_id: str | None = None,
) -> str | None:
    """Pick a worker to continue a task's branch.

    Order of preference:
      1. ``preferred_worker_id`` if it has at least one idle agent in the guild
      2. ``original_worker_id`` if it has at least one idle agent (worktree
         likely still on disk for free reuse)
      3. Any other worker in the guild with an idle agent

    Returns the chosen worker_id, or None if no idle worker is available.
    """

    async def _idle(worker_id: str) -> bool:
        if not worker_id:
            return False
        result = await db.exec(
            select(col(Agent.id))
            .where(col(Agent.worker_id) == worker_id, col(Agent.state) == "idle")
            .limit(1)
        )
        return result.one_or_none() is not None

    if preferred_worker_id and await _idle(preferred_worker_id):
        return preferred_worker_id
    if original_worker_id and await _idle(original_worker_id):
        return original_worker_id
    # Fallback: any other idle agent in the guild. Pick the worker_id of the
    # first idle agent we find — repos are configured per-worker, but for now
    # the foreman trusts that a guild's workers cover the same repo set.
    if guild_pk is None:
        from auth_deps import get_guild_pk

        guild_pk = await get_guild_pk(db, guild_id)
    result = await db.exec(
        select(col(Agent.worker_id))
        .where(
            col(Agent.guild_id) == guild_pk,
            col(Agent.state) == "idle",
            col(Agent.worker_id).is_not(None),
        )
        .limit(1)
    )
    return result.one_or_none()


async def maybe_post_plan_comment(guild_id: str, task_id: str, last_text: str) -> None:
    """Post plan output as a GitHub issue comment when a plan-phase task completes."""
    logger = logging.getLogger(__name__)
    try:
        db = await get_db()
        try:
            result = await db.exec(
                select(col(Task.phase), col(Task.issue_number), col(Task.issue_repo)).where(
                    col(Task.id) == task_id
                )
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
        await _to_thread(
            _gh_api_post,
            f"/repos/{issue_repo}/issues/{issue_number}/comments",
            token,
            {"body": body},
        )
        logger.info("plan comment posted to %s#%s for task %s", issue_repo, issue_number, task_id)
    except Exception as exc:
        logger.warning("plan comment failed for task %s: %s", task_id, exc)


async def _resolve_issue_title(
    guild_id: str, issue_repo: str, issue_number: int, fallback: str
) -> str:
    """Best-effort live GitHub title lookup for Discord thread naming.

    Falls back to *fallback* (the task's own name/description) on a missing
    token or any API failure, so a slow or failing GitHub call never blocks
    thread creation.
    """
    try:
        creds = await _guild_github_token(guild_id)
        if not creds:
            return fallback
        token, _ = creds
        issue = await _to_thread(_gh_api, f"/repos/{issue_repo}/issues/{issue_number}", token)
        return issue.get("title") or fallback
    except Exception:
        logger.warning(
            "issue title lookup failed repo=%s number=%s",
            issue_repo,
            issue_number,
            exc_info=True,
        )
        return fallback


async def notify_discord_task_assigned(
    guild_id: str,
    task_id: str,
    fallback_title: str,
    issue_repo: str | None = None,
    issue_number: int | None = None,
) -> None:
    """Notify Discord that a task was assigned, routing to the right thread.

    When *issue_repo*/*issue_number* are given, resolves (or creates) the
    issue's Discord thread as soon as a task is assigned — this is what moves
    thread creation earlier in the lifecycle; previously the first per-issue
    Discord thread only appeared when a PR was opened. This whole coroutine
    (including the GitHub title lookup) only ever runs via ``spawn``, which
    schedules it with ``asyncio.create_task`` and never awaits it in the
    caller's context — so a slow GitHub API call delays this background task,
    not the tool call that triggered it.

    When they're omitted, the GitHub title lookup is skipped and this falls
    back to the task's own ``"task_stream"`` thread (see
    ``discord_notifier.notify_task_stream_event``) — every task gets a
    Discord thread of some kind, whether or not it has a linked GitHub issue.

    Skips entirely when Discord notifications aren't configured.
    """
    if not discord_notifier.is_configured():
        return
    description = f"Foreman assigned task `{task_id}`."
    if issue_repo and issue_number is not None:
        title = await _resolve_issue_title(guild_id, issue_repo, issue_number, fallback_title)
        prefix = f"#{issue_number}: "
        thread_name = prefix + title[: 100 - len(prefix)]
        description = f"Foreman assigned task `{task_id}` on {issue_repo}#{issue_number}: {title}"
        await discord_notifier.notify_event(
            "task-assigned",
            title=f"Task assigned: #{issue_number}",
            description=description,
            issue_repo=issue_repo,
            issue_number=issue_number,
            thread_name=thread_name,
            ps_guild_slug=guild_id,
            task_id=task_id,
        )
        return

    await discord_notifier.notify_task_stream_event(
        guild_id,
        task_id,
        "task-assigned",
        title=f"Task assigned: {task_id}",
        description=description,
    )


def _spawn_discord_task_assigned(
    guild_id: str,
    inp: dict,
    task_id: str,
    fallback_title: str,
) -> None:
    """Fire off ``notify_discord_task_assigned`` for a just-assigned task.

    Shared by both branches of ``assign_task`` (re-assign vs. newly created
    task). Always spawns: every assigned task gets a Discord notification,
    routed to its linked issue's thread when the tool call carries
    ``issue_repo``/``issue_number``, or to its own task-stream thread
    otherwise (standalone tasks created without a GitHub issue, e.g. from
    Discord chat — see ``notify_discord_task_assigned``).
    """
    spawn(
        notify_discord_task_assigned(
            guild_id,
            task_id,
            fallback_title,
            issue_repo=inp.get("issue_repo"),
            issue_number=int(inp["issue_number"]) if inp.get("issue_number") is not None else None,
        ),
        name=f"discord.task-assigned:{task_id}",
    )


async def notify_discord_followup(
    issue_repo: str | None,
    issue_number: int | None,
    task_id: str,
    instructions: str,
) -> None:
    """Post a follow-up notification into the task's existing Discord thread.

    Uses ``notify_existing_thread`` rather than ``notify_event``: the thread
    should already exist from ``assign_task``, so a missing thread here
    falls back to the flat channel instead of spawning a new one. *task_id*
    lets the notifier resolve the canonical thread even when the task has no
    issue linkage (PR-only work).
    """
    if not discord_notifier.is_configured():
        return
    await discord_notifier.notify_existing_thread(
        "task-followup",
        title=f"Follow-up sent: {task_id}",
        description=f"Follow-up dispatched for task `{task_id}`: {instructions[:500]}",
        issue_repo=issue_repo,
        issue_number=issue_number,
        task_id=task_id,
    )


async def notify_discord_redirect(
    issue_repo: str | None,
    issue_number: int | None,
    task_id: str,
    instructions: str,
) -> None:
    """Post a redirect notification into the task's existing Discord thread.

    See ``notify_discord_followup`` — same existing-thread-only routing.
    """
    if not discord_notifier.is_configured():
        return
    await discord_notifier.notify_existing_thread(
        "task-redirect",
        title=f"Task redirected: {task_id}",
        description=f"Redirect sent for task `{task_id}`: {instructions[:500]}",
        issue_repo=issue_repo,
        issue_number=issue_number,
        task_id=task_id,
    )


async def notify_discord_task_finalized(
    issue_repo: str | None,
    issue_number: int | None,
    task_id: str,
    state: str,
    reason: str | None = None,
) -> None:
    """Post a task-closed notification into the task's existing Discord thread.

    Covers the outcomes that never trigger a notification anywhere else:
    ``finalize_task(outcome="failed")`` and ``cancel_task`` both closed a task
    out silently before this — unlike a successful finish, which is already
    covered by ``handle_task_complete``'s ``task-complete`` notification (#920).
    See ``notify_discord_followup`` — same existing-thread-only routing.
    """
    if not discord_notifier.is_configured():
        return
    verb = "cancelled" if state == "cancelled" else "failed"
    description = f"Task `{task_id}` was {verb}."
    if reason:
        description += f" Reason: {reason[:400]}"
    await discord_notifier.notify_existing_thread(
        f"task-{verb}",
        title=f"Task {verb}: {task_id}",
        description=description,
        issue_repo=issue_repo,
        issue_number=issue_number,
        task_id=task_id,
    )


# ---------------------------------------------------------------------------
# review_pr_internal helpers
# ---------------------------------------------------------------------------

_PR_URL_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)/?$")

_GQL_PR_THREADS = """
query GetPRThreads($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 10) {
            nodes {
              databaseId
              pullRequestReview { databaseId }
            }
          }
        }
      }
    }
  }
}
"""

_GQL_RESOLVE_THREAD = """
mutation ResolveThread($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


async def _supersede_prior_bot_reviews(
    pr_repo: str, pr_number: int, bot_username: str, token: str
) -> int:
    """Resolve prior inline threads from previous bot reviews.

    Before a new review is posted this function:
    1. Fetches all existing reviews and filters to those authored by ``bot_username``.
    2. Resolves any unresolved inline threads that belong to those reviews via
       the GitHub GraphQL ``resolveReviewThread`` mutation.

    Returns the count of threads resolved. Errors in individual steps are
    logged as warnings rather than raised so that the main review post is never
    blocked by a cleanup failure.
    """
    reviews = await _to_thread(_gh_api, f"/repos/{pr_repo}/pulls/{pr_number}/reviews", token)
    if not isinstance(reviews, list):
        return 0

    bot_reviews = [r for r in reviews if (r.get("user") or {}).get("login") == bot_username]
    if not bot_reviews:
        return 0

    bot_review_ids = {r["id"] for r in bot_reviews}

    # Resolve unresolved inline threads that belong to our prior reviews.
    threads_resolved = 0
    owner, repo_name = pr_repo.split("/", 1)
    try:
        gql_result = await _to_thread(
            _gh_graphql,
            token,
            _GQL_PR_THREADS,
            {"owner": owner, "repo": repo_name, "number": pr_number},
        )
        threads = (
            gql_result.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        for thread in threads:
            if thread.get("isResolved"):
                continue
            comments = thread.get("comments", {}).get("nodes", [])
            if any(
                (c.get("pullRequestReview") or {}).get("databaseId") in bot_review_ids
                for c in comments
            ):
                try:
                    await _to_thread(
                        _gh_graphql,
                        token,
                        _GQL_RESOLVE_THREAD,
                        {"threadId": thread["id"]},
                    )
                    threads_resolved += 1
                    logger.info(
                        "review_pr: resolved thread %s on %s#%d",
                        thread["id"],
                        pr_repo,
                        pr_number,
                    )
                except Exception as exc:
                    logger.warning("review_pr: failed to resolve thread %s: %s", thread["id"], exc)
    except Exception as exc:
        logger.warning(
            "review_pr: GraphQL thread resolution failed for %s#%d: %s",
            pr_repo,
            pr_number,
            exc,
        )

    return threads_resolved


# ---------------------------------------------------------------------------
# A2A agent call helpers
# ---------------------------------------------------------------------------


def _dnsid_resolve(fqdn: str) -> dict:
    """Fully verify a domain with the configured dnsid-py IdentityManager."""
    from foreman.dnsid_identity import get_dnsid_runtime

    runtime = get_dnsid_runtime()
    if runtime is None:
        raise RuntimeError("dnsid resolve: DNSID_CONFIG_DIR is not configured")
    verified = runtime.manager.verify_domain(fqdn)
    return {
        "ok": True,
        "fqdn": verified.domain,
        "governance_id": verified.record.gi,
        "status": verified.cached_state(),
        "dnssec_state": verified.dnssec_state.name,
        "verified_at": verified.verified_at.isoformat(),
        "expiry": verified.expiry().isoformat(),
    }


def _dnsid_verify(jwt_token: str, expected_aud: str, expected_nonce: str | None = None) -> dict:
    """Verify a JWT and its issuer through dnsid-py's JoseProfile."""
    from dnsid.jose import JoseProfile
    from foreman.dnsid_identity import get_dnsid_runtime

    runtime = get_dnsid_runtime()
    if runtime is None:
        raise RuntimeError("dnsid verify: DNSID_CONFIG_DIR is not configured")
    verified = JoseProfile.from_identity_manager(runtime.manager).verify_jwt(jwt_token)
    from foreman.oidc import _decode_jwt_parts

    _, payload, _, _ = _decode_jwt_parts(jwt_token)
    aud = payload.get("aud")
    audiences = [aud] if isinstance(aud, str) else list(aud or [])
    if expected_aud not in audiences:
        raise RuntimeError("dnsid verify: audience mismatch")
    if expected_nonce and payload.get("nonce") != expected_nonce:
        raise RuntimeError("dnsid verify: nonce mismatch")
    return {"ok": True, "iss": verified.domain, **payload}


async def _run_dnsid(command: str, inp: dict) -> dict:
    """Run a dnsid operation using the dnsid-py library."""
    if command == "resolve":
        fqdn = inp.get("fqdn", "")
        if not fqdn:
            raise ValueError("dnsid resolve requires fqdn")
        return await _to_thread(_dnsid_resolve, fqdn)
    elif command == "verify":
        jwt_token = inp.get("jwt", "")
        expected_aud = inp.get("expected_aud", "")
        if not jwt_token:
            raise ValueError("dnsid verify requires jwt")
        if not expected_aud:
            raise ValueError("dnsid verify requires expected_aud")
        return await _to_thread(_dnsid_verify, jwt_token, expected_aud, inp.get("expected_nonce"))
    else:
        raise ValueError(f"Unknown dnsid command: {command!r}")


async def _message_discord_bot(inp: dict, db) -> tuple[str, bool]:
    """Send a message to a Discord bot user (see ``message_discord_bot`` in
    ``foreman/tools_schema.py``). Returns ``(result_text, is_error)``."""
    bot_user_id = inp.get("bot_user_id") or ""
    message = inp.get("message") or ""
    delivery_method = inp.get("delivery_method") or "channel"

    if not bot_user_id:
        return "message_discord_bot requires bot_user_id", True
    if not message:
        return "message_discord_bot requires message", True

    result = await db.exec(select(User).where(col(User.id) == bot_user_id))
    user = result.one_or_none()
    if user is None:
        return f"No user found with id {bot_user_id!r}.", True
    if not user.is_bot or user.bot_provider != "discord":
        return (
            f"User {bot_user_id!r} is not a Discord bot "
            f"(is_bot={user.is_bot}, bot_provider={user.bot_provider!r})."
        ), True
    if not user.discord_channel_id:
        return f"Bot {bot_user_id!r} has no discord_channel_id configured.", True

    content = message
    if delivery_method == "mention":
        discord_user_id = (user.bot_metadata or {}).get("discord_user_id")
        if not discord_user_id:
            return (
                f"Bot {bot_user_id!r} has no discord_user_id in bot_metadata for mention delivery."
            ), True
        content = f"<@{discord_user_id}> {message}"

    response = await discord_notifier.post(
        f"/channels/{user.discord_channel_id}/messages", {"content": content}
    )
    if response is None:
        return f"Failed to send Discord message to bot {bot_user_id!r}.", True

    return (
        json.dumps(
            {
                "bot_user_id": bot_user_id,
                "channel_id": user.discord_channel_id,
                "delivery_method": delivery_method,
                "message_id": response.get("id") if isinstance(response, dict) else None,
            }
        ),
        False,
    )


def _fetch_agent_card(card_url: str) -> dict:
    """Fetch and parse an A2A agent card from a well-known URL."""
    req = urllib.request.Request(
        card_url,
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _post_agent_task(task_url: str, body: bytes) -> dict:
    """POST a JSON-RPC tasks/send payload to an A2A agent and return the result dict."""
    req = urllib.request.Request(
        task_url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"Agent error {err.get('code')}: {err.get('message', 'unknown')}")
    return data.get("result", data)


# ---------------------------------------------------------------------------
# spawn_worker implementation — exposed to the parent foreman via FOREMAN_TOOLS
# and invoked by the Discord /worker-spawn command. Spawned workers are
# auto-reaped by worker_lifecycle.idle_worker_reaper() after a period of
# inactivity; parameters omitted from the call fall back through resolve_spawn
# (spawn_settings: this user's override, then the guild baseline).
# ---------------------------------------------------------------------------


async def spawn_worker(
    inp: dict,
    guild_id: str,
    guild_pk: int | None,
    db,
    user_id: str | None = None,
) -> tuple[str, bool]:
    """Spawn a new worker container.

    Asynchronous: this coroutine returns as soon as the container/ECS task has
    been started, not once the worker is online. The worker process itself
    takes about 2 minutes to come up and register before it can pick up
    assigned tasks.

    *user_id* identifies the human on whose behalf the worker is being spawned
    (e.g. a Discord user's linked Pioneer Square account) and is stamped onto
    the new ``Worker`` row for ownership attribution. ``None`` for
    unattributed spawns.

    Returns (result_text, is_error).
    """
    from spawn_config import SpawnLayer, merge_layers, resolve_spawn  # noqa: PLC0415

    custom_name: str | None = inp.get("name")

    # One resolution path: call args override the user's spawn_settings override
    # over the guild baseline (see spawn_config). Anything the call omits falls
    # back down the layers, so "spawn a worker" works without restating config.
    call = SpawnLayer(
        repos=inp.get("repos") or None,
        tools=inp.get("tools") or None,
        agent_count=inp.get("agent_count"),
    )
    resolved = (
        await resolve_spawn(db, guild_pk, user_id, call)
        if guild_pk is not None
        else merge_layers([call])
    )
    repos = resolved.repos
    tools_list = resolved.tools
    # "Spawn a worker" means one agent unless asked for more; without this the
    # worker falls through to config's max_agents default (4). ponytail: 1 slot.
    agent_count = resolved.agent_count if resolved.agent_count is not None else 1
    if not repos:
        return (
            "spawn_worker requires at least one repo in the 'repos' list — this guild has "
            "no recorded spawn defaults to fall back on."
        ), True

    if guild_pk is not None:
        from worker_lifecycle import check_worker_spawn_cooldown  # noqa: PLC0415

        remaining = await check_worker_spawn_cooldown(db, guild_pk)
        if remaining is not None:
            minutes = max(1, math.ceil(remaining.total_seconds() / 60))
            return (
                "Worker spawn cooldown active. Try again in "
                f"{minutes} minute{'s' if minutes != 1 else ''}."
            ), True

    worker_id = "w-" + secrets.token_hex(3)
    auth_token = secrets.token_urlsafe(32)
    worker_name = custom_name or worker_display_name(worker_id, None)
    created_at = datetime.now(UTC)
    db.add(
        Worker(
            id=worker_id,
            guild_id=guild_pk or 0,
            repos=json.dumps(repos),
            state="launching",
            created_at=created_at,
            auth_token=auth_token,
            name=worker_name,
            user_id=user_id,
        )
    )
    await db.commit()
    await broadcast_msg(
        guild_id, WorkerStateMsg(workerId=worker_id, state="launching", name=worker_name)
    )

    # Worker-facing env vars come from the resolved spawn_settings (guild
    # baseline + user override), already merged in spawn_config. tool_env_vars
    # still reach the worker via /guilds/{id}/foreman/env-vars at startup.
    env = build_spawn_worker_env(
        guild_id=guild_id,
        repos=repos,
        worker_name=worker_name,
        source_env=dict(os.environ),
        worker_id=worker_id,
        auth_token=auth_token,
        agent_count=agent_count,
        tools=tools_list or None,
        extra_env=resolved.env_vars or None,
    )

    try:
        spawned = await worker_runtime.start_worker_container(env=env, guild_id=guild_id)
        # Persist the spawn handle and version so the lifecycle module can
        # force-kill this container/task if the backend is redeployed with a
        # different version (or the worker idles past the reap timeout).
        from worker_lifecycle import record_worker_spawn as _record_worker_spawn  # noqa: PLC0415

        await _record_worker_spawn(
            db,
            worker_id,
            spawned.handle,
        )
        result_text = json.dumps(
            {
                "worker_id": worker_id,
                "container_id": spawned.short_id,
                "runtime": spawned.runtime,
                "repos": repos,
                "name": worker_name,
            }
        )
        logger.info(
            "spawn_worker: guild=%s worker_id=%s runtime=%s container=%s repos=%s",
            guild_id,
            worker_id,
            spawned.runtime,
            spawned.short_id,
            repos,
        )
        return result_text, False
    except ImportError:
        await db.exec(
            update(Worker).where(col(Worker.id) == worker_id).values(state="spawn_failed")
        )
        await db.commit()
        await broadcast_msg(
            guild_id, WorkerStateMsg(workerId=worker_id, state="spawn_failed", name=worker_name)
        )
        sdk = "boto3" if worker_runtime.ecs_enabled() else "Docker SDK"
        return (
            f"Worker pre-registered as {worker_id} but the {sdk} is not "
            "installed on the backend — container was NOT started. "
            f"To start manually: PIONEER_WORKER_ID={worker_id} "
            f"PIONEER_AUTH_TOKEN={auth_token} "
            "<worker-start-command>"
        ), True
    except Exception as exc:
        await db.exec(
            update(Worker).where(col(Worker.id) == worker_id).values(state="spawn_failed")
        )
        await db.commit()
        await broadcast_msg(
            guild_id, WorkerStateMsg(workerId=worker_id, state="spawn_failed", name=worker_name)
        )
        return (f"Worker pre-registered as {worker_id} but failed to start container: {exc}"), True


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------


async def exec_tools(
    guild_id: str,
    tool_uses: list,
    user_id: str | None = None,
    *,
    own_task_id: str | None = None,
) -> list:
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

    *own_task_id* is the task_id of the per-task child context this batch is
    running in (None for parent/whole-guild runs). Passed through to
    task-mutating handlers (send_followup, redirect_task, cancel_task,
    finalize_task) so they can tell whether they're a task's own child
    context (never conflicts with itself) versus a parent run reaching into a
    task that a child run currently owns — see ``_task_mutation_blocked``.
    """
    coros = [_exec_one_tool(guild_id, tu, user_id, own_task_id=own_task_id) for tu in tool_uses]
    return list(await asyncio.gather(*coros))


def _full_log_content(data_json: str | None) -> str | None:
    """Extract the full, untruncated content from a TaskLog.data JSON blob.

    Runners store the complete tool input/output in ``data`` while ``line``
    holds a short display summary; this recovers the full text so it can be
    surfaced to the Foreman via get_task_status.
    """
    if not data_json:
        return None
    try:
        detail = json.loads(data_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(detail, dict):
        return None
    for key in ("output", "fullText", "input"):
        val = detail.get(key)
        if isinstance(val, str) and val:
            return val
        if val is not None:
            return json.dumps(val, default=_json_default)
    return None


def _task_mutation_blocked(guild_id: str, task_id: str, own_task_id: str | None) -> str | None:
    """Return a user-facing message if *task_id* is owned by a different in-flight
    Foreman context right now, else None.

    ``own_task_id`` is the task_id of the per-task child context this tool
    call is itself running in (None for a parent/whole-guild run). A run
    never conflicts with its own per-task lock — this only fires when a
    *different* context holds it, which in practice means a parent run
    (periodic-check or human chat) reaching into a task whose own child run
    is currently mutating it. See issue #927: parent and child runs key on
    different (guild_id, key) pairs and would otherwise never serialise
    against each other for the same task.
    """
    if own_task_id == task_id:
        return None
    from foreman.runner import is_child_task_run_active  # noqa: PLC0415 — avoids circular import

    if is_child_task_run_active(guild_id, task_id):
        return (
            f"Task {task_id} has an in-flight child Foreman run right now — "
            "skipping this action to avoid racing it. Retry shortly once it completes."
        )
    return None


async def _resolve_bedrock_model_id(db, provider: str | None, model: str | None) -> str | None:
    """For Bedrock workers, swap a short models.dev model ID for the canonical
    inference-profile ARN stored in the catalog.

    The Claude CLI on Bedrock requires the full ARN; short IDs are rejected by
    the InvokeModel API. No-op for non-Bedrock providers or when *model* is
    unset. Falls back to the short ID (with a warning) when no ARN is on file.
    """
    if provider != "bedrock" or not model:
        return model
    from models import ModelCatalog  # noqa: PLC0415

    bedrock_id_result = await db.exec(
        select(col(ModelCatalog.bedrock_model_id)).where(
            col(ModelCatalog.provider) == "bedrock",
            col(ModelCatalog.model_id) == model,
        )
    )
    resolved = bedrock_id_result.one_or_none()
    if resolved:
        return resolved
    logger.warning(
        "no Bedrock ARN for model %r "
        "(catalog may need a refresh with AWS credentials configured) "
        "— using short ID as fallback; worker may fail at inference time",
        model,
    )
    return model


async def _exec_one_tool(
    guild_id: str, tu, user_id: str | None = None, *, own_task_id: str | None = None
) -> dict:
    """Execute a single tool call and return its tool_result block."""
    inp = tu.input
    result_text = ""
    is_error = False
    _ctx_token = _api_calls_ctx.set([])
    try:
        db = await get_db()
        try:
            from auth_deps import get_guild_pk

            guild_pk = await get_guild_pk(db, guild_id)
            if tu.name == "create_task":
                # No lock needed: creates an unassigned task (worker_id=None) so
                # there is no worker state to race on. Task ID collisions are
                # statistically negligible and caught by the DB unique constraint.
                name = (inp.get("name") or "")[:80]
                desc = inp.get("description", name)
                phase = inp.get("phase", "execute")
                task_id = "t-" + "".join(
                    random.choices(string.ascii_lowercase + string.digits, k=6)
                )
                created_at = datetime.now(UTC)
                db.add(
                    Task(
                        id=task_id,
                        worker_id=None,
                        guild_id=guild_pk or 0,
                        name=name,
                        description=desc,
                        tool="claude",
                        state="pending",
                        phase=phase,
                        issue_number=inp.get("issue_number"),
                        issue_repo=inp.get("issue_repo"),
                        pr_number=inp.get("pr_number"),
                        pr_repo=inp.get("pr_repo"),
                        created_at=created_at,
                        user_id=user_id,
                    )
                )
                await db.commit()
                await broadcast(
                    guild_id,
                    TaskCreatedMsg(
                        taskId=task_id,
                        name=name,
                        description=desc,
                        phase=phase,
                        state="pending",
                        createdAt=created_at.isoformat(),
                    ).model_dump(by_alias=True, exclude_none=True),
                )
                result_text = (
                    f"Task {task_id} created: '{name}'. Reference this task_id in assign_task."
                )

            elif tu.name == "assign_task":
                wid = inp["worker_id"]
                desc = inp.get("description", "")
                phase = inp.get("phase", "execute")
                requested_tool: str | None = inp.get("tool")
                model = inp.get("model") or None
                requested_tier: str | None = inp.get("tier") or None
                provider = inp.get("provider") or None
                existing_task_id = inp.get("task_id")
                existing_linkage: tuple[int | None, str | None, int | None, str | None] | None = (
                    None
                )
                if existing_task_id:
                    existing_linkage_result = await db.exec(
                        select(
                            col(Task.issue_number),
                            col(Task.issue_repo),
                            col(Task.pr_number),
                            col(Task.pr_repo),
                        ).where(col(Task.id) == existing_task_id, col(Task.guild_id) == guild_pk)
                    )
                    existing_linkage = existing_linkage_result.one_or_none()

                effective_issue_number = inp.get("issue_number")
                effective_issue_repo = inp.get("issue_repo")
                effective_pr_number = inp.get("pr_number")
                effective_pr_repo = inp.get("pr_repo")
                if existing_linkage:
                    (
                        existing_issue_number,
                        existing_issue_repo,
                        existing_pr_number,
                        existing_pr_repo,
                    ) = existing_linkage
                    if effective_issue_number is None:
                        effective_issue_number = existing_issue_number
                    if not effective_issue_repo:
                        effective_issue_repo = existing_issue_repo
                    if effective_pr_number is None:
                        effective_pr_number = existing_pr_number
                    if not effective_pr_repo:
                        effective_pr_repo = existing_pr_repo

                guild_result = await db.exec(
                    select(col(Guild.primary_repo)).where(col(Guild.id) == guild_pk)
                )
                primary_repo: str | None = guild_result.one_or_none()
                target_repo = effective_pr_repo or effective_issue_repo
                repos: list[str] = inp.get("repos") or ([target_repo] if target_repo else [])
                if not repos and primary_repo:
                    repos = [primary_repo]
                worker_result = await db.exec(
                    select(
                        col(Worker.id),
                        col(Worker.repos),
                        col(Worker.org),
                        col(Worker.tools),
                        col(Worker.provider),
                    ).where(col(Worker.id) == wid, col(Worker.guild_id) == guild_pk)
                )
                worker_row = worker_result.one_or_none()
                if not worker_row:
                    result_text = f"Worker {wid} not found — task NOT queued."
                    is_error = True
                else:
                    worker_tools: list[str] = json.loads(worker_row.tools or "[]")
                    worker_provider: str | None = worker_row.provider

                    # Resolve the tool FIRST — before computing the model tier or
                    # auto-selecting a model — so both are derived from the tool the
                    # worker actually has, never from a "claude" placeholder. A worker
                    # with tools=["pi"] must never end up with a claude model_tier.
                    if requested_tool is None:
                        tool = worker_tools[0] if worker_tools else "claude"
                    elif worker_tools and requested_tool not in worker_tools:
                        available = ", ".join(worker_tools)
                        result_text = (
                            f"Worker {wid} does not support tool {requested_tool!r}. "
                            f"Available tools: {available}"
                        )
                        is_error = True
                        tool = requested_tool
                    else:
                        # requested_tool is set and either matches worker_tools or the
                        # worker is legacy (no tools registered) — accept it as-is.
                        tool = requested_tool

                    # Pi has no built-in default that authenticates — fall back to
                    # the guild's configured pi default provider/model (#1040).
                    if tool == "pi" and (provider is None or model is None):
                        provider, model = _apply_pi_defaults(
                            tool, provider, model, await _guild_foreman_config(db, guild_pk)
                        )

                    from util.model_tiers import select_model_tier as _select_tier  # noqa: PLC0415

                    model_tier = _select_tier(phase, complexity_hint=requested_tier)

                    # Filter model selection to only provider-compatible models.
                    if worker_provider and not is_error:
                        from models import ModelCatalog  # noqa: PLC0415

                        if model:
                            catalog_check = await db.exec(
                                select(col(ModelCatalog.model_id)).where(
                                    col(ModelCatalog.provider) == worker_provider,
                                    col(ModelCatalog.model_id) == model,
                                )
                            )
                            if catalog_check.one_or_none() is None:
                                result_text = (
                                    f"Model {model!r} is not available for provider "
                                    f"{worker_provider!r}. Use GET /api/models to see "
                                    "available models for each provider."
                                )
                                is_error = True
                        else:
                            # Auto-select: use pre-computed model_tier → best model from catalog.
                            from util.model_tiers import get_model_for_tier  # noqa: PLC0415
                            from util.models_dev import get_providers_from_db  # noqa: PLC0415

                            catalog = await get_providers_from_db(db)
                            model = get_model_for_tier(model_tier, worker_provider, catalog)
                    # For Bedrock workers, resolve the short model ID to the
                    # canonical inference-profile ARN the Claude CLI requires.
                    if not is_error:
                        model = await _resolve_bedrock_model_id(db, worker_provider, model)
                    if not is_error and repos:
                        worker_repos: list[str] = json.loads(worker_row.repos or "[]")
                        worker_org: str | None = worker_row.org
                        unreachable = [
                            r
                            for r in repos
                            if r not in worker_repos
                            and not (worker_org and r.startswith(f"{worker_org}/"))
                        ]
                        if unreachable:
                            result_text = (
                                f"Worker {wid} cannot access repo(s) {unreachable} — "
                                f"task NOT queued. Worker has {len(worker_repos)} registered "
                                f"repo(s) and org={worker_org!r}. Choose a worker that has "
                                f"access to these repos, or omit repos to use the worker's "
                                f"full configured list."
                            )
                            is_error = True
                if not is_error:
                    # Prevent two concurrent foreman runs from double-assigning
                    # the same idle worker.  Lock covers the window from worker
                    # selection through task row written + worker notified.
                    assign_lock_key = f"assign_task:{wid}"
                    assign_lock_id = "".join(
                        random.choices(string.ascii_lowercase + string.digits, k=8)
                    )
                    lock_acquired = await LockService(db).acquire(
                        assign_lock_key, owner=assign_lock_id, ttl_seconds=60
                    )
                    await db.commit()
                    if not lock_acquired:
                        result_text = (
                            f"Worker {wid} is already being assigned a task by a concurrent "
                            "foreman run. Retry after the current assignment completes."
                        )
                        is_error = True
                    else:
                        try:
                            # Re-check worker availability inside the lock to close the
                            # TOCTOU window: two concurrent foremen may have both seen the
                            # worker as available before either acquired the lock.
                            worker_recheck = await db.exec(
                                select(col(Worker.id))
                                .where(
                                    col(Worker.id) == wid,
                                    col(Worker.state) != "offline",
                                )
                                .limit(1)
                            )
                            parent_task_id = inp.get("parent_task_id")
                            if not worker_recheck.one_or_none():
                                result_text = (
                                    f"Worker {wid} went offline — task NOT assigned. "
                                    "Pick a different worker and retry."
                                )
                                is_error = True
                            elif existing_task_id:
                                name_override = inp.get("name")
                                update_values: dict = {
                                    "worker_id": wid,
                                    "description": desc,
                                    "tool": tool,
                                    "model": model,
                                    "model_tier": model_tier,
                                    "provider": provider,
                                    "phase": phase,
                                    "state": "pending",
                                }
                                if name_override:
                                    update_values["name"] = name_override
                                if effective_issue_number is not None:
                                    update_values["issue_number"] = effective_issue_number
                                if effective_issue_repo:
                                    update_values["issue_repo"] = effective_issue_repo
                                if effective_pr_number is not None:
                                    update_values["pr_number"] = effective_pr_number
                                if effective_pr_repo:
                                    update_values["pr_repo"] = effective_pr_repo
                                if parent_task_id is not None:
                                    update_values["parent_task_id"] = parent_task_id
                                await db.exec(
                                    update(Task)
                                    .where(
                                        col(Task.id) == existing_task_id,
                                        col(Task.guild_id) == guild_pk,
                                    )
                                    .values(**update_values)
                                )
                                await db.commit()
                                name_result = await db.exec(
                                    select(col(Task.name)).where(col(Task.id) == existing_task_id)
                                )
                                task_name = name_result.one_or_none() or desc[:60]
                                task_id = existing_task_id
                                await broadcast(
                                    guild_id,
                                    TaskAssignedMsg(
                                        workerId=wid,
                                        taskId=task_id,
                                        name=task_name,
                                        description=desc,
                                        tool=tool,
                                        model=model,
                                        modelTier=model_tier,
                                        provider=provider,
                                        phase=phase,
                                        parentTaskId=parent_task_id,
                                        issueNumber=effective_issue_number,
                                        issueRepo=effective_issue_repo,
                                        prNumber=effective_pr_number,
                                        prRepo=effective_pr_repo,
                                        repos=repos,
                                    ).model_dump(by_alias=True, exclude_none=True),
                                )
                                _spawn_discord_task_assigned(
                                    guild_id,
                                    {
                                        **inp,
                                        "issue_number": effective_issue_number,
                                        "issue_repo": effective_issue_repo,
                                        "pr_number": effective_pr_number,
                                        "pr_repo": effective_pr_repo,
                                    },
                                    task_id,
                                    task_name,
                                )
                                result_text = f"Task {task_id} assigned to {wid}."
                            else:
                                name = inp.get("name") or desc[:60]
                                task_id = "t-" + "".join(
                                    random.choices(string.ascii_lowercase + string.digits, k=6)
                                )
                                created_at = datetime.now(UTC)
                                db.add(
                                    Task(
                                        id=task_id,
                                        worker_id=wid,
                                        guild_id=guild_pk or 0,
                                        name=name,
                                        description=desc,
                                        tool=tool,
                                        model=model,
                                        model_tier=model_tier,
                                        provider=provider,
                                        issue_number=effective_issue_number,
                                        issue_repo=effective_issue_repo,
                                        pr_number=effective_pr_number,
                                        pr_repo=effective_pr_repo,
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
                                    TaskAssignedMsg(
                                        workerId=wid,
                                        taskId=task_id,
                                        name=name,
                                        description=desc,
                                        tool=tool,
                                        model=model,
                                        modelTier=model_tier,
                                        provider=provider,
                                        phase=phase,
                                        parentTaskId=parent_task_id,
                                        issueNumber=effective_issue_number,
                                        issueRepo=effective_issue_repo,
                                        prNumber=effective_pr_number,
                                        prRepo=effective_pr_repo,
                                        repos=repos,
                                    ).model_dump(by_alias=True, exclude_none=True),
                                )
                                _spawn_discord_task_assigned(
                                    guild_id,
                                    {
                                        **inp,
                                        "issue_number": effective_issue_number,
                                        "issue_repo": effective_issue_repo,
                                        "pr_number": effective_pr_number,
                                        "pr_repo": effective_pr_repo,
                                    },
                                    task_id,
                                    name,
                                )
                                result_text = f"Task {task_id} queued for {wid}."
                        finally:
                            await LockService(db).release(assign_lock_key, owner=assign_lock_id)
                            await db.commit()

            elif tu.name == "send_followup":
                task_id = inp["task_id"]
                instructions = inp["instructions"]
                preferred_worker_id = inp.get("preferred_worker_id")
                requested_tool: str | None = inp.get("tool")
                requested_model: str | None = inp.get("model") or None
                requested_tier: str | None = inp.get("tier") or None
                requested_provider: str | None = inp.get("provider") or None
                _blocked = _task_mutation_blocked(guild_id, task_id, own_task_id)
                if _blocked:
                    result_text = _blocked
                    is_error = True
                else:
                    result = await db.exec(
                        select(
                            col(Task.worker_id),
                            col(Task.state),
                            col(Task.branch),
                            col(Task.description),
                            col(Task.name),
                            col(Task.tool),
                            col(Task.model),
                            col(Task.model_tier),
                            col(Task.provider),
                            col(Task.issue_number),
                            col(Task.issue_repo),
                            col(Task.claude_session_id),
                        ).where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
                    )
                    row = result.one_or_none()
                    if not row:
                        result_text = f"Task {task_id} not found."
                    else:
                        (
                            original_worker_id,
                            prior_state,
                            branch,
                            task_desc,
                            task_name,
                            task_tool,
                            task_model,
                            task_model_tier,
                            task_provider,
                            task_issue_number,
                            task_issue_repo,
                            task_session_id,
                        ) = row
                        target_worker_id = await _select_followup_worker(
                            db,
                            guild_id=guild_id,
                            original_worker_id=original_worker_id,
                            preferred_worker_id=preferred_worker_id,
                        )
                        if not target_worker_id:
                            result_text = (
                                f"No idle worker available to continue task {task_id} on branch "
                                f"{branch or '<unknown>'}. Wait for one to come online or shut "
                                "down a busy worker before retrying."
                            )
                            is_error = True
                        elif not branch:
                            result_text = (
                                f"Task {task_id} has no branch recorded — can't dispatch a "
                                "follow-up. The task may have failed before its first push."
                            )
                            is_error = True
                        else:
                            followup_worker_result = await db.exec(
                                select(col(Worker.tools), col(Worker.provider)).where(
                                    col(Worker.id) == target_worker_id
                                )
                            )
                            followup_worker_row = followup_worker_result.one_or_none()
                            followup_worker_tools_json = (
                                followup_worker_row[0] if followup_worker_row else None
                            )
                            followup_worker_provider = (
                                followup_worker_row[1] if followup_worker_row else None
                            )
                            if isinstance(followup_worker_tools_json, str):
                                followup_worker_tools: list[str] = json.loads(
                                    followup_worker_tools_json or "[]"
                                )
                            else:
                                followup_worker_tools = followup_worker_tools_json or []

                            if (
                                requested_tool
                                and followup_worker_tools
                                and requested_tool not in followup_worker_tools
                            ):
                                available = ", ".join(followup_worker_tools)
                                result_text = (
                                    f"Worker {target_worker_id} does not support tool "
                                    f"{requested_tool!r}. Available tools: {available}"
                                )
                                is_error = True
                            else:
                                effective_tool = (
                                    requested_tool
                                    or task_tool
                                    or (
                                        followup_worker_tools[0]
                                        if followup_worker_tools
                                        else "claude"
                                    )
                                )
                                if requested_model is not None:
                                    effective_model = requested_model
                                elif requested_tier:
                                    # Explicit tier bump/de-escalation — drop the pinned
                                    # model so it's re-resolved from the new tier below.
                                    effective_model = None
                                elif requested_tool and requested_tool != task_tool:
                                    # Switching tools invalidates the previous model choice
                                    # (e.g. a claude model id is meaningless to codex/pi) —
                                    # drop it and let the worker pick its own default.
                                    effective_model = None
                                else:
                                    effective_model = task_model
                                effective_provider = (
                                    requested_provider
                                    if requested_provider is not None
                                    else task_provider
                                    if task_provider is not None
                                    else followup_worker_provider
                                )
                                # Pi has no authenticating built-in default — fall
                                # back to the guild's configured pi default (#1040).
                                if effective_tool == "pi" and (
                                    effective_provider is None or effective_model is None
                                ):
                                    effective_provider, effective_model = _apply_pi_defaults(
                                        effective_tool,
                                        effective_provider,
                                        effective_model,
                                        await _guild_foreman_config(db, guild_pk),
                                    )

                                from util.model_tiers import (  # noqa: PLC0415
                                    select_model_tier as _select_tier,
                                )

                                if requested_tier:
                                    effective_tier = _select_tier(
                                        "followup", complexity_hint=requested_tier
                                    )
                                elif requested_tool and requested_tool != task_tool:
                                    effective_tier = _select_tier("followup")
                                else:
                                    effective_tier = task_model_tier or _select_tier("followup")

                                if (
                                    requested_tier
                                    and requested_model is None
                                    and effective_provider
                                    and not is_error
                                ):
                                    from util.model_tiers import (  # noqa: PLC0415
                                        get_model_for_tier as _get_model_for_tier,
                                    )
                                    from util.models_dev import (  # noqa: PLC0415
                                        get_providers_from_db as _get_providers_from_db,
                                    )

                                    _catalog = await _get_providers_from_db(db)
                                    effective_model = _get_model_for_tier(
                                        effective_tier, effective_provider, _catalog
                                    )
                                if effective_model and effective_provider:
                                    from models import ModelCatalog  # noqa: PLC0415

                                    catalog_check = await db.exec(
                                        select(col(ModelCatalog.model_id)).where(
                                            col(ModelCatalog.provider) == effective_provider,
                                            col(ModelCatalog.model_id) == effective_model,
                                        )
                                    )
                                    if catalog_check.one_or_none() is None:
                                        result_text = (
                                            f"Model {effective_model!r} is not available for "
                                            f"provider {effective_provider!r}. Use GET "
                                            "/api/models to see available models for each provider."
                                        )
                                        is_error = True
                                # Validation matches on the short models.dev ID, so
                                # swap to the Bedrock inference-profile ARN only after
                                # it passes (no-op for non-Bedrock providers).
                                if not is_error:
                                    effective_model = await _resolve_bedrock_model_id(
                                        db, effective_provider, effective_model
                                    )

                            if not is_error:
                                # Atomically acquire the follow-up lock to prevent two
                                # concurrent foreman runs from both dispatching a worker.
                                lock_id = "".join(
                                    random.choices(string.ascii_lowercase + string.digits, k=8)
                                )
                                lock_acquired = await LockService(db).acquire(
                                    f"task:{task_id}", owner=lock_id
                                )
                                await db.commit()
                                if not lock_acquired:
                                    # Task already locked by a concurrent follow-up —
                                    # queue this request for replay when the lock releases.
                                    db.add(
                                        TaskEvent(
                                            task_id=task_id,
                                            event_type="pending-followup",
                                            payload_json=json.dumps(
                                                {
                                                    "instructions": instructions,
                                                    "preferred_worker_id": preferred_worker_id,
                                                    "tool": requested_tool,
                                                    "model": requested_model,
                                                    "tier": requested_tier,
                                                    "provider": requested_provider,
                                                }
                                            ),
                                            created_at=datetime.now(UTC),
                                        )
                                    )
                                    await db.commit()
                                    result_text = (
                                        f"Task {task_id} is locked by an in-progress follow-up. "
                                        "Instructions have been queued and will be passed to the "
                                        "foreman when the current follow-up completes."
                                    )
                                else:
                                    update_vals: dict = {
                                        "state": "working",
                                        "phase": "followup",
                                        "worker_id": target_worker_id,
                                        "tool": effective_tool,
                                        "model": effective_model,
                                        "model_tier": effective_tier,
                                        "provider": effective_provider,
                                    }
                                    if prior_state in ("done", "failed", "cancelled"):
                                        # Re-opening a terminal task: clear soft-delete so it
                                        # reappears in the live task list and isn't auto-purged.
                                        update_vals["deleted_at"] = None
                                    await db.exec(
                                        update(Task)
                                        .where(col(Task.id) == task_id)
                                        .values(**update_vals)
                                    )
                                    await db.commit()
                                    await broadcast(
                                        guild_id,
                                        TaskUpdateMsg(
                                            taskId=task_id,
                                            state="working",
                                            workerId=target_worker_id,
                                            deletedAt=None,
                                        ).model_dump(by_alias=True, exclude_none=True),
                                    )
                                    # Only resume the prior agent session when the follow-up
                                    # stays on the same worker — a different machine won't
                                    # have the session data on disk (see _select_followup_worker).
                                    followup_session_id = (
                                        task_session_id
                                        if target_worker_id == original_worker_id
                                        else None
                                    )
                                    await broadcast(
                                        guild_id,
                                        TaskFollowupMsg(
                                            workerId=target_worker_id,
                                            taskId=task_id,
                                            name=task_name or "",
                                            description=task_desc or "",
                                            tool=effective_tool,
                                            model=effective_model,
                                            modelTier=effective_tier,
                                            provider=effective_provider,
                                            branch=branch,
                                            instructions=instructions,
                                            issueNumber=task_issue_number,
                                            issueRepo=task_issue_repo,
                                            sessionId=followup_session_id,
                                        ).model_dump(by_alias=True, exclude_none=True),
                                    )
                                    spawn(
                                        notify_discord_followup(
                                            task_issue_repo,
                                            task_issue_number,
                                            task_id,
                                            instructions,
                                        ),
                                        name=f"discord.followup:{task_id}",
                                    )
                                    if (
                                        target_worker_id != original_worker_id
                                        and original_worker_id
                                    ):
                                        result_text = (
                                            f"Follow-up reassigned from {original_worker_id} "
                                            f"to {target_worker_id} (task {task_id} on branch {branch})."
                                        )
                                    else:
                                        result_text = (
                                            f"Follow-up sent to {target_worker_id} for task {task_id} "
                                            f"on branch {branch}."
                                        )

            elif tu.name == "finalize_task":
                task_id = inp["task_id"]
                raw_outcome = inp.get("outcome", "done")
                outcome = raw_outcome if raw_outcome in ("done", "failed") else "done"
                if outcome != raw_outcome:
                    logger.warning(
                        "finalize_task: unknown outcome %r, defaulting to 'done'", raw_outcome
                    )
                _blocked = _task_mutation_blocked(guild_id, task_id, own_task_id)
                if _blocked:
                    result_text = _blocked
                    is_error = True
                else:
                    result = await db.exec(
                        select(Task)
                        .where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
                        .with_for_update()
                    )
                    task = result.one_or_none()
                    if not task:
                        result_text = f"Task {task_id} not found."
                    else:
                        deleted_at = finalize_soft_delete_at(
                            outcome, task.issue_number, task.issue_state, task.phase
                        )
                        await db.exec(
                            update(Task)
                            .where(col(Task.id) == task_id)
                            .values(
                                state=outcome,
                                deleted_at=deleted_at,
                            )
                        )
                        # Discard any queued follow-up events — the task is closed.
                        await db.exec(delete(TaskEvent).where(col(TaskEvent.task_id) == task_id))
                        await LockService(db).release(f"task:{task_id}")

                        # phase='issue' root tasks own an entire GitHub issue's worth of
                        # work — cascade the soft-delete to descendants that already
                        # finished, but never force-close in-progress/pending ones (a
                        # human decides whether to cancel in-flight work).
                        descendants: list[Task] = []
                        if task.phase == "issue":
                            descendants = await find_descendant_tasks(db, task_id)
                            await cascade_soft_delete_terminal_descendants(
                                db, descendants, deleted_at
                            )

                        await db.commit()
                        await broadcast_msg(
                            guild_id,
                            TaskFinalizeMsg(workerId=task.worker_id, taskId=task_id),
                        )
                        await broadcast_msg(
                            guild_id,
                            TaskUpdateMsg(
                                taskId=task_id,
                                state=outcome,
                                deletedAt=deleted_at.isoformat()
                                if deleted_at is not None
                                else None,
                            ),
                        )
                        if (
                            task.phase == "issue"
                            and task.issue_repo
                            and task.issue_number is not None
                        ):
                            await post_issue_close_summary_comment(
                                guild_id, task.issue_repo, task.issue_number, descendants
                            )
                        if outcome == "failed":
                            spawn(
                                notify_discord_task_finalized(
                                    task.issue_repo, task.issue_number, task_id, "failed"
                                ),
                                name=f"discord.finalize:{task_id}",
                            )
                        result_text = f"Task {task_id} finalized as {outcome}; " + (
                            f"soft-deleted at {deleted_at.isoformat()}."
                            if deleted_at is not None
                            else "kept live until its issue closes."
                        )

            elif tu.name == "message_worker":
                wid = inp["worker_id"]
                msg = inp["message"]
                await emit_terminal_line(guild_id, wid, f"[foreman] {msg}")
                await broadcast_msg(guild_id, WorkerMessageMsg(workerId=wid, message=msg))
                result_text = f"Message delivered to {wid}."

            elif tu.name == "redirect_task":
                task_id = inp["task_id"]
                instructions = inp["instructions"]
                _blocked = _task_mutation_blocked(guild_id, task_id, own_task_id)
                if _blocked:
                    result_text = _blocked
                    is_error = True
                else:
                    result = await db.exec(
                        select(
                            col(Task.worker_id),
                            col(Task.state),
                            col(Task.issue_number),
                            col(Task.issue_repo),
                        ).where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
                    )
                    row = result.one_or_none()
                    if not row:
                        result_text = f"Task {task_id} not found."
                    else:
                        worker_id_val, state, redirect_issue_number, redirect_issue_repo = row
                        if state in ("done", "failed", "cancelled"):
                            result_text = f"Task {task_id} is {state} — cannot redirect."
                        else:
                            await db.exec(
                                update(Task).where(col(Task.id) == task_id).values(state="working")
                            )
                            await db.commit()
                            await broadcast_msg(
                                guild_id,
                                TaskRedirectMsg(
                                    workerId=worker_id_val,
                                    taskId=task_id,
                                    instructions=instructions,
                                ),
                            )
                            await broadcast_msg(
                                guild_id, TaskUpdateMsg(taskId=task_id, state="working")
                            )
                            if redirect_issue_number is not None and redirect_issue_repo:
                                spawn(
                                    notify_discord_redirect(
                                        redirect_issue_repo,
                                        redirect_issue_number,
                                        task_id,
                                        instructions,
                                    ),
                                    name=f"discord.redirect:{task_id}",
                                )
                            result_text = f"Redirect sent to {worker_id_val} for task {task_id}."

            elif tu.name == "cancel_task":
                task_id = inp["task_id"]
                reason = inp.get("reason", "")
                _blocked = _task_mutation_blocked(guild_id, task_id, own_task_id)
                if _blocked:
                    result_text = _blocked
                    is_error = True
                else:
                    result = await db.exec(
                        select(
                            col(Task.worker_id),
                            col(Task.state),
                            col(Task.issue_repo),
                            col(Task.issue_number),
                        ).where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
                    )
                    row = result.one_or_none()
                    if not row:
                        result_text = f"Task {task_id} not found."
                    else:
                        worker_id_val, state, cancel_issue_repo, cancel_issue_number = row
                        if state in ("done", "failed", "cancelled"):
                            result_text = f"Task {task_id} is already {state}."
                        else:
                            deleted_at = datetime.now(UTC)
                            await db.exec(
                                update(Task)
                                .where(col(Task.id) == task_id)
                                .values(
                                    state="cancelled",
                                    deleted_at=deleted_at,
                                )
                            )
                            await LockService(db).release(f"task:{task_id}")
                            await db.commit()
                            await broadcast_msg(
                                guild_id,
                                TaskCancelMsg(workerId=worker_id_val, taskId=task_id),
                            )
                            await broadcast_msg(
                                guild_id,
                                TaskUpdateMsg(
                                    taskId=task_id,
                                    state="cancelled",
                                    deletedAt=deleted_at.isoformat(),
                                ),
                            )
                            spawn(
                                notify_discord_task_finalized(
                                    cancel_issue_repo,
                                    cancel_issue_number,
                                    task_id,
                                    "cancelled",
                                    reason=reason or None,
                                ),
                                name=f"discord.cancel:{task_id}",
                            )
                            result_text = f"Task {task_id} cancelled." + (
                                f" Reason: {reason}" if reason else ""
                            )

            elif tu.name == "shutdown_worker":
                wid = inp["worker_id"]
                reason = inp.get("reason", "")
                worker_result = await db.exec(
                    select(col(Worker.id)).where(
                        col(Worker.id) == wid, col(Worker.guild_id) == guild_pk
                    )
                )
                if worker_result.one_or_none() is None:
                    result_text = f"Worker {wid} not found."
                else:
                    await broadcast_msg(
                        guild_id, WorkerShutdownMsg(workerId=wid, reason=reason or None)
                    )
                    await db.exec(update(Worker).where(col(Worker.id) == wid).values(disabled=True))
                    await db.commit()
                    # Graceful signal only: give the worker a chance to finish any
                    # in-progress task and exit on its own. Force-kill its container
                    # only if it's still not offline after the timeout — see
                    # worker_lifecycle.force_kill_worker_if_unresponsive.
                    from worker_lifecycle import (  # noqa: PLC0415
                        force_kill_worker_if_unresponsive as _force_kill_if_unresponsive,
                    )

                    spawn(
                        _force_kill_if_unresponsive(wid),
                        name=f"shutdown-escalate:{wid}",
                    )
                    result_text = f"Shutdown signal sent to {wid}." + (
                        f" Reason: {reason}" if reason else ""
                    )

            elif tu.name == "spawn_worker":
                # user_id here is whoever's foreman session is running this tool
                # call — for periodic-check/other automated triggers, runner.py's
                # _run_foreman_ai resolves it to the guild owner (see
                # _get_guild_user_id) rather than leaving it unset. Passing it
                # through lets resolve_spawn() apply that user's spawn_settings
                # override layer (on top of the guild baseline, which resolves
                # off guild_pk regardless) and stamps Worker.user_id so the
                # spawned worker is attributed instead of orphaned.
                result_text, is_error = await spawn_worker(
                    inp, guild_id, guild_pk, db, user_id=user_id
                )

            elif tu.name == "get_task_status":
                task_id = inp["task_id"]
                limit = min(int(inp.get("log_lines", 10)), 50)
                task_result = await db.exec(
                    select(Task).where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
                )
                task = task_result.one_or_none()
                if not task:
                    result_text = f"Task {task_id} not found."
                else:
                    agent_info = None
                    if task.worker_id:
                        agent_result = await db.exec(
                            select(col(Agent.id), col(Agent.state))
                            .where(
                                col(Agent.worker_id) == task.worker_id,
                                col(Agent.state) != "offline",
                            )
                            .limit(1)
                        )
                        agent_row = agent_result.one_or_none()
                        if agent_row:
                            agent_info = {"agent_id": agent_row[0], "agent_state": agent_row[1]}
                    logs_result = await db.exec(
                        select(col(TaskLog.timestamp), col(TaskLog.line), col(TaskLog.data))
                        .where(col(TaskLog.task_id) == task_id)
                        .order_by(col(TaskLog.id).desc())
                        .limit(limit)
                    )
                    log_rows = list(reversed(logs_result.all()))
                    recent_logs = []
                    for time_, line, data_json in log_rows:
                        entry: dict = {"time": time_, "line": line}
                        full = _full_log_content(data_json)
                        if full and full != line:
                            entry["data"] = truncate_tool_result(full)
                        recent_logs.append(entry)
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
                            "deleted_at": task.deleted_at,
                            "recent_logs": recent_logs,
                        },
                        default=_json_default,
                    )

            elif tu.name == "message_discord_bot":
                result_text, is_error = await _message_discord_bot(inp, db)
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
            "get_pr_status",
            "review_pr_internal",
            "analyze_epic",
        ):
            logger.info("Executing GitHub tool %s with input %s", tu.name, inp)
            creds = await _guild_github_token(guild_id, user_id)
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
                        issues = await _to_thread(
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
                        issue = await _to_thread(_gh_api, f"/repos/{repo}/issues/{num}", token)
                        comments_raw = await _to_thread(
                            _gh_api, f"/repos/{repo}/issues/{num}/comments?per_page=20", token
                        )
                        # Native GitHub sub-issues (parenting), not the body checklist.
                        # 404/empty for issues without children — treat any failure as none.
                        try:
                            sub_raw = await _to_thread(
                                _gh_api,
                                f"/repos/{repo}/issues/{num}/sub_issues?per_page=100",
                                token,
                            )
                        except urllib.error.HTTPError:
                            sub_raw = []
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
                                "sub_issues": [
                                    {
                                        "number": s["number"],
                                        "title": s["title"],
                                        "state": s["state"],
                                        "assignees": [a["login"] for a in s.get("assignees", [])],
                                    }
                                    for s in sub_raw
                                ],
                            }
                        )

                    elif tu.name == "list_github_prs":
                        repo = inp["repo"]
                        state = inp.get("state", "open")
                        prs = await _to_thread(
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
                        await _to_thread(
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
                        issue = await _to_thread(
                            _gh_api_post, f"/repos/{repo}/issues", token, payload
                        )
                        result_text = json.dumps(
                            {
                                "number": issue["number"],
                                "url": issue["html_url"],
                                "title": issue["title"],
                            }
                        )

                    elif tu.name == "get_pr_status":
                        repo = inp["repo"]
                        num = int(inp["pr_number"])
                        result_text = json.dumps(await fetch_pr_status(repo, num, token))

                    elif tu.name == "search_github_issues":
                        repo = inp["repo"]
                        query = inp["query"]
                        state = inp.get("state", "open")
                        state_q = "" if state == "all" else f"+state:{state}"
                        search_url = (
                            f"/search/issues?q={urllib.parse.quote(query)}"
                            f"+repo:{repo}{state_q}&per_page=10&sort=created&order=desc"
                        )
                        data = await _to_thread(_gh_api, search_url, token)
                        items = data.get("items", []) if isinstance(data, dict) else data
                        result_text = json.dumps(
                            [
                                {
                                    "number": i["number"],
                                    "title": i["title"],
                                    "state": i["state"],
                                    "url": i["html_url"],
                                    "labels": [l["name"] for l in i.get("labels", [])],
                                    "assignees": [a["login"] for a in i.get("assignees", [])],
                                }
                                for i in items
                            ]
                        )

                    elif tu.name == "review_pr_internal":
                        # Review action policy (mirrors the worker-driven `gh pr review` path):
                        #   APPROVE           - functionally correct; issues are minor nits
                        #                       (style, naming, formatting). Note nits inline.
                        #   COMMENT           - moderate concerns (performance, clarity) that
                        #                       don't block merging.
                        #   REQUEST_CHANGES   - genuine bugs, security issues, or logic errors
                        #                       only. Be specific and firm about what breaks and
                        #                       why it must be fixed before merge. Never for
                        #                       style preferences.
                        # Tone: polite but firm. Never apologetic about calling out a real bug.
                        # Never blocking a merge over style.
                        #
                        # An explicit `action` input overrides the tool's own judgement; when
                        # omitted, the verdict comes from the diff analysis below and is biased
                        # toward APPROVE per the policy above.
                        pr_url = inp["pr_url"]
                        explicit_action = inp.get("action")
                        if explicit_action:
                            explicit_action = explicit_action.upper()
                            if explicit_action not in ("APPROVE", "REQUEST_CHANGES", "COMMENT"):
                                explicit_action = None
                        pr_match = _PR_URL_RE.match(pr_url.rstrip("/"))
                        if not pr_match:
                            result_text = (
                                f"Invalid GitHub PR URL: {pr_url!r}. "
                                "Expected https://github.com/owner/repo/pull/N"
                            )
                            is_error = True
                        else:
                            pr_repo = pr_match.group(1)
                            pr_number = int(pr_match.group(2))
                            pr_data, diff_text = await asyncio.gather(
                                _to_thread(
                                    _gh_api,
                                    f"/repos/{pr_repo}/pulls/{pr_number}",
                                    token,
                                ),
                                _to_thread(
                                    _gh_api_diff,
                                    f"/repos/{pr_repo}/pulls/{pr_number}",
                                    token,
                                ),
                            )
                            pr_title = pr_data.get("title", "")
                            pr_body_text = pr_data.get("body") or "(no description)"
                            base_ref = (pr_data.get("base") or {}).get("ref", "")
                            head_ref = (pr_data.get("head") or {}).get("ref", "")

                            try:
                                # Use the guild's configured provider/model (Bedrock,
                                # env-var overrides, ...) instead of always defaulting
                                # to the direct Anthropic API — same resolution the main
                                # foreman loop uses for every other inference call.
                                from foreman.runner import (
                                    _call_llm,
                                    _load_foreman_config,
                                    resolve_foreman_client,
                                )

                                guild_cfg = await _load_foreman_config(guild_id)
                                (
                                    client,
                                    effective_provider,
                                    review_model,
                                ) = await resolve_foreman_client(guild_id, guild_cfg)
                                review_prompt = (
                                    "You are a thorough but fair code reviewer. Review the "
                                    "following GitHub pull request and provide structured "
                                    "feedback.\n\n"
                                    f"PR: {pr_title}\n"
                                    f"Base: {base_ref} ← Head: {head_ref}\n"
                                    f"Description: {pr_body_text[:1000]}\n\n"
                                    f"Diff (up to 40 000 chars):\n{diff_text[:40000]}\n\n"
                                    "Respond with a JSON object only (no markdown fences) "
                                    "with exactly these fields:\n"
                                    '{"verdict": "APPROVE|REQUEST_CHANGES|COMMENT", '
                                    '"summary": "3-5 markdown bullet points (use - prefix)", '
                                    '"comments": [{"path": "file.py", "line": 42, '
                                    '"side": "RIGHT", "body": "concise comment"}]}\n\n'
                                    "Verdict policy — bias toward APPROVE:\n"
                                    "- APPROVE: the code is functionally correct and any issues "
                                    "are minor nits (style, naming, formatting). Note the nits as "
                                    "inline comments but still approve.\n"
                                    "- COMMENT: moderate concerns (performance, clarity) that "
                                    "don't block merging.\n"
                                    "- REQUEST_CHANGES: reserved for genuine bugs, security "
                                    "issues, or logic errors that must be fixed before merge. "
                                    "Never use this for style preferences alone.\n\n"
                                    "Rules:\n"
                                    "- summary: 3-5 bullet points covering key findings, written "
                                    "in a polite, constructive tone. Be firm and specific when "
                                    "flagging a real bug (explain what breaks and why it must be "
                                    "fixed before merge) — never apologetic about it. Don't be "
                                    "pedantic or block merging over style.\n"
                                    "- comments: 0-5 objects for the most important issues\n"
                                    "- line: line number in the NEW file version (RIGHT side)\n"
                                    "- Only comment on lines present in the diff\n"
                                    "- Focus on bugs, security issues, and significant design problems\n"
                                    "- Keep each comment concise (1-3 sentences)"
                                )
                                llm_result = await _call_llm(
                                    guild_id,
                                    client=client,
                                    provider=effective_provider,
                                    model=review_model,
                                    max_tokens=2048,
                                    system_blocks=[],
                                    messages=[{"role": "user", "content": review_prompt}],
                                    tools=[],
                                )
                                review_json = _parse_review_from_claude(
                                    llm_result.response.content[0].text
                                )
                            except Exception as exc:
                                logger.error(
                                    "guild=%s review_pr_internal: ai generation failed: %s",
                                    guild_id,
                                    exc,
                                    exc_info=True,
                                )
                                review_json = {
                                    "verdict": "COMMENT",
                                    "summary": (
                                        "Review could not be generated by the AI agent "
                                        f"({type(exc).__name__}: {exc})."
                                    ),
                                    "comments": [],
                                }

                            summary_text = review_json.get("summary", "(no summary)")
                            raw_comments = review_json.get("comments") or []
                            gh_comments = [
                                {
                                    "path": c["path"],
                                    "line": int(c["line"]),
                                    "side": c.get("side", "RIGHT"),
                                    "body": c["body"],
                                }
                                for c in raw_comments
                                if c.get("path") and c.get("line") and c.get("body")
                            ]

                            recommended_verdict = str(
                                review_json.get("verdict") or "COMMENT"
                            ).upper()
                            if recommended_verdict not in (
                                "APPROVE",
                                "REQUEST_CHANGES",
                                "COMMENT",
                            ):
                                recommended_verdict = "COMMENT"
                            action = explicit_action or recommended_verdict
                            logger.info(
                                "guild=%s review_pr_internal: pr_url=%s action=%s"
                                " (explicit=%s recommended=%s)",
                                guild_id,
                                pr_url,
                                action,
                                bool(explicit_action),
                                recommended_verdict,
                            )

                            try:
                                threads_resolved = await _supersede_prior_bot_reviews(
                                    pr_repo, pr_number, username, token
                                )
                                if threads_resolved:
                                    logger.info(
                                        "guild=%s review_pr_internal: resolved %d thread(s)"
                                        " from prior review(s) on %s#%d",
                                        guild_id,
                                        threads_resolved,
                                        pr_repo,
                                        pr_number,
                                    )
                            except Exception as _sup_exc:
                                logger.warning(
                                    "guild=%s review_pr_internal: thread resolution step failed"
                                    " (non-fatal): %s",
                                    guild_id,
                                    _sup_exc,
                                )
                            try:
                                review_data = await _to_thread(
                                    _gh_api_post,
                                    f"/repos/{pr_repo}/pulls/{pr_number}/reviews",
                                    token,
                                    {
                                        "body": summary_text,
                                        "event": action,
                                        "comments": gh_comments,
                                    },
                                )
                            except urllib.error.HTTPError:
                                logger.warning(
                                    "guild=%s review_pr_internal: inline comments rejected, "
                                    "retrying without them",
                                    guild_id,
                                )
                                review_data = await _to_thread(
                                    _gh_api_post,
                                    f"/repos/{pr_repo}/pulls/{pr_number}/reviews",
                                    token,
                                    {"body": summary_text, "event": action, "comments": []},
                                )
                                gh_comments = []

                            logger.info(
                                "guild=%s review_pr_internal: review=%s verdict=%s comments=%d",
                                guild_id,
                                review_data.get("id"),
                                action,
                                len(gh_comments),
                            )
                            result_text = json.dumps(
                                {
                                    "pr_url": pr_url,
                                    "verdict": action,
                                    "review_id": review_data.get("id"),
                                    "review_posted": True,
                                    "inline_comments_posted": len(gh_comments),
                                    "summary": summary_text[:400],
                                }
                            )

                    elif tu.name == "analyze_epic":
                        repo = inp["repo"]
                        num = int(inp["issue_number"])
                        force = inp.get("force", False)
                        file_gap_issues = inp.get("file_gap_issues", False)
                        trigger_deep = inp.get("trigger_deep_analysis", False)

                        # Fetch the epic issue
                        epic = await _to_thread(_gh_api, f"/repos/{repo}/issues/{num}", token)
                        epic_labels = [l["name"] for l in epic.get("labels", [])]

                        # Safeguard: skip if already reported (unless forced)
                        if "pm-reported" in epic_labels and not force:
                            # Check if report was recent (within 7 days)
                            comments_raw = await _to_thread(
                                _gh_api,
                                f"/repos/{repo}/issues/{num}/comments?per_page=50",
                                token,
                            )
                            recent_report = False
                            for c in reversed(comments_raw):
                                if c.get("body", "").startswith("## 📋 Epic Status Summary"):
                                    created = c.get("created_at", "")
                                    if created:
                                        from datetime import datetime as dt

                                        report_time = dt.fromisoformat(
                                            created.replace("Z", "+00:00")
                                        )
                                        if (datetime.now(UTC) - report_time) < timedelta(days=7):
                                            recent_report = True
                                    break
                            if recent_report:
                                result_text = json.dumps(
                                    {
                                        "skipped": True,
                                        "reason": "Report already posted within the last 7 days. Use force=true to override.",
                                        "epic": num,
                                    }
                                )
                                # Skip to end — result_text is already set
                            else:
                                # Label exists but report is stale — proceed
                                force = True

                        if not ("pm-reported" in epic_labels and not force):
                            # --- Level 1: Lightweight status fetch ---
                            # Fetch sub-issues
                            try:
                                sub_raw = await _to_thread(
                                    _gh_api,
                                    f"/repos/{repo}/issues/{num}/sub_issues?per_page=100",
                                    token,
                                )
                            except urllib.error.HTTPError:
                                sub_raw = []

                            # Fetch linked PRs (single search, no per-sub-issue queries)
                            mention_q = urllib.parse.quote(f"repo:{repo} is:pr #{num}")
                            try:
                                mention_prs_data = await _to_thread(
                                    _gh_api,
                                    f"/search/issues?q={mention_q}&per_page=30",
                                    token,
                                )
                                all_prs = mention_prs_data.get("items", [])
                            except urllib.error.HTTPError:
                                all_prs = []

                            # Build lightweight sub-issue summary (no per-issue API calls)
                            sub_issue_details = []
                            for sub in sub_raw:
                                sub_detail = {
                                    "number": sub["number"],
                                    "title": sub.get("title", ""),
                                    "state": sub.get("state", "unknown"),
                                    "labels": [l["name"] for l in sub.get("labels", [])],
                                }
                                sub_issue_details.append(sub_detail)

                            # Analyze completeness
                            total_subs = len(sub_issue_details)
                            closed_subs = sum(
                                1 for s in sub_issue_details if s["state"] == "closed"
                            )
                            open_subs = total_subs - closed_subs

                            # Identify basic inconsistencies (no API calls needed)
                            gaps: list = []
                            for s in sub_issue_details:
                                # Closed issue — note it for potential verification
                                if s["state"] == "closed" and not any(
                                    f"#{s['number']}" in (p.get("title", "") + p.get("body", ""))
                                    for p in all_prs
                                ):
                                    gaps.append(
                                        f"Sub-issue #{s['number']} ({s['title']}) is closed but no PR references it"
                                    )

                            # Determine if deep analysis is recommended
                            recommend_deep = len(gaps) >= 2 or (
                                total_subs > 0 and open_subs == 0 and len(all_prs) > 0
                            )

                            # Build concise status summary
                            pct = int((closed_subs / total_subs) * 100) if total_subs > 0 else 0
                            report_lines = [
                                "## 📋 Epic Status Summary",
                                "",
                                f"**Epic:** #{num} — {epic.get('title', '')}",
                                f"**Checked:** {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
                                "",
                                f"### Progress: {pct}%",
                                f"- **Sub-issues:** {total_subs} total — {closed_subs} closed, {open_subs} open",
                                f"- **Related PRs:** {len(all_prs)} found",
                            ]

                            if gaps:
                                report_lines.append(f"- **Potential gaps:** {len(gaps)}")
                                report_lines.append("")
                                report_lines.append("### ⚠️ Gaps")
                                for g in gaps[:10]:
                                    report_lines.append(f"- {g}")
                            else:
                                report_lines.append("- **Gaps:** None detected")

                            report_lines.append("")
                            if total_subs > 0 and open_subs == 0:
                                report_lines.append(
                                    "✅ All sub-issues closed. Epic may be ready to finalize."
                                )
                            report_lines.append("")
                            report_lines.append(
                                "*Level 1 status check. Use `trigger_deep_analysis` for "
                                "code-level review.*"
                            )

                            report_body = "\n".join(report_lines)

                            # Post the status summary as a comment
                            await _to_thread(
                                _gh_api_post,
                                f"/repos/{repo}/issues/{num}/comments",
                                token,
                                {"body": report_body},
                            )

                            # Add pm-reported label (create if needed)
                            try:
                                await _to_thread(
                                    _gh_api_post,
                                    f"/repos/{repo}/issues/{num}/labels",
                                    token,
                                    {"labels": ["pm-reported"]},
                                )
                            except urllib.error.HTTPError:
                                pass  # label may already exist or lack permission

                            # File gap issues if requested
                            filed_issues: list = []
                            if file_gap_issues and gaps:
                                for gap in gaps[:5]:  # cap at 5 new issues
                                    gap_payload = {
                                        "title": f"[Epic #{num}] {gap[:80]}",
                                        "body": (
                                            f"Identified during epic analysis of #{num}.\n\n"
                                            f"**Gap:** {gap}\n\n"
                                            f"Parent epic: #{num}"
                                        ),
                                        "labels": ["bug", "epic-gap"],
                                    }
                                    try:
                                        new_issue = await _to_thread(
                                            _gh_api_post,
                                            f"/repos/{repo}/issues",
                                            token,
                                            gap_payload,
                                        )
                                        filed_issues.append(
                                            {
                                                "number": new_issue["number"],
                                                "title": new_issue["title"],
                                                "url": new_issue["html_url"],
                                            }
                                        )
                                    except urllib.error.HTTPError as exc:
                                        logger.warning(
                                            "analyze_epic: failed to file gap issue: %s",
                                            exc,
                                        )

                            # Build deep analysis task description if triggered
                            deep_analysis_task_id = None
                            if trigger_deep and recommend_deep:
                                # Level 2: Create a worker task for deep code analysis
                                deep_desc = (
                                    f"Deep code analysis for epic #{num} in {repo}.\n\n"
                                    f"## Context\n"
                                    f"Epic: {epic.get('title', '')}\n"
                                    f"Sub-issues: {total_subs} ({closed_subs} closed, {open_subs} open)\n"
                                    f"Related PRs: {', '.join('#' + str(p['number']) for p in all_prs[:15])}\n\n"
                                    f"## Instructions\n"
                                    f"1. Check out each related PR branch and review the code changes\n"
                                    f"2. Compare implementations against the requirements in each sub-issue\n"
                                    f"3. Identify: bugs, missing functionality, architectural issues, "
                                    f"test gaps\n"
                                    f"4. Post detailed findings as a comment on epic #{num}\n"
                                    f"5. If bugs are found, file individual issues with 'bug' label\n\n"
                                    f"## Gaps already identified (Level 1)\n"
                                )
                                for g in gaps:
                                    deep_desc += f"- {g}\n"

                                # Store the description for the foreman to use with assign_task
                                # We return it in the result so the foreman can create+assign
                                deep_analysis_task_id = "pending_assignment"

                            result_text = json.dumps(
                                {
                                    "epic": num,
                                    "repo": repo,
                                    "level": 1,
                                    "report_posted": True,
                                    "sub_issues_total": total_subs,
                                    "sub_issues_closed": closed_subs,
                                    "sub_issues_open": open_subs,
                                    "related_prs": len(all_prs),
                                    "gaps_found": len(gaps),
                                    "gaps": gaps[:10],
                                    "issues_filed": filed_issues,
                                    "all_complete": total_subs > 0 and open_subs == 0,
                                    "recommend_deep_analysis": recommend_deep,
                                    "deep_analysis_triggered": deep_analysis_task_id is not None,
                                    "deep_analysis_description": (
                                        deep_desc if deep_analysis_task_id else None
                                    ),
                                }
                            )

                except urllib.error.HTTPError as exc:
                    result_text = f"GitHub API error: {exc.code} {exc.reason}"
                    is_error = True
                except Exception as exc:
                    result_text = f"GitHub error: {exc}"
                    is_error = True

        # DNSid verification tools — signing is intentionally application-owned.
        if tu.name == "dnsid":
            logger.info("dnsid tool: input=%s", inp)
            command = inp.get("command", "")
            if not command:
                result_text = "dnsid requires command (resolve or verify)"
                is_error = True
            else:
                try:
                    result_text = json.dumps(await _run_dnsid(command, inp))
                except (ValueError, RuntimeError) as exc:
                    result_text = str(exc)
                    is_error = True
                except Exception as exc:
                    result_text = f"dnsid {command} failed: {exc}"
                    is_error = True

        # A2A agent call — no GitHub token or DB required
        if tu.name == "call_agent":
            logger.info("call_agent: input=%s", inp)
            agent_url = (inp.get("agent_url") or "").rstrip("/")
            skill_id = inp.get("skill") or ""
            params = inp.get("params") or {}
            if not agent_url:
                result_text = "call_agent requires agent_url"
                is_error = True
            elif not skill_id:
                result_text = "call_agent requires skill"
                is_error = True
            else:
                try:
                    card_url = f"{agent_url}/.well-known/agent.json"
                    card = await _to_thread(_fetch_agent_card, card_url)
                    logger.debug("call_agent: fetched agent card from %s: %s", card_url, card)
                    skills = card.get("skills", [])
                    skill_ids = [s.get("id", "") for s in skills]
                    if skills and skill_id not in skill_ids:
                        result_text = (
                            f"Skill {skill_id!r} not found on agent at {agent_url}. "
                            f"Available skills: {', '.join(skill_ids)}"
                        )
                        is_error = True
                    else:
                        task_body = json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": "tasks/send",
                                "params": {
                                    "skill_id": skill_id,
                                    "message": {
                                        "parts": [{"type": "text", "text": json.dumps(params)}]
                                    },
                                },
                                "id": 1,
                            }
                        ).encode()
                        response = await _to_thread(
                            _post_agent_task,
                            f"{agent_url}/jsonrpc",
                            task_body,
                        )
                        result_text = json.dumps(
                            {
                                "agent_url": agent_url,
                                "skill": skill_id,
                                "agent_name": card.get("name", ""),
                                "response": response,
                            }
                        )
                except urllib.error.HTTPError as exc:
                    result_text = f"Agent HTTP error {exc.code}: {exc.reason}"
                    is_error = True
                except Exception as exc:
                    result_text = f"Agent call failed: {exc}"
                    is_error = True

    except Exception as exc:
        result_text = f"Tool {tu.name} failed: {exc}"
        is_error = True
    finally:
        _api_call_log = _api_calls_ctx.get([])
        _api_calls_ctx.reset(_ctx_token)

    # Surface GitHub request IDs in error responses so failures can be correlated
    # with provider-side logs without digging through the database.
    if is_error and _api_call_log:
        req_ids = [c["x_github_request_id"] for c in _api_call_log if c.get("x_github_request_id")]
        if req_ids:
            result_text = f"{result_text}\n[x-github-request-id: {', '.join(req_ids)}]"

    block: dict = {"type": "tool_result", "tool_use_id": tu.id, "content": result_text}
    if is_error:
        block["is_error"] = True
    if _api_call_log:
        block["api_calls"] = _api_call_log
    return block
