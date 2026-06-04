"""Debug endpoint: unified task timeline from all data sources.

Returns a single chronologically sorted list of everything that happened for a
given task: DB state transitions, worker logs, GitHub webhook events, Claude
usage records, and live GitHub API data (PR metadata, commits, reviews, checks,
comments) if the task has a ``pr_url``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from auth_deps import get_guild_pk, require_member
from database import get_db_dep
from fastapi import APIRouter, Depends, HTTPException
from models import ClaudeUsage, GithubEvent, GithubToken, Task, TaskLog
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from utils import row_to_dict

router = APIRouter()
logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_pr_url(pr_url: str) -> tuple[str, str, int] | None:
    """Return (owner, repo, pr_number) from a GitHub PR URL, or None."""
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def _iso(dt: datetime | None) -> str | None:
    """Return an ISO-8601 UTC string for *dt*, or None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _parse_ts(ts: str | None) -> datetime:
    """Parse an ISO-8601 timestamp string for sorting; unknown → far future."""
    if not ts:
        return datetime.max.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.max.replace(tzinfo=UTC)


async def _gh_get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> tuple[Any, str | None]:
    """GET *url* via *client*; return (data, error_string)."""
    try:
        r = await client.get(url, **kwargs)
        r.raise_for_status()
        return r.json(), None
    except httpx.HTTPStatusError as exc:
        return None, f"{url}: HTTP {exc.response.status_code}"
    except httpx.RequestError as exc:
        return None, f"{url}: {exc}"


