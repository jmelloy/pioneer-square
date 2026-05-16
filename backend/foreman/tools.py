"""Foreman tool definitions, GitHub API helpers, and tool-call executor."""

import asyncio
import json
import logging
import os
import random
import re
import string
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from database import get_db
from events import broadcast, emit_terminal_line
from models import Agent, GithubToken, GuildKey, GuildMember, Task, TaskLog, Worker
from sqlalchemy import select, update

logger = logging.getLogger(__name__)

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
            "Continue work on an existing task's branch. The worker executes the "
            "follow-up instructions on the same branch (and same worktree if the "
            "original worker is still idle and within the 24h cleanup window) — "
            "ideal for CI fixes, lint, test fixes, doc additions, or reviewer "
            "comments. Worker selection: by default the original worker is "
            "preferred when idle (free worktree reuse); when it's busy or "
            "offline, any idle worker in the guild picks up the branch from "
            "GitHub. Pass preferred_worker_id to force a specific worker. "
            "Call this in response to task-complete, task-followup-done, "
            "user comments on a parked PR task, or CI failures."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID (t-xxxxxx)."},
                "instructions": {
                    "type": "string",
                    "description": "Follow-up instructions to execute on the existing branch.",
                },
                "preferred_worker_id": {
                    "type": "string",
                    "description": (
                        "Optional: prefer this worker if idle. Defaults to the "
                        "task's current worker_id."
                    ),
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
        "name": "get_pr_status",
        "description": (
            "Fetch the live status of a single pull request: merged/closed/open state, "
            "submitted reviews, and the latest check-run conclusions for the head SHA. "
            "Use this to interpret a `[github-event]` message that doesn't carry enough "
            "context, or when deciding whether all required checks have completed before "
            "finalizing a task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "pr_number": {"type": "integer", "description": "Pull request number."},
            },
            "required": ["repo", "pr_number"],
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
    {
        "name": "review_pr",
        "description": (
            "Request an automated code review via the EXTERNAL code-review-agent MCP server "
            "at agent.meyers.life (override with REVIEWER_AGENT_URL env var). "
            "The remote agent fetches the PR diff, runs a Claude-powered review, and posts "
            "the result as a GitHub PR review (APPROVE / REQUEST_CHANGES / COMMENT). "
            "Use this when you want a specialised external reviewer. "
            "For a self-contained internal review without any external dependency, "
            "use review_pr_internal instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pr_url": {
                    "type": "string",
                    "description": (
                        "Full GitHub PR URL, e.g. https://github.com/owner/repo/pull/123"
                    ),
                },
            },
            "required": ["pr_url"],
        },
    },
    {
        "name": "review_pr_internal",
        "description": (
            "Perform an internal agent-driven code review of a GitHub pull request "
            "without calling any external service. "
            "Fetches the PR diff directly from the GitHub API, uses the Foreman AI to "
            "analyse it, then posts a GitHub PR review with a 3–5 bullet-point summary "
            "and up to 5 inline comments on specific lines. "
            "Supports action values APPROVE, REQUEST_CHANGES, or COMMENT (default COMMENT). "
            "Use this instead of review_pr when you want a quick review with no external "
            "dependency, or when agent.meyers.life is unavailable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pr_url": {
                    "type": "string",
                    "description": "Full GitHub PR URL, e.g. https://github.com/owner/repo/pull/123",
                },
                "action": {
                    "type": "string",
                    "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"],
                    "description": (
                        "Review verdict to submit to GitHub. "
                        "APPROVE — looks good; REQUEST_CHANGES — must fix before merge; "
                        "COMMENT — neutral feedback (default)."
                    ),
                },
            },
            "required": ["pr_url"],
        },
    },
    {
        "name": "dnsid",
        "description": (
            "Run a DNSid operation via the local dnsid-sdk CLI. "
            "Three commands: "
            "'resolve' — look up an FQDN's _dnsid TXT record and JWKS (param: fqdn); "
            "'sign' — sign a JWT with the agent's configured Ed25519 identity (param: claims object); "
            "'verify' — verify a JWT against its DNSid record (params: jwt, expected_aud, optional expected_nonce). "
            "Returns the CLI's JSON output on success; raises on failure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["resolve", "sign", "verify"],
                    "description": "Subcommand to run.",
                },
                "fqdn": {
                    "type": "string",
                    "description": "resolve: FQDN to look up, e.g. 'example.com'.",
                },
                "claims": {
                    "type": "object",
                    "description": "sign: JSON object of JWT claims (iss, sub, exp, nonce, …).",
                },
                "jwt": {
                    "type": "string",
                    "description": "verify: compact JWT string to verify.",
                },
                "expected_aud": {
                    "type": "string",
                    "description": "verify: required — aud claim must contain this value.",
                },
                "expected_nonce": {
                    "type": "string",
                    "description": "verify: optional — nonce claim must equal this value.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "call_agent",
        "description": (
            "Call a skill on a remote A2A-compatible HTTP agent. "
            "Fetches the agent card from {agent_url}/.well-known/agent.json to discover "
            "available skills, then dispatches the requested skill with the given params. "
            "Returns the raw JSON result from the agent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_url": {
                    "type": "string",
                    "description": (
                        "Base URL of the target A2A agent, "
                        "e.g. https://agent.example.com or http://localhost:8080"
                    ),
                },
                "skill": {
                    "type": "string",
                    "description": (
                        "Skill id to invoke, e.g. 'verify_identity_record' or 'lookup_dnsid'. "
                        "Must match a skill id listed in the agent card."
                    ),
                },
                "params": {
                    "type": "object",
                    "description": "Parameters to pass to the skill as a JSON object.",
                },
            },
            "required": ["agent_url", "skill"],
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


def _gh_api_diff(path: str, token: str) -> str:
    """GET a GitHub API path and return the raw unified diff text."""
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.diff",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


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


async def _guild_github_token(guild_id: str) -> tuple[str, str] | None:
    """Return (access_token, github_username) for this guild, or None."""
    from auth_deps import get_guild_pk

    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            return None
        result = await db.execute(
            select(GithubToken.access_token, GithubToken.github_username)
            .join(GuildMember, GuildMember.user_id == GithubToken.github_user_id)
            .where(GuildMember.guild_pk == guild_pk, GuildMember.role == "owner")
            .limit(1)
        )
        row = result.first()
        return (row.access_token, row.github_username) if row else None
    finally:
        await db.close()


async def _guild_private_key_pem(guild_id: str) -> str | None:
    """Return the Ed25519 private key PEM for the guild, or None if not found."""
    from auth_deps import get_guild_pk

    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            return None
        result = await db.execute(
            select(GuildKey.private_key_pem).where(GuildKey.guild_pk == guild_pk)
        )
        return result.scalar_one_or_none()
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
        if not worker_id or worker_id == "foreman":
            return False
        result = await db.execute(
            select(Agent.id).where(Agent.worker_id == worker_id, Agent.state == "idle").limit(1)
        )
        return result.scalar_one_or_none() is not None

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
    result = await db.execute(
        select(Agent.worker_id)
        .where(
            Agent.guild_pk == guild_pk,
            Agent.state == "idle",
            Agent.worker_id.is_not(None),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


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
# code-review-agent helpers
# ---------------------------------------------------------------------------

_PR_URL_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)/?$")

_VERDICT_TO_GH_EVENT = {
    "approved": "APPROVE",
    "changes-requested": "REQUEST_CHANGES",
    "comment": "COMMENT",
}

_REVIEW_REPORT_MIME = "application/vnd.code-review-agent.report+json"


def _extract_review_data(a2a_result: dict) -> tuple[str, str]:
    """Parse an A2A tasks/send result from the code-review-agent.

    Returns ``(github_event, review_body)`` where ``github_event`` is one of
    ``"APPROVE"``, ``"REQUEST_CHANGES"``, or ``"COMMENT"``.

    Inspects ``artifacts[*].parts[*]``: text parts supply the review body,
    a part with type ``application/vnd.code-review-agent.report+json`` supplies
    the structured verdict.
    """
    review_body = ""
    verdict = "comment"

    for artifact in a2a_result.get("artifacts", []):
        for part in artifact.get("parts", []):
            part_type = part.get("type", "")
            if part_type == "text" and not review_body:
                review_body = part.get("text", "")
            elif part_type == _REVIEW_REPORT_MIME:
                try:
                    report = json.loads(part.get("text", "{}"))
                    verdict = report.get("verdict") or report.get("summary", {}).get(
                        "verdict", "comment"
                    )
                except (json.JSONDecodeError, AttributeError):
                    pass

    github_event = _VERDICT_TO_GH_EVENT.get(str(verdict).lower(), "COMMENT")
    review_body = review_body or f"Automated code review completed (verdict: {verdict})."
    return github_event, review_body


# ---------------------------------------------------------------------------
# A2A agent call helpers
# ---------------------------------------------------------------------------


def _dnsid_bin() -> str:
    return os.path.expanduser(os.environ.get("DNSID_SDK_BIN", "~/dnsid-go/bin/dnsid-sdk"))


async def _run_dnsid(command: str, inp: dict, private_key_pem: str | None = None) -> dict:
    """Run a dnsid-sdk subcommand and return the parsed JSON result."""
    if command == "resolve":
        fqdn = inp.get("fqdn", "")
        if not fqdn:
            raise ValueError("dnsid resolve requires fqdn")
        cmd = [_dnsid_bin(), "resolve", fqdn]
        stdin_data = None
    elif command == "sign":
        claims = inp.get("claims")
        if not isinstance(claims, dict):
            raise ValueError("dnsid sign requires claims object")
        if not private_key_pem:
            raise ValueError("dnsid sign requires a guild signing key (none found in DB)")
        from foreman.auth import _dnsid_sign_sync

        return {
            "ok": True,
            "jwt": await asyncio.to_thread(_dnsid_sign_sync, claims, private_key_pem),
        }
    elif command == "verify":
        jwt_token = inp.get("jwt", "")
        expected_aud = inp.get("expected_aud", "")
        if not jwt_token:
            raise ValueError("dnsid verify requires jwt")
        if not expected_aud:
            raise ValueError("dnsid verify requires expected_aud")
        cmd = [_dnsid_bin(), "verify", "--jwt", jwt_token, "--expected-aud", expected_aud]
        if nonce := inp.get("expected_nonce"):
            cmd += ["--expected-nonce", nonce]
        stdin_data = None
    else:
        raise ValueError(f"Unknown dnsid command: {command!r}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(stdin_data), timeout=15)
    result = json.loads(stdout)
    if not result.get("ok"):
        raise RuntimeError(
            f"dnsid {command} [{result.get('error', '?')}]: "
            f"{result.get('message', stderr.decode(errors='replace')[:200])}"
        )
    return result


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
            from auth_deps import get_guild_pk

            guild_pk = await get_guild_pk(db, guild_id)
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
                        guild_pk=guild_pk,
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
                    select(Worker.id).where(Worker.id == wid, Worker.guild_pk == guild_pk)
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
                        .where(Task.id == existing_task_id, Task.guild_pk == guild_pk)
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
                            guild_pk=guild_pk,
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
                preferred_worker_id = inp.get("preferred_worker_id")
                result = await db.execute(
                    select(
                        Task.worker_id,
                        Task.state,
                        Task.branch,
                        Task.description,
                        Task.name,
                        Task.tool,
                        Task.issue_number,
                        Task.issue_repo,
                    ).where(Task.id == task_id, Task.guild_pk == guild_pk)
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
                        task_issue_number,
                        task_issue_repo,
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
                        update_vals: dict = {
                            "state": "working",
                            "phase": "followup",
                            "worker_id": target_worker_id,
                        }
                        if prior_state in ("done", "failed", "cancelled"):
                            # Re-opening a terminal task: clear soft-delete fields so it
                            # reappears in the live task list and isn't auto-purged.
                            update_vals["deleted_at"] = None
                            update_vals["finished_at"] = None
                        await db.execute(
                            update(Task).where(Task.id == task_id).values(**update_vals)
                        )
                        await db.commit()
                        await broadcast(
                            guild_id,
                            {
                                "type": "task-update",
                                "taskId": task_id,
                                "state": "working",
                                "workerId": target_worker_id,
                                "deletedAt": None,
                                "finishedAt": None,
                            },
                        )
                        await broadcast(
                            guild_id,
                            {
                                "type": "task-followup",
                                "workerId": target_worker_id,
                                "taskId": task_id,
                                "name": task_name or "",
                                "description": task_desc or "",
                                "tool": task_tool or "claude",
                                "branch": branch,
                                "instructions": instructions,
                                "issueNumber": task_issue_number,
                                "issueRepo": task_issue_repo,
                            },
                        )
                        if (
                            target_worker_id != original_worker_id
                            and original_worker_id
                            and original_worker_id != "foreman"
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
                deleted_at, err = _resolve_finalize_deleted_at(inp)
                if err:
                    result_text = err
                    is_error = True
                else:
                    result = await db.execute(
                        select(Task.worker_id).where(Task.id == task_id, Task.guild_pk == guild_pk)
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
                        Task.id == task_id, Task.guild_pk == guild_pk
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
                        Task.id == task_id, Task.guild_pk == guild_pk
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
                    select(Worker.id).where(Worker.id == wid, Worker.guild_pk == guild_pk)
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
                    select(Task).where(Task.id == task_id, Task.guild_pk == guild_pk)
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
            "get_pr_status",
            "review_pr",
            "review_pr_internal",
        ):
            logger.info("Executing GitHub tool %s with input %s", tu.name, inp)
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

                    elif tu.name == "get_pr_status":
                        repo = inp["repo"]
                        num = int(inp["pr_number"])
                        pr = await asyncio.to_thread(_gh_api, f"/repos/{repo}/pulls/{num}", token)
                        reviews_raw = await asyncio.to_thread(
                            _gh_api,
                            f"/repos/{repo}/pulls/{num}/reviews?per_page=20",
                            token,
                        )
                        head_sha = (pr.get("head") or {}).get("sha")
                        check_runs: list = []
                        if head_sha:
                            crs = await asyncio.to_thread(
                                _gh_api,
                                f"/repos/{repo}/commits/{head_sha}/check-runs?per_page=30",
                                token,
                            )
                            if isinstance(crs, dict):
                                check_runs = crs.get("check_runs", []) or []
                        result_text = json.dumps(
                            {
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
                                        "summary": ((cr.get("output") or {}).get("summary") or "")[
                                            :300
                                        ],
                                    }
                                    for cr in check_runs
                                ],
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

                    elif tu.name == "review_pr":
                        pr_url = inp["pr_url"]
                        logger.info("guild=%s review_pr: pr_url=%s", guild_id, pr_url)
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
                            from foreman.a2a_client import A2AClient, _guild_caller_domain

                            review_agent = os.environ.get(
                                "REVIEWER_AGENT_URL", "https://agent.meyers.life"
                            )
                            client = A2AClient(f"{review_agent.rstrip('/')}/.well-known/agent.json")
                            try:
                                a2a_result = await client.review_pr(
                                    pr_url,
                                    caller_domain=_guild_caller_domain(guild_id),
                                    private_key_pem=await _guild_private_key_pem(guild_id),
                                )
                            except urllib.error.HTTPError as exc:
                                try:
                                    err_body = exc.read().decode(errors="replace")
                                except Exception:
                                    err_body = ""
                                logger.error(
                                    "guild=%s review_pr: mcp_request_failed pr_url=%s status=%d err_body=%.500s",
                                    guild_id,
                                    pr_url,
                                    exc.code,
                                    err_body,
                                    exc_info=True,
                                )
                                raise
                            except Exception:
                                logger.error(
                                    "guild=%s review_pr: mcp_request_failed pr_url=%s",
                                    guild_id,
                                    pr_url,
                                    exc_info=True,
                                )
                                raise
                            github_event, review_body = _extract_review_data(a2a_result)
                            logger.info(
                                "guild=%s review_pr: verdict=%s summary_preview=%.200s",
                                guild_id,
                                github_event,
                                review_body,
                            )
                            review_data = await asyncio.to_thread(
                                _gh_api_post,
                                f"/repos/{pr_repo}/pulls/{pr_number}/reviews",
                                token,
                                {"body": review_body, "event": github_event},
                            )
                            result_text = json.dumps(
                                {
                                    "pr_url": pr_url,
                                    "verdict": github_event,
                                    "review_id": review_data.get("id"),
                                    "review_posted": True,
                                    "summary": review_body[:400],
                                }
                            )

                    elif tu.name == "review_pr_internal":
                        pr_url = inp["pr_url"]
                        action = (inp.get("action") or "COMMENT").upper()
                        if action not in ("APPROVE", "REQUEST_CHANGES", "COMMENT"):
                            action = "COMMENT"
                        logger.info(
                            "guild=%s review_pr_internal: pr_url=%s action=%s",
                            guild_id,
                            pr_url,
                            action,
                        )
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
                                asyncio.to_thread(
                                    _gh_api,
                                    f"/repos/{pr_repo}/pulls/{pr_number}",
                                    token,
                                ),
                                asyncio.to_thread(
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
                                import anthropic as _anthropic

                                _ai = _anthropic.AsyncAnthropic()
                                review_prompt = (
                                    "You are a thorough code reviewer. Review the following "
                                    "GitHub pull request and provide structured feedback.\n\n"
                                    f"PR: {pr_title}\n"
                                    f"Base: {base_ref} ← Head: {head_ref}\n"
                                    f"Description: {pr_body_text[:1000]}\n\n"
                                    f"Diff (up to 40 000 chars):\n{diff_text[:40000]}\n\n"
                                    "Respond with a JSON object only (no markdown fences) "
                                    "with exactly these fields:\n"
                                    '{"summary": "3-5 markdown bullet points (use - prefix)", '
                                    '"comments": [{"path": "file.py", "line": 42, '
                                    '"side": "RIGHT", "body": "concise comment"}]}\n\n'
                                    "Rules:\n"
                                    "- summary: 3-5 bullet points covering key findings\n"
                                    "- comments: 0-5 objects for the most important issues\n"
                                    "- line: line number in the NEW file version (RIGHT side)\n"
                                    "- Only comment on lines present in the diff\n"
                                    "- Focus on bugs, security issues, and significant design problems\n"
                                    "- Keep each comment concise (1-3 sentences)"
                                )
                                ai_msg = await _ai.messages.create(
                                    model="claude-sonnet-4-6",
                                    max_tokens=2048,
                                    messages=[{"role": "user", "content": review_prompt}],
                                )
                                review_json = _parse_review_from_claude(ai_msg.content[0].text)
                            except Exception as exc:
                                logger.error(
                                    "guild=%s review_pr_internal: ai generation failed: %s",
                                    guild_id,
                                    exc,
                                    exc_info=True,
                                )
                                review_json = {
                                    "summary": "Review could not be generated by the AI agent.",
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

                            try:
                                review_data = await asyncio.to_thread(
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
                                review_data = await asyncio.to_thread(
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

                except urllib.error.HTTPError as exc:
                    result_text = f"GitHub API error: {exc.code} {exc.reason}"
                    is_error = True
                except Exception as exc:
                    result_text = f"GitHub error: {exc}"
                    is_error = True

        # dnsid CLI — resolve / sign / verify
        if tu.name == "dnsid":
            logger.info("dnsid tool: input=%s", inp)
            command = inp.get("command", "")
            if not command:
                result_text = "dnsid requires command (resolve, sign, verify)"
                is_error = True
            else:
                try:
                    pem = await _guild_private_key_pem(guild_id) if command == "sign" else None
                    result_text = json.dumps(await _run_dnsid(command, inp, pem))
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
                    card = await asyncio.to_thread(_fetch_agent_card, card_url)
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
                        response = await asyncio.to_thread(
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

    block: dict = {"type": "tool_result", "tool_use_id": tu.id, "content": result_text}
    if is_error:
        block["is_error"] = True
    return block