async def _fetch_github_timeline(pr_url: str, token: str | None) -> tuple[list[dict], list[str]]:
    """Fetch live GitHub PR data; return (timeline_events, warnings)."""
    parsed = _parse_pr_url(pr_url)
    if not parsed:
        return [], [f"Could not parse PR URL: {pr_url}"]

    owner, repo, pr_number = parsed
    base_headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        base_headers["Authorization"] = f"Bearer {token}"

    events: list[dict] = []
    warnings: list[str] = []
    head_sha: str | None = None

    async with httpx.AsyncClient(headers=base_headers, timeout=15.0) as client:
        # PR metadata
        pr_data, err = await _gh_get(
            client, f"{_GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}"
        )
        if err:
            warnings.append(f"PR fetch failed: {err}")
        else:
            head_sha = (pr_data.get("head") or {}).get("sha")
            events.append(
                {
                    "timestamp": pr_data.get("created_at"),
                    "source": "github",
                    "event_type": "pr_opened",
                    "summary": f"PR #{pr_number} opened: {pr_data.get('title', '')}",
                    "detail": pr_data,
                }
            )
            if pr_data.get("merged_at"):
                merged_by = (pr_data.get("merged_by") or {}).get("login", "?")
                events.append(
                    {
                        "timestamp": pr_data["merged_at"],
                        "source": "github",
                        "event_type": "pr_merged",
                        "summary": f"PR #{pr_number} merged by {merged_by}",
                        "detail": {
                            "merged_at": pr_data["merged_at"],
                            "merge_commit_sha": pr_data.get("merge_commit_sha"),
                            "merged_by": pr_data.get("merged_by"),
                        },
                    }
                )
            elif pr_data.get("closed_at") and pr_data.get("state") == "closed":
                events.append(
                    {
                        "timestamp": pr_data["closed_at"],
                        "source": "github",
                        "event_type": "pr_closed",
                        "summary": f"PR #{pr_number} closed without merge",
                        "detail": {"closed_at": pr_data["closed_at"]},
                    }
                )

        # Commits on the PR branch
        commits, err = await _gh_get(
            client,
            f"{_GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/commits",
            params={"per_page": 100},
        )
        if err:
            warnings.append(f"Commits fetch failed: {err}")
        elif commits:
            for c in commits:
                commit_info = c.get("commit") or {}
                ts = (commit_info.get("committer") or {}).get("date") or (
                    commit_info.get("author") or {}
                ).get("date")
                msg = commit_info.get("message", "")
                first_line = msg.splitlines()[0] if msg else ""
                events.append(
                    {
                        "timestamp": ts,
                        "source": "github",
                        "event_type": "commit_pushed",
                        "summary": f"Commit {c.get('sha', '')[:7]}: {first_line[:100]}",
                        "detail": c,
                    }
                )

        # PR reviews
        reviews, err = await _gh_get(
            client, f"{_GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        )
        if err:
            warnings.append(f"Reviews fetch failed: {err}")
        elif reviews:
            for rev in reviews:
                login = (rev.get("user") or {}).get("login", "?")
                events.append(
                    {
                        "timestamp": rev.get("submitted_at"),
                        "source": "github",
                        "event_type": "pr_review",
                        "summary": f"Review by {login}: {rev.get('state', '')}",
                        "detail": rev,
                    }
                )

        # Check runs on head SHA
        if head_sha:
            check_data, err = await _gh_get(
                client,
                f"{_GITHUB_API}/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
                params={"per_page": 100},
            )
            if err:
                warnings.append(f"Check runs fetch failed: {err}")
            elif check_data:
                for cr in check_data.get("check_runs", []):
                    ts = cr.get("completed_at") or cr.get("started_at")
                    status = cr.get("status", "")
                    conclusion = cr.get("conclusion") or status
                    etype = "check_run_completed" if status == "completed" else "check_run_started"
                    events.append(
                        {
                            "timestamp": ts,
                            "source": "github",
                            "event_type": etype,
                            "summary": f"Check '{cr.get('name', '')}': {conclusion}",
                            "detail": cr,
                        }
                    )

        # Issue/PR comments
        comments, err = await _gh_get(
            client,
            f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments",
            params={"per_page": 100},
        )
        if err:
            warnings.append(f"Comments fetch failed: {err}")
        elif comments:
            for cm in comments:
                login = (cm.get("user") or {}).get("login", "?")
                body_preview = (cm.get("body") or "")[:100]
                events.append(
                    {
                        "timestamp": cm.get("created_at"),
                        "source": "github",
                        "event_type": "pr_comment",
                        "summary": f"Comment by {login}: {body_preview}",
                        "detail": cm,
                    }
                )

        # PR timeline events (requires mockingbird preview)
        tl_headers = {"Accept": "application/vnd.github.mockingbird-preview+json"}
        tl_events, err = await _gh_get(
            client,
            f"{_GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/timeline",
            headers=tl_headers,
            params={"per_page": 100},
        )
        if err:
            warnings.append(f"PR timeline fetch failed: {err}")
        elif tl_events:
            for ev in tl_events:
                ts = ev.get("created_at") or ev.get("submitted_at")
                if not ts:
                    continue
                etype = ev.get("event", "unknown")
                actor = (ev.get("actor") or {}).get("login", "")
                events.append(
                    {
                        "timestamp": ts,
                        "source": "github",
                        "event_type": etype,
                        "summary": f"Timeline: {etype}" + (f" by {actor}" if actor else ""),
                        "detail": ev,
                    }
                )

    return events, warnings


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/guilds/{guild_id}/tasks/{task_id}/debug")
async def get_task_debug_timeline(
    guild_id: str,
    task_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Unified debug timeline for a task.

    Aggregates all data sources — DB rows, worker logs, GitHub webhook events,
    Claude usage records, and live GitHub API data — into a single list sorted
    ascending by timestamp.

    Response shape::

        {
            "task": {<full task record>},
            "timeline": [
                {
                    "timestamp": "ISO-8601 | null",
                    "source": "db | worker | foreman | api | github",
                    "event_type": "task_created | log | pr_opened | ...",
                    "summary": "<human-readable one-liner>",
                    "detail": {<raw payload>}
                }
            ],
            "warnings": ["..."]   // optional; only present when GitHub API calls fail
        }
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    # Include soft-deleted rows so the debug view shows completed tasks.
    result = await db.exec(
        select(Task).where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
    )
    task = result.one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    timeline: list[dict] = []
    warnings: list[str] = []
    task_dict = row_to_dict(task)

    # ── 1. DB: task lifecycle ─────────────────────────────────────────────────
    timeline.append(
        {
            "timestamp": _iso(task.created_at),
            "source": "db",
            "event_type": "task_created",
            "summary": f"Task created: {(task.description or '')[:120]}",
            "detail": task_dict,
        }
    )
    if task.finished_at:
        timeline.append(
            {
                "timestamp": _iso(task.finished_at),
                "source": "db",
                "event_type": "task_finished",
                "summary": f"Task finished with state: {task.state}",
                "detail": {"state": task.state, "finished_at": _iso(task.finished_at)},
            }
        )

    # ── 2. Worker / agent logs ────────────────────────────────────────────────
    logs_result = await db.exec(
        select(TaskLog).where(col(TaskLog.task_id) == task_id).order_by(col(TaskLog.id).asc())
    )
    for log in logs_result.all():
        detail: dict[str, Any] = {
            "line": log.line,
            "worker_id": log.worker_id,
            "agent_id": log.agent_id,
            "level": log.level,
        }
        if log.data:
            try:
                detail["data"] = json.loads(log.data)
            except Exception:
                detail["data"] = log.data

        source = "worker"
        if log.level == "foreman":
            source = "foreman"
        elif log.level == "api":
            source = "api"

        timeline.append(
            {
                "timestamp": _iso(log.timestamp),
                "source": source,
                "event_type": log.level or "log",
                "summary": log.line[:200],
                "detail": detail,
            }
        )

    # ── 3. GitHub webhook events stored in DB ─────────────────────────────────
    gh_db_result = await db.exec(
        select(GithubEvent)
        .where(col(GithubEvent.task_id) == task_id)
        .order_by(col(GithubEvent.id).asc())
    )
    for ev in gh_db_result.all():
        etype = f"{ev.event_type}.{ev.action}" if ev.action else ev.event_type
        payload: dict = {}
        if ev.payload_json:
            try:
                payload = json.loads(ev.payload_json)
            except Exception:
                pass
        timeline.append(
            {
                "timestamp": _iso(ev.created_at),
                "source": "github",
                "event_type": etype,
                "summary": (
                    f"GitHub webhook: {etype} on {ev.repo}"
                    + (f" PR#{ev.pr_number}" if ev.pr_number else "")
                ),
                "detail": {
                    "delivery_id": ev.delivery_id,
                    "sender": ev.sender_login,
                    "payload": payload,
                },
            }
        )

    # ── 4. Claude usage / cost records ────────────────────────────────────────
    usage_result = await db.exec(
        select(ClaudeUsage)
        .where(col(ClaudeUsage.task_id) == task_id)
        .order_by(col(ClaudeUsage.id).asc())
    )
    for usage in usage_result.all():
        if usage.kind == "result":
            cost_str = f", cost ${usage.cost_usd:.4f}" if usage.cost_usd else ""
            summary = (
                f"Claude run complete: {usage.stop_reason or '?'}, "
                f"{usage.num_turns or 0} turns{cost_str}"
            )
        else:
            summary = (
                f"API call #{usage.call_index}: "
                f"{usage.input_tokens}in/{usage.output_tokens}out tokens"
            )
        timeline.append(
            {
                "timestamp": _iso(usage.created_at),
                "source": "worker",
                "event_type": f"usage_{usage.kind}",
                "summary": summary,
                "detail": row_to_dict(usage),
            }
        )

    # ── 5. Live GitHub API ────────────────────────────────────────────────────
    if task.pr_url:
        github_token: str | None = os.getenv("GITHUB_TOKEN")
        if not github_token:
            tok_result = await db.exec(
                select(col(GithubToken.access_token)).where(
                    col(GithubToken.github_user_id) == github_user_id
                )
            )
            github_token = tok_result.one_or_none()

        gh_events, gh_warnings = await _fetch_github_timeline(task.pr_url, github_token)
        timeline.extend(gh_events)
        warnings.extend(gh_warnings)

    # ── Sort ascending by timestamp (None / unparseable → end) ───────────────
    timeline.sort(key=lambda e: _parse_ts(e.get("timestamp")))

    response: dict[str, Any] = {"task": task_dict, "timeline": timeline}
    if warnings:
        response["warnings"] = warnings
    return response
