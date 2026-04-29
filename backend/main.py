import asyncio
import json
import os
import random
import secrets
import string
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import AsyncSessionLocal, get_db
from models import Agent, Guild, GithubToken, Message, Task, TaskLog, UserSession, Worker

# Load .env (looked up from CWD upward, then alongside this file) before any
# code reads os.environ, so ANTHROPIC_API_KEY etc. are available.
try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

try:
    import anthropic as _anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


def _row(obj) -> dict:
    """Convert a SQLAlchemy ORM model instance to a plain dict."""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(title="Pioneer Square", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# GitHub OAuth config (set via environment variables)
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
GITHUB_REDIRECT_URI = os.environ.get("GITHUB_REDIRECT_URI", "http://localhost:8000/auth/github/callback")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")

# In-memory: short-lived state tokens for OAuth CSRF protection
oauth_states: set[str] = set()

_http_bearer = HTTPBearer(auto_error=False)

# In-memory WebSocket connections: guild_id -> list of WebSocket
connections: Dict[str, List[WebSocket]] = {}

# Running agent subprocesses: agent_id -> asyncio.subprocess.Process
# Used only by /agents/{id}/run (one-off claude/codex/pi invocations).
# Worker subprocesses live in the standalone /worker process.
running_processes: Dict[str, asyncio.subprocess.Process] = {}

# Foreman AI conversation history per guild
foreman_conversations: Dict[str, List[dict]] = {}    # guild_id -> messages list


async def init_db():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text as sa_text

    def run_migrations():
        cfg = Config(Path(__file__).resolve().parent / "alembic.ini")
        db_url = os.environ.get("DATABASE_URL", f"sqlite+aiosqlite:///{os.environ.get('DB_PATH', 'pioneer_square.db')}")
        # Need a sync engine for inspection; strip aiosqlite async driver prefix.
        sync_url = db_url.replace("+aiosqlite", "")
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                tables = inspect(engine).get_table_names()
                has_alembic = "alembic_version" in tables
                has_data = "guilds" in tables
            # Pre-Alembic database: stamp to current head so upgrade is a no-op.
            if has_data and not has_alembic:
                command.stamp(cfg, "head")
        finally:
            engine.dispose()
        command.upgrade(cfg, "head")

    await asyncio.to_thread(run_migrations)

    # On every startup, no worker processes are connected yet.
    async with AsyncSessionLocal() as db:
        await db.execute(update(Worker).values(state="offline"))
        await db.execute(
            update(Agent)
            .where(Agent.worker_id.in_(select(Worker.id).where(Worker.state == "offline")))
            .values(state="offline")
        )
        await db.commit()


def generate_guild_id():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _worker_name(worker_id: str) -> str:
    raw = worker_id[2:].upper()
    split = 2 + sum(ord(c) for c in raw) % 3
    return f"{raw[:split]}-{raw[split:]}"


class GuildCreate(BaseModel):
    name: Optional[str] = None


class GuildUpdate(BaseModel):
    name: str


class RunAgentRequest(BaseModel):
    tool: str          # "claude" | "codex" | "pi"
    prompt: str
    model: Optional[str] = None
    provider: Optional[str] = None   # pi only


class WorkerCreate(BaseModel):
    repos: List[str]                  # ["owner/repo", ...]
    github_token: Optional[str] = None


class TaskCreate(BaseModel):
    description: str
    name: Optional[str] = None
    tool: str = "claude"          # "claude" | "codex" | "pi"
    issue_number: Optional[int] = None
    issue_repo: Optional[str] = None
    parent_task_id: Optional[str] = None
    phase: Optional[str] = "execute"


# ---------------------------------------------------------------------------
# Worker tasks live in the standalone /worker package; this backend only
# persists worker/task state and dispatches assignments over WebSocket.
# ---------------------------------------------------------------------------


FOREMAN_SYSTEM = """\
You are the Foreman AI in Pioneer Square, a multi-agent coding workshop.
You coordinate worker agents that autonomously clone repos, write code, and open PRs.

## Your responsibilities
- Understand what the human wants and break it into named, tracked tasks
- Always call create_task first to name the work and get a task_id; then pass that task_id to assign_task so the same record is assigned to a worker (no duplicate rows)
- After a worker finishes (task-complete), review the result and decide: send_followup for \
additional work in the same worktree, or finalize_task when done
- Message workers mid-task via message_worker for context
- Redirect running tasks via redirect_task (SIGTERM + resume with full context) to course-correct
- Cancel tasks that are going wrong or are no longer needed via cancel_task
- Summarise status and outcomes when asked
- Escalate to the human only when genuinely stuck

## Multi-step flows
For complex work use phases:
1. **plan** — create_task(phase='plan'), assign a worker to produce an outline/spec
2. **execute** — assign workers to implement
3. **review** — assign a worker to verify correctness, run tests, check the PR

## Task ownership
- create_task before assign_task so every job has a human-readable name in the sidebar
- Pass the task_id from create_task into assign_task — this assigns the same task to a worker instead of creating a second row
- After task-complete: call send_followup for further work (update tests, fix lint, add docs),
  or call finalize_task to mark it complete — don't leave tasks in limbo

## GitHub access
You have direct GitHub access via list_github_issues, get_github_issue, and list_github_prs.
Use these to discover work from issues, understand requirements before assigning tasks,
and review PRs opened by workers.

Workers are configured with repos. Prefer workers whose repos cover the task.
Be concise — one short paragraph maximum unless detail is requested.\
"""

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
                "issue_number": {"type": "integer", "description": "GitHub issue to close (optional)."},
                "issue_repo": {"type": "string", "description": "owner/repo for the issue (optional)."},
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
            "Call after reviewing a completed task when no additional work is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to finalize."},
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
        "name": "list_github_issues",
        "description": (
            "List GitHub issues for a repo. Use this to discover work that needs to be done."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo, e.g. 'acme/backend'"},
                "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "Default: open"},
                "limit": {"type": "integer", "description": "Max issues to return (default 20, max 50)"},
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
                "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "Default: open"},
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
]


async def _foreman_exec_tools(guild_id: str, tool_uses: list) -> list:
    """Execute tool calls from the foreman AI and return tool-result blocks."""
    results = []
    for tu in tool_uses:
        inp = tu.input
        result_text = ""
        db = await get_db()
        try:
            if tu.name == "create_task":
                name = (inp.get("name") or "")[:80]
                desc = inp.get("description", name)
                phase = inp.get("phase", "execute")
                task_id = "t-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
                created_at = datetime.now(timezone.utc).isoformat()
                db.add(Task(
                    id=task_id,
                    worker_id="foreman",
                    guild_id=guild_id,
                    name=name,
                    description=desc,
                    tool="claude",
                    state="pending",
                    phase=phase,
                    created_at=created_at,
                ))
                await db.commit()
                await broadcast(guild_id, {
                    "type": "task-created",
                    "taskId": task_id,
                    "name": name,
                    "description": desc,
                    "phase": phase,
                    "state": "pending",
                    "createdAt": created_at,
                })
                result_text = f"Task {task_id} created: '{name}'. Reference this task_id in assign_task."

            elif tu.name == "assign_task":
                wid = inp["worker_id"]
                desc = inp["description"]
                phase = inp.get("phase", "execute")
                tool = inp.get("tool", "claude")
                existing_task_id = inp.get("task_id")
                result = await db.execute(
                    select(Worker.id).where(Worker.id == wid, Worker.guild_id == guild_id)
                )
                worker_row = result.scalar_one_or_none()
                if not worker_row:
                    result_text = f"Worker {wid} not found — task NOT queued."
                elif existing_task_id:
                    # Update the existing foreman task in place — no duplicate row
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
                    await broadcast(guild_id, {
                        "type": "task-assigned",
                        "workerId": wid,
                        "taskId": task_id,
                        "name": task_name,
                        "description": desc,
                        "tool": tool,
                        "phase": phase,
                        "issueNumber": inp.get("issue_number"),
                        "issueRepo": inp.get("issue_repo"),
                    })
                    result_text = f"Task {task_id} assigned to {wid}."
                else:
                    # No existing task_id — create a new row
                    name = (inp.get("name") or desc[:60])
                    parent_task_id = inp.get("parent_task_id")
                    task_id = "t-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
                    created_at = datetime.now(timezone.utc).isoformat()
                    db.add(Task(
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
                    ))
                    await db.commit()
                    await broadcast(guild_id, {
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
                    })
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
                        update(Task).where(Task.id == task_id).values(state="working", phase="followup")
                    )
                    await db.commit()
                    await broadcast(guild_id, {
                        "type": "task-followup",
                        "workerId": worker_id_val,
                        "taskId": task_id,
                        "instructions": instructions,
                    })
                    result_text = f"Follow-up sent to {worker_id_val} for task {task_id}."

            elif tu.name == "finalize_task":
                task_id = inp["task_id"]
                result = await db.execute(
                    select(Task.worker_id).where(Task.id == task_id, Task.guild_id == guild_id)
                )
                worker_id_val = result.scalar_one_or_none()
                if not worker_id_val:
                    result_text = f"Task {task_id} not found."
                else:
                    finished_at = datetime.now(timezone.utc).isoformat()
                    await db.execute(
                        update(Task).where(Task.id == task_id).values(state="done", finished_at=finished_at)
                    )
                    await db.commit()
                    await broadcast(guild_id, {
                        "type": "task-finalize",
                        "workerId": worker_id_val,
                        "taskId": task_id,
                    })
                    await broadcast(guild_id, {
                        "type": "task-update",
                        "taskId": task_id,
                        "state": "done",
                        "finishedAt": finished_at,
                    })
                    result_text = f"Task {task_id} finalized."
                    # Compact all prior tool-result blocks mentioning this task
                    for msg in foreman_conversations.get(guild_id, []):
                        if msg["role"] == "user" and isinstance(msg["content"], list):
                            for block in msg["content"]:
                                if (block.get("type") == "tool_result"
                                        and task_id in block.get("content", "")):
                                    block["content"] = f"[{task_id}: done]"

            elif tu.name == "message_worker":
                wid = inp["worker_id"]
                msg = inp["message"]
                await _emit_terminal_line(guild_id, wid, f"[foreman] {msg}")
                await broadcast(guild_id, {
                    "type": "worker-message",
                    "workerId": wid,
                    "message": msg,
                })
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
                        await broadcast(guild_id, {
                            "type": "task-redirect",
                            "workerId": worker_id_val,
                            "taskId": task_id,
                            "instructions": instructions,
                        })
                        await broadcast(guild_id, {
                            "type": "task-update",
                            "taskId": task_id,
                            "state": "working",
                        })
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
                        finished_at = datetime.now(timezone.utc).isoformat()
                        await db.execute(
                            update(Task).where(Task.id == task_id).values(
                                state="cancelled", finished_at=finished_at
                            )
                        )
                        await db.commit()
                        await broadcast(guild_id, {
                            "type": "task-cancel",
                            "workerId": worker_id_val,
                            "taskId": task_id,
                        })
                        await broadcast(guild_id, {
                            "type": "task-update",
                            "taskId": task_id,
                            "state": "cancelled",
                            "finishedAt": finished_at,
                        })
                        result_text = f"Task {task_id} cancelled." + (f" Reason: {reason}" if reason else "")
        finally:
            await db.close()

        # GitHub tools — use guild's OAuth token
        if tu.name in ("list_github_issues", "get_github_issue", "list_github_prs", "claim_github_issue"):
            creds = await _guild_github_token(guild_id)
            if not creds:
                result_text = "No GitHub token found for this guild — user must connect GitHub first."
            else:
                token, username = creds
                try:
                    if tu.name == "list_github_issues":
                        repo = inp["repo"]
                        state = inp.get("state", "open")
                        limit = min(int(inp.get("limit", 20)), 50)
                        issues = await asyncio.to_thread(
                            _gh_api, f"/repos/{repo}/issues?state={state}&per_page={limit}", token
                        )
                        trimmed = [
                            {"number": i["number"], "title": i["title"],
                             "state": i["state"], "labels": [l["name"] for l in i.get("labels", [])],
                             "assignees": [a["login"] for a in i.get("assignees", [])],
                             "created_at": i["created_at"]}
                            for i in issues if "pull_request" not in i
                        ]
                        result_text = json.dumps(trimmed)

                    elif tu.name == "get_github_issue":
                        repo = inp["repo"]
                        num = int(inp["issue_number"])
                        issue = await asyncio.to_thread(_gh_api, f"/repos/{repo}/issues/{num}", token)
                        comments_raw = await asyncio.to_thread(
                            _gh_api, f"/repos/{repo}/issues/{num}/comments?per_page=20", token
                        )
                        result_text = json.dumps({
                            "number": issue["number"],
                            "title": issue["title"],
                            "state": issue["state"],
                            "body": (issue.get("body") or "")[:2000],
                            "labels": [l["name"] for l in issue.get("labels", [])],
                            "comments": [
                                {"author": c["user"]["login"], "body": (c.get("body") or "")[:500]}
                                for c in comments_raw
                            ],
                        })

                    elif tu.name == "list_github_prs":
                        repo = inp["repo"]
                        state = inp.get("state", "open")
                        prs = await asyncio.to_thread(
                            _gh_api, f"/repos/{repo}/pulls?state={state}&per_page=20", token
                        )
                        result_text = json.dumps([
                            {"number": p["number"], "title": p["title"],
                             "state": p["state"], "head": p["head"]["ref"],
                             "draft": p.get("draft", False)}
                            for p in prs
                        ])

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

                except urllib.error.HTTPError as exc:
                    result_text = f"GitHub API error: {exc.code} {exc.reason}"
                except Exception as exc:
                    result_text = f"GitHub error: {exc}"

        results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result_text})
    return results


async def _run_foreman_ai(guild_id: str, human_message: str, extra_context: str = ""):
    """Process a human message (or escalation) through the Claude foreman AI."""
    if not HAS_ANTHROPIC:
        now = datetime.now(timezone.utc).isoformat()
        await broadcast(guild_id, {
            "type": "chat", "from": "foreman", "to": "user",
            "content": "Foreman AI offline (install `anthropic` package to enable).",
            "createdAt": now,
        })
        return

    # Build live context for the system prompt
    db = await get_db()
    try:
        # Complex UNION query: registered workers (with connected agents) + ephemeral agents
        result = await db.execute(
            text(
                "SELECT w.id, w.repos, w.state as worker_state,"
                " COUNT(a.id) as agent_count,"
                " GROUP_CONCAT(a.id || ':' || a.state) as agents"
                " FROM workers w"
                " LEFT JOIN agents a ON a.worker_id = w.id AND a.state != 'offline'"
                " WHERE w.guild_id = :guild_id"
                " GROUP BY w.id"
                " UNION ALL"
                " SELECT a.id, '[]', a.state, 1, a.id || ':' || a.state"
                " FROM agents a"
                " WHERE a.guild_id = :guild_id AND a.type = 'worker'"
                " AND a.worker_id IS NULL AND a.state != 'offline'"
            ),
            {"guild_id": guild_id},
        )
        worker_rows = [dict(r._mapping) for r in result.fetchall()]
        task_result = await db.execute(
            select(Task.id, Task.worker_id, Task.description, Task.state, Task.branch, Task.pr_url)
            .where(Task.guild_id == guild_id)
            .order_by(Task.created_at.desc())
            .limit(10)
        )
        task_rows = [dict(r._mapping) for r in task_result.fetchall()]
    finally:
        await db.close()

    workers_block = json.dumps(
        [{"id": r["id"], "state": r["worker_state"] or "idle",
          "repos": json.loads(r["repos"] or "[]"),
          "agent_count": r["agent_count"] or 0} for r in worker_rows],
        indent=2,
    )
    tasks_block = json.dumps(task_rows[:6], indent=2)
    system = (
        f"{FOREMAN_SYSTEM}\n\n"
        f"## Current workers\n```json\n{workers_block}\n```\n\n"
        f"## Recent tasks\n```json\n{tasks_block}\n```"
        + (f"\n\n## Context\n{extra_context}" if extra_context else "")
    )

    history = foreman_conversations.setdefault(guild_id, [])
    history.append({"role": "user", "content": human_message})

    client = _anthropic.AsyncAnthropic()

    _RESULT_MAX = 400  # chars kept per tool result in history

    try:
        text_parts = []
        for _ in range(6):  # safety cap on tool-call rounds
            resp = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system,
                messages=history,
                tools=FOREMAN_TOOLS,
            )
            history.append({"role": "assistant", "content": resp.content})
            text_parts += [b.text for b in resp.content if b.type == "text" and b.text.strip()]

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break  # end_turn — foreman is done

            tool_results = await _foreman_exec_tools(guild_id, tool_uses)
            # Truncate verbose results (e.g. GitHub JSON) before storing in history
            trimmed = [
                {**r, "content": r["content"][:_RESULT_MAX] + " …[truncated]"}
                if len(r.get("content", "")) > _RESULT_MAX else r
                for r in tool_results
            ]
            history.append({"role": "user", "content": trimmed})

        # Trim history
        foreman_conversations[guild_id] = history[-20:]

        response_text = "\n".join(text_parts).strip()
        if response_text:
            now = datetime.now(timezone.utc).isoformat()
            await broadcast(guild_id, {
                "type": "chat", "from": "foreman", "to": "user",
                "content": response_text, "createdAt": now,
            })
            db = await get_db()
            try:
                db.add(Message(
                    guild_id=guild_id,
                    from_agent="foreman",
                    to_agent="user",
                    content=response_text,
                    message_type="chat",
                    created_at=now,
                ))
                await db.commit()
            finally:
                await db.close()

    except Exception as exc:
        now = datetime.now(timezone.utc).isoformat()
        await broadcast(guild_id, {
            "type": "chat", "from": "foreman", "to": "user",
            "content": f"Foreman error: {exc}", "createdAt": now,
        })


# ---------------------------------------------------------------------------
# GitHub OAuth helpers
# ---------------------------------------------------------------------------

def _gh_exchange_code(code: str) -> dict:
    payload = urllib.parse.urlencode({
        "client_id": GITHUB_CLIENT_ID,
        "client_secret": GITHUB_CLIENT_SECRET,
        "code": code,
        "redirect_uri": GITHUB_REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(
        "https://github.com/login/oauth/access_token",
        data=payload,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _gh_get_user(token: str) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


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


async def _guild_github_token(guild_id: str) -> Optional[tuple[str, str]]:
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


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def require_user(
    credentials: HTTPAuthorizationCredentials = Depends(_http_bearer),
) -> str:
    """FastAPI dependency: validates the login_token and returns github_user_id."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    token = credentials.credentials
    db = await get_db()
    try:
        result = await db.execute(
            select(UserSession.github_user_id).where(UserSession.token == token)
        )
        github_user_id = result.scalar_one_or_none()
        if not github_user_id:
            raise HTTPException(status_code=401, detail="Invalid or expired session token")
        return github_user_id
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# GitHub OAuth endpoints
# ---------------------------------------------------------------------------

@app.get("/auth/github/login")
async def github_login():
    """Start the GitHub OAuth flow. Returns the authorization URL."""
    if not GITHUB_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured (missing GITHUB_CLIENT_ID)")
    state = secrets.token_urlsafe(16)
    oauth_states.add(state)
    params = urllib.parse.urlencode({
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": GITHUB_REDIRECT_URI,
        "scope": "repo read:org project",
        "state": state,
    })
    return {"url": f"https://github.com/login/oauth/authorize?{params}"}


@app.get("/auth/github/callback")
async def github_callback(code: str = Query(...), state: str = Query(...)):
    """Handle the GitHub OAuth callback: store token, issue a login_token, redirect to frontend."""
    if state not in oauth_states:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    oauth_states.discard(state)

    try:
        token_data = await asyncio.to_thread(_gh_exchange_code, code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub token exchange failed: {exc}")

    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail=f"No access_token in GitHub response: {token_data}")

    try:
        user_data = await asyncio.to_thread(_gh_get_user, access_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub user fetch failed: {exc}")

    github_user_id = str(user_data["id"])
    github_username = user_data.get("login", "")
    login_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()

    db = await get_db()
    try:
        stmt = sqlite_insert(GithubToken).values(
            github_user_id=github_user_id,
            github_username=github_username,
            access_token=access_token,
            token_type=token_data.get("token_type", "bearer"),
            scope=token_data.get("scope", ""),
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["github_user_id"],
            set_={
                "github_username": stmt.excluded.github_username,
                "access_token": stmt.excluded.access_token,
                "token_type": stmt.excluded.token_type,
                "scope": stmt.excluded.scope,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await db.execute(stmt)
        db.add(UserSession(token=login_token, github_user_id=github_user_id, created_at=now))
        await db.commit()
    finally:
        await db.close()

    # Redirect to the frontend landing page with the login_token and GitHub info
    qs = urllib.parse.urlencode({
        "login_token": login_token,
        "gh_token": access_token,
        "gh_user_id": github_user_id,
        "gh_login": github_username,
        "gh_name": user_data.get("name") or "",
        "gh_avatar": user_data.get("avatar_url") or "",
    })
    return RedirectResponse(url=f"{FRONTEND_URL}/?{qs}")


@app.get("/auth/github/token")
async def get_github_token(guild_id: str = Query(...)):
    """Return the stored OAuth token for the guild's linked GitHub user. Used by workers."""
    db = await get_db()
    try:
        result = await db.execute(
            select(Guild.github_user_id).where(Guild.id == guild_id)
        )
        github_user_id_val = result.scalar_one_or_none()
        if not github_user_id_val:
            raise HTTPException(status_code=404, detail="No GitHub account linked to this guild")
        result = await db.execute(
            select(GithubToken.access_token, GithubToken.github_username)
            .where(GithubToken.github_user_id == github_user_id_val)
        )
        token_row = result.first()
        if not token_row:
            raise HTTPException(status_code=404, detail="GitHub token not found")
        return {"access_token": token_row.access_token, "username": token_row.github_username}
    finally:
        await db.close()


@app.get("/auth/me")
async def get_me(github_user_id: str = Depends(require_user)):
    """Return the currently authenticated user's info."""
    db = await get_db()
    try:
        result = await db.execute(
            select(
                GithubToken.github_user_id,
                GithubToken.github_username,
                GithubToken.scope,
            ).where(GithubToken.github_user_id == github_user_id)
        )
        row = result.first()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        return {
            "github_user_id": row.github_user_id,
            "github_username": row.github_username,
            "scope": row.scope,
        }
    finally:
        await db.close()


@app.delete("/auth/logout")
async def logout(credentials: HTTPAuthorizationCredentials = Depends(_http_bearer)):
    """Invalidate the current login_token."""
    if credentials:
        db = await get_db()
        try:
            await db.execute(
                delete(UserSession).where(UserSession.token == credentials.credentials)
            )
            await db.commit()
        finally:
            await db.close()
    return {"status": "logged_out"}


@app.post("/guilds")
async def create_guild(
    data: Optional[GuildCreate] = None,
    github_user_id: str = Depends(require_user),
):
    if data is None:
        data = GuildCreate()
    created_at = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        for _ in range(5):
            guild_id = generate_guild_id()
            try:
                db.add(Guild(
                    id=guild_id,
                    created_at=created_at,
                    name=data.name or f"Guild {guild_id}",
                    github_user_id=github_user_id,
                ))
                await db.commit()
                break
            except IntegrityError:
                await db.rollback()
                continue
        else:
            raise HTTPException(status_code=500, detail="Could not generate unique guild ID")
    finally:
        await db.close()
    return {"id": guild_id, "created_at": created_at, "name": data.name or f"Guild {guild_id}"}


@app.get("/guilds")
async def list_guilds(github_user_id: str = Depends(require_user)):
    db = await get_db()
    try:
        result = await db.execute(
            select(
                Guild.id,
                Guild.created_at,
                Guild.name,
                func.count(Agent.id).label("agent_count"),
            )
            .select_from(Guild)
            .outerjoin(Agent, (Agent.guild_id == Guild.id) & (Agent.type != "foreman"))
            .where(Guild.github_user_id == github_user_id)
            .group_by(Guild.id)
            .order_by(Guild.created_at.desc())
        )
        return [dict(r._mapping) for r in result.fetchall()]
    finally:
        await db.close()


@app.patch("/guilds/{guild_id}")
async def update_guild(
    guild_id: str,
    data: GuildUpdate,
    github_user_id: str = Depends(require_user),
):
    db = await get_db()
    try:
        result = await db.execute(
            select(Guild).where(Guild.id == guild_id, Guild.github_user_id == github_user_id)
        )
        guild = result.scalar_one_or_none()
        if not guild:
            raise HTTPException(status_code=404, detail="Guild not found")
        guild.name = data.name
        await db.commit()
    finally:
        await db.close()
    await broadcast(guild_id, {"type": "guild-updated", "id": guild_id, "name": data.name})
    return {"id": guild_id, "name": data.name}


@app.get("/guilds/{guild_id}")
async def get_guild(guild_id: str):
    db = await get_db()
    try:
        result = await db.execute(select(Guild).where(Guild.id == guild_id))
        guild = result.scalar_one_or_none()
        if not guild:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.execute(
            select(Agent).where(
                Agent.guild_id == guild_id,
                Agent.state != "offline",
                Agent.type != "foreman",
            )
        )
        agents = result.scalars().all()
        result = await db.execute(
            select(Message)
            .where(Message.guild_id == guild_id)
            .order_by(Message.created_at.desc())
            .limit(100)
        )
        messages = result.scalars().all()
        return {
            **_row(guild),
            "agents": [_row(a) for a in agents],
            "messages": [_row(m) for m in reversed(messages)],
        }
    finally:
        await db.close()


async def broadcast(guild_id: str, message: dict, exclude: WebSocket = None):
    """Broadcast a message to all connections in a guild."""
    if guild_id not in connections:
        return
    dead = []
    for ws in connections[guild_id]:
        if ws is exclude:
            continue
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connections[guild_id].remove(ws)


@app.websocket("/ws/{guild_id}")
async def websocket_endpoint(websocket: WebSocket, guild_id: str):
    await websocket.accept()
    if guild_id not in connections:
        connections[guild_id] = []
    connections[guild_id].append(websocket)
    # Agents that joined via this websocket; marked offline when it disconnects.
    joined_agents: set[str] = set()
    db = await get_db()
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "join":
                agent_id = data.get("agentId")
                agent_name = data.get("agentName", "Unknown")
                agent_type = data.get("agentType", "worker")
                worker_id = data.get("workerId")
                joined_at = datetime.now(timezone.utc).isoformat()
                stmt = sqlite_insert(Agent).values(
                    id=agent_id,
                    guild_id=guild_id,
                    worker_id=worker_id,
                    name=agent_name,
                    type=agent_type,
                    state="idle",
                    joined_at=joined_at,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "guild_id": stmt.excluded.guild_id,
                        "worker_id": stmt.excluded.worker_id,
                        "name": stmt.excluded.name,
                        "type": stmt.excluded.type,
                        "state": stmt.excluded.state,
                        "joined_at": stmt.excluded.joined_at,
                    },
                )
                await db.execute(stmt)
                # Mark worker online when it (re)connects.
                if agent_type == "worker" and worker_id:
                    await db.execute(
                        update(Worker)
                        .where(Worker.id == worker_id, Worker.guild_id == guild_id)
                        .values(state="online")
                    )
                await db.commit()
                if agent_id:
                    joined_agents.add(agent_id)
                broadcast_msg = {
                    "type": "agent-joined",
                    "agentId": agent_id,
                    "agentName": agent_name,
                    "agentType": agent_type,
                    "workerId": worker_id,
                    "state": "idle",
                    "joinedAt": joined_at
                }
                await broadcast(guild_id, broadcast_msg)

            elif msg_type == "agent-state":
                agent_id = data.get("agentId")
                state = data.get("state", "idle")
                await db.execute(
                    update(Agent)
                    .where(Agent.id == agent_id, Agent.guild_id == guild_id)
                    .values(state=state)
                )
                await db.commit()
                await broadcast(guild_id, {
                    "type": "agent-state",
                    "agentId": agent_id,
                    "state": state
                })

            elif msg_type == "chat":
                from_agent = data.get("from", "user")
                to_agent = data.get("to", "foreman")
                content = data.get("content", "")
                created_at = datetime.now(timezone.utc).isoformat()
                db.add(Message(
                    guild_id=guild_id,
                    from_agent=from_agent,
                    to_agent=to_agent,
                    content=content,
                    message_type="chat",
                    created_at=created_at,
                ))
                await db.commit()
                await broadcast(guild_id, {
                    "type": "chat",
                    "from": from_agent,
                    "to": to_agent,
                    "content": content,
                    "createdAt": created_at
                })
                # Route human messages addressed to foreman through the AI
                if from_agent == "user" and to_agent == "foreman" and content:
                    asyncio.create_task(_run_foreman_ai(guild_id, content))

            elif msg_type == "terminal-output":
                msg_agent_id = data.get("agentId")
                line = data.get("line", "")
                task_id = data.get("taskId")
                log_id = data.get("logId")
                created_at = datetime.now(timezone.utc).isoformat()
                # Look up worker_id for this agent to tag logs for cross-filter queries.
                worker_id_for_log = None
                if msg_agent_id:
                    result = await db.execute(select(Agent.worker_id).where(Agent.id == msg_agent_id))
                    worker_id_for_log = result.scalar_one_or_none()
                if line:
                    db.add(TaskLog(task_id=task_id or None, timestamp=created_at, line=line,
                                   worker_id=worker_id_for_log, agent_id=msg_agent_id,
                                   log_id=log_id))
                    await db.commit()
                bcast: dict = {
                    "type": "terminal-output",
                    "agentId": msg_agent_id,
                    "workerId": worker_id_for_log,
                    "taskId": task_id,
                    "line": line,
                    "timestamp": created_at,
                }
                if log_id:
                    bcast["logId"] = log_id
                await broadcast(guild_id, bcast)

            elif msg_type == "tool-detail":
                # Full tool input/output payload — persist to DB and forward to frontend.
                log_id = data.get("logId")
                task_id = data.get("taskId")
                if log_id:
                    detail_json = json.dumps({k: v for k, v in data.items()
                                              if k not in ("type", "logId", "taskId")})
                    await db.execute(
                        update(TaskLog)
                        .where(TaskLog.log_id == log_id)
                        .values(data=detail_json)
                    )
                    await db.commit()
                await broadcast(guild_id, data)

            elif msg_type == "worker-register":
                # A standalone worker process is announcing/refreshing its config.
                worker_id = data.get("workerId")
                repos = data.get("repos") or []
                if worker_id:
                    await db.execute(
                        update(Worker)
                        .where(Worker.id == worker_id, Worker.guild_id == guild_id)
                        .values(repos=json.dumps(repos))
                    )
                    await db.commit()

            elif msg_type == "worker-disconnect":
                # Worker is shutting down gracefully; mark agents and worker offline now
                # rather than waiting for the WebSocket to close.
                worker_id = data.get("workerId")
                for agent_id in joined_agents:
                    await db.execute(
                        "UPDATE agents SET state = 'offline' WHERE id = ? AND guild_id = ?",
                        (agent_id, guild_id),
                    )
                if worker_id:
                    await db.execute(
                        "UPDATE workers SET state = 'offline' WHERE id = ? AND guild_id = ?",
                        (worker_id, guild_id),
                    )
                if joined_agents or worker_id:
                    await db.commit()
                for agent_id in joined_agents:
                    await broadcast(guild_id, {
                        "type": "agent-state",
                        "agentId": agent_id,
                        "state": "offline",
                    })

            elif msg_type == "task-update":
                # Worker is reporting a task state change; persist + rebroadcast.
                task_id = data.get("taskId")
                if task_id:
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
                    if update_values:
                        await db.execute(
                            update(Task).where(Task.id == task_id).values(**update_values)
                        )
                        await db.commit()
                    await broadcast(guild_id, data, exclude=websocket)

            elif msg_type == "task-complete":
                task_id = data.get("taskId")
                worker_id_msg = data.get("workerId", "")
                desc = data.get("description", "")
                branch = data.get("branch", "")
                if task_id:
                    await db.execute(
                        update(Task)
                        .where(Task.id == task_id, Task.state == "working")
                        .values(state="awaiting-review")
                    )
                    await db.commit()
                await broadcast(guild_id, data, exclude=websocket)
                if task_id:
                    asyncio.create_task(_run_foreman_ai(
                        guild_id,
                        f"[task-complete] Worker {worker_id_msg} finished task {task_id}: "
                        f"\"{desc[:80]}\" — branch: {branch}. "
                        "Review this result. Call send_followup if additional work is needed "
                        "(e.g. update tests, add docs, fix lint errors). "
                        "Otherwise call finalize_task to mark it complete.",
                    ))

            elif msg_type == "task-followup-done":
                task_id = data.get("taskId")
                worker_id_msg = data.get("workerId", "")
                if task_id:
                    await db.execute(
                        update(Task).where(Task.id == task_id).values(state="awaiting-review")
                    )
                    await db.commit()
                await broadcast(guild_id, data, exclude=websocket)
                if task_id:
                    asyncio.create_task(_run_foreman_ai(
                        guild_id,
                        f"[followup-done] Worker {worker_id_msg} completed a follow-up for task {task_id}. "
                        "Decide: call send_followup for more work, or call finalize_task to mark it done.",
                    ))

            elif msg_type == "needs-input":
                # Worker escalation: broadcast to frontend and loop the foreman in.
                await broadcast(guild_id, data, exclude=websocket)
                wid = data.get("workerId", "a worker")
                task_id = data.get("taskId", "")
                description = data.get("description", "")
                stop_reason = data.get("stopReason", "")
                last_msg = data.get("lastMessage", "")
                escalation = (
                    f"Worker {wid} could not complete task {task_id} and needs your help.\n"
                    f"Task: {description}\n"
                    f"Stop reason: {stop_reason}"
                    + (f"\nLast message: {last_msg}" if last_msg else "")
                )
                asyncio.create_task(_run_foreman_ai(guild_id, escalation))

            elif msg_type in ("offer", "answer", "ice-candidate"):
                # WebRTC signaling - forward to all
                await broadcast(guild_id, data, exclude=websocket)

            else:
                # Generic broadcast
                await broadcast(guild_id, data)

    except WebSocketDisconnect:
        if guild_id in connections and websocket in connections[guild_id]:
            connections[guild_id].remove(websocket)
    except Exception:
        if guild_id in connections and websocket in connections[guild_id]:
            connections[guild_id].remove(websocket)
    finally:
        try:
            for agent_id in joined_agents:
                await db.execute(
                    update(Agent)
                    .where(Agent.id == agent_id, Agent.guild_id == guild_id)
                    .values(state="offline")
                )
                # Mirror into workers table so foreman sees the worker as offline.
                await db.execute(
                    update(Worker)
                    .where(Worker.id == agent_id, Worker.guild_id == guild_id)
                    .values(state="offline")
                )
            if joined_agents:
                await db.commit()
            for agent_id in joined_agents:
                await broadcast(guild_id, {
                    "type": "agent-state",
                    "agentId": agent_id,
                    "state": "offline",
                })
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Agent process management
# ---------------------------------------------------------------------------

async def _emit_terminal_line(guild_id: str, agent_id: str, line: str):
    """Broadcast and persist a terminal output line."""
    now = datetime.now(timezone.utc).isoformat()
    await broadcast(guild_id, {
        "type": "terminal-output",
        "agentId": agent_id,
        "line": line,
        "timestamp": now,
    })
    if line:
        db = await get_db()
        try:
            result = await db.execute(select(Agent.worker_id).where(Agent.id == agent_id))
            worker_id_for_log = result.scalar_one_or_none()
            db.add(TaskLog(task_id=None, timestamp=now, line=line,
                           worker_id=worker_id_for_log, agent_id=agent_id))
            await db.commit()
        finally:
            await db.close()


async def _set_agent_state(guild_id: str, agent_id: str, state: str):
    """Broadcast and persist an agent state change."""
    await broadcast(guild_id, {"type": "agent-state", "agentId": agent_id, "state": state})
    db = await get_db()
    try:
        await db.execute(
            update(Agent)
            .where(Agent.id == agent_id, Agent.guild_id == guild_id)
            .values(state=state)
        )
        await db.commit()
    finally:
        await db.close()


def _build_command(req: RunAgentRequest) -> tuple[list[str], bool]:
    """Return (cmd_list, needs_stdin_prompt).

    needs_stdin_prompt=True means we must write the RPC prompt to stdin
    (Pi RPC mode) rather than passing it on the command line.
    """
    tool = req.tool.lower()

    if tool == "claude":
        cmd = ["claude", "-p", req.prompt, "--output-format", "stream-json", "--verbose", "--max-turns", "20"]
        if req.model:
            cmd += ["--model", req.model]
        return cmd, False

    if tool == "codex":
        # codex exec --full-auto accepts a prompt as positional arg; --json for structured output
        cmd = ["codex", "exec", "--json", req.prompt]
        if req.model:
            cmd += ["--model", req.model]
        return cmd, False

    if tool == "pi":
        # Pi --mode rpc: bidirectional JSONL over stdin/stdout
        cmd = ["pi", "--mode", "rpc", "--no-session"]
        if req.provider:
            cmd += ["--provider", req.provider]
        if req.model:
            cmd += ["--model", req.model]
        return cmd, True

    raise ValueError(f"Unknown tool: {req.tool!r}")


async def _stream_agent(guild_id: str, agent_id: str, req: RunAgentRequest):
    """Spawn the agent subprocess and stream its output as terminal-output events."""
    tool = req.tool.lower()

    try:
        cmd, needs_stdin = _build_command(req)
    except ValueError as exc:
        await _emit_terminal_line(guild_id, agent_id, f"✗ {exc}")
        return

    stdin_pipe = asyncio.subprocess.PIPE if needs_stdin else asyncio.subprocess.DEVNULL
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=stdin_pipe,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        await _emit_terminal_line(guild_id, agent_id, f"✗ command not found: {cmd[0]}")
        await _set_agent_state(guild_id, agent_id, "error")
        return
    except Exception as exc:
        await _emit_terminal_line(guild_id, agent_id, f"✗ failed to start process: {exc}")
        await _set_agent_state(guild_id, agent_id, "error")
        return

    if needs_stdin:
        # Pi RPC: send the initial prompt as a JSON command, then leave stdin open
        rpc_msg = json.dumps({"type": "prompt", "content": req.prompt}) + "\n"
        proc.stdin.write(rpc_msg.encode())
        await proc.stdin.drain()

    running_processes[agent_id] = proc
    await _set_agent_state(guild_id, agent_id, "working")

    # For Pi message_update we track accumulated text to emit only deltas
    pi_last_text = ""

    async def _drain_stderr():
        async for raw in proc.stderr:  # type: ignore[union-attr]
            line = raw.decode(errors="replace").strip()
            if line:
                await _emit_terminal_line(guild_id, agent_id, f"[stderr] {line}")

    stderr_task = asyncio.create_task(_drain_stderr())

    try:
        async for raw_line in proc.stdout:
            line_str = raw_line.decode(errors="replace").strip()
            if not line_str:
                continue

            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                await _emit_terminal_line(guild_id, agent_id, line_str)
                continue

            text_out = _parse_event(tool, event, pi_last_text)

            # Pi: update delta baseline
            if tool == "pi" and event.get("type") == "message_update":
                full = ""
                for blk in event.get("message", {}).get("content", []):
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        full += blk.get("text", "")
                pi_last_text = full
            elif tool == "pi" and event.get("type") == "agent_end":
                pi_last_text = ""

            if text_out:
                await _emit_terminal_line(guild_id, agent_id, text_out)

    finally:
        if needs_stdin and proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        exit_code = await proc.wait()
        await stderr_task
        running_processes.pop(agent_id, None)
        await _set_agent_state(guild_id, agent_id, "idle" if exit_code == 0 else "error")


def _parse_event(tool: str, event: dict, pi_last_text: str) -> Optional[str]:
    """Extract a human-readable line from one stream-JSON / RPC event."""

    if tool == "claude":
        t = event.get("type")
        if t == "assistant":
            parts = []
            for blk in event.get("message", {}).get("content", []):
                btype = blk.get("type")
                if btype == "text":
                    txt = blk.get("text", "").strip()
                    if txt:
                        parts.append(txt)
                elif btype == "thinking":
                    thinking = blk.get("thinking", "").strip()
                    if thinking:
                        preview = thinking[:100].replace("\n", " ")
                        parts.append(f"[thinking] {preview}{'...' if len(thinking) > 100 else ''}")
                elif btype == "tool_use":
                    name = blk.get("name", "")
                    inp = blk.get("input", {})
                    if name == "Bash":
                        parts.append(f"▶ bash: {inp.get('command', '')[:120]}")
                    elif name in ("Read", "Write", "Edit"):
                        fp = inp.get("file_path", inp.get("path", ""))
                        parts.append(f"▶ {name.lower()}: {fp}")
                    else:
                        parts.append(f"▶ {name}: {json.dumps(inp)[:80]}")
            return "\n".join(parts) or None
        if t == "user":
            parts = []
            for blk in event.get("message", {}).get("content", []):
                if blk.get("type") == "tool_result":
                    content = blk.get("content", "")
                    if isinstance(content, list):
                        content = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
                    if isinstance(content, str) and content.strip():
                        lines = content.strip().split("\n")
                        preview = lines[0][:120]
                        if len(lines) > 1:
                            preview += f" (+{len(lines) - 1} lines)"
                        parts.append(f"  → {preview}")
            return "\n".join(parts) or None
        if t == "result":
            subtype = event.get("subtype", "success")
            turns = event.get("num_turns", 0)
            cost = event.get("cost_usd")
            cost_str = f" (${cost:.4f})" if cost else ""
            if subtype == "success":
                return f"✓ Done in {turns} turns{cost_str}"
            return f"✗ {subtype}: {event.get('error', '')}"
        if t == "system" and event.get("subtype") == "init":
            tools = event.get("tools", [])
            return f"[claude] tools: {', '.join(tools[:6])}"

    elif tool == "codex":
        t = event.get("type")
        if t == "message" and event.get("role") == "assistant":
            return (event.get("content") or "").strip() or None
        if t == "function_call":
            name = event.get("name", "")
            args = event.get("arguments", "")
            return f"▶ {name}({args[:80]})"
        if t == "function_result":
            return f"  → {str(event.get('output', ''))[:200]}"
        if t == "done":
            return "✓ Done"
        if t == "error":
            return f"✗ {event.get('message', '')}"

    elif tool == "pi":
        t = event.get("type")
        if t == "message_update":
            full = ""
            for blk in event.get("message", {}).get("content", []):
                if isinstance(blk, dict) and blk.get("type") == "text":
                    full += blk.get("text", "")
            delta = full[len(pi_last_text):]
            return delta if delta.strip() else None
        if t == "tool_execution_start":
            ti = event.get("tool", {})
            name = ti.get("name", "")
            inp = ti.get("input", {})
            if name == "bash":
                return f"▶ bash: {inp.get('command', '')[:120]}"
            if name in ("read", "write", "edit"):
                return f"▶ {name}: {inp.get('path', inp.get('file_path', ''))}"
            return f"▶ {name}({json.dumps(inp)[:80]})"
        if t == "tool_execution_end":
            out = str(event.get("output", "")).strip()
            if not out:
                return None
            lines = out.split("\n")
            preview = lines[0][:120]
            if len(lines) > 1:
                preview += f" (+{len(lines) - 1} lines)"
            return f"  → {preview}"
        if t == "agent_end":
            err = event.get("error")
            return f"✗ {err}" if err else None
        if t == "agent_start":
            return "[pi] agent started"

    return None


@app.post("/guilds/{guild_id}/agents/{agent_id}/run")
async def start_agent_run(guild_id: str, agent_id: str, req: RunAgentRequest):
    """Spawn an AI coding agent subprocess and stream its output over WebSocket."""
    old = running_processes.get(agent_id)
    if old:
        try:
            old.kill()
        except ProcessLookupError:
            pass

    asyncio.create_task(_stream_agent(guild_id, agent_id, req))
    return {"status": "started", "agentId": agent_id, "tool": req.tool}


@app.delete("/guilds/{guild_id}/agents/{agent_id}/run")
async def stop_agent_run(guild_id: str, agent_id: str):
    """Terminate a running agent subprocess."""
    proc = running_processes.get(agent_id)
    if not proc:
        raise HTTPException(status_code=404, detail="No running process for this agent")
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    return {"status": "stopped", "agentId": agent_id}


# ---------------------------------------------------------------------------
# Worker endpoints
# ---------------------------------------------------------------------------

@app.post("/guilds/{guild_id}/workers")
async def create_worker(guild_id: str, data: WorkerCreate):
    """Register a worker agent. The actual worker process must connect via WebSocket
    using the returned id (see the standalone /worker package)."""
    worker_id   = "w-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    created_at  = datetime.now(timezone.utc).isoformat()
    worker_name = _worker_name(worker_id)

    db = await get_db()
    try:
        db.add(Worker(
            id=worker_id,
            guild_id=guild_id,
            repos=json.dumps(data.repos),
            state="offline",
            created_at=created_at,
        ))
        stmt = sqlite_insert(Agent).values(
            id=worker_id,
            guild_id=guild_id,
            worker_id=worker_id,
            name=worker_name,
            type="worker",
            state="offline",
            joined_at=created_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "guild_id": stmt.excluded.guild_id,
                "worker_id": stmt.excluded.worker_id,
                "name": stmt.excluded.name,
                "type": stmt.excluded.type,
                "state": stmt.excluded.state,
                "joined_at": stmt.excluded.joined_at,
            },
        )
        await db.execute(stmt)
        await db.commit()
    finally:
        await db.close()

    await broadcast(guild_id, {
        "type": "agent-joined",
        "agentId": worker_id,
        "agentName": worker_name,
        "agentType": "worker",
        "state": "offline",
        "joinedAt": created_at,
    })
    return {"id": worker_id, "name": worker_name, "repos": data.repos, "created_at": created_at}


@app.get("/guilds/{guild_id}/workers")
async def list_workers(guild_id: str):
    db = await get_db()
    try:
        result = await db.execute(
            select(Worker).where(Worker.guild_id == guild_id).order_by(Worker.created_at.desc())
        )
        return [_row(w) for w in result.scalars().all()]
    finally:
        await db.close()


@app.post("/guilds/{guild_id}/workers/{worker_id}/tasks")
async def assign_task(guild_id: str, worker_id: str, data: TaskCreate):
    """Persist a task and broadcast a task-assigned event for the worker process."""
    task_id    = "t-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    created_at = datetime.now(timezone.utc).isoformat()

    db = await get_db()
    try:
        result = await db.execute(
            select(Worker.id).where(Worker.id == worker_id, Worker.guild_id == guild_id)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Worker not found")
        name = (data.name or data.description[:60])
        db.add(Task(
            id=task_id,
            worker_id=worker_id,
            guild_id=guild_id,
            name=name,
            description=data.description,
            tool=data.tool,
            issue_number=data.issue_number,
            issue_repo=data.issue_repo,
            state="pending",
            phase=data.phase or "execute",
            parent_task_id=data.parent_task_id,
            created_at=created_at,
        ))
        await db.commit()
    finally:
        await db.close()

    await broadcast(guild_id, {
        "type": "task-assigned",
        "workerId": worker_id,
        "taskId": task_id,
        "name": name,
        "description": data.description,
        "tool": data.tool,
        "phase": data.phase or "execute",
        "parentTaskId": data.parent_task_id,
        "issueNumber": data.issue_number,
        "issueRepo": data.issue_repo,
    })

    return {"id": task_id, "worker_id": worker_id, "state": "pending"}


@app.get("/guilds/{guild_id}/workers/{worker_id}/tasks")
async def list_tasks(guild_id: str, worker_id: str):
    db = await get_db()
    try:
        result = await db.execute(
            select(Task)
            .where(Task.worker_id == worker_id, Task.guild_id == guild_id)
            .order_by(Task.created_at.desc())
        )
        return [_row(t) for t in result.scalars().all()]
    finally:
        await db.close()


class WorkerMessage(BaseModel):
    message: str


@app.post("/guilds/{guild_id}/workers/{worker_id}/message")
async def message_worker(guild_id: str, worker_id: str, data: WorkerMessage):
    """Forward a message to a worker process via its guild WebSocket."""
    text_msg = data.message.strip()
    if not text_msg:
        raise HTTPException(status_code=400, detail="Empty message")

    await _emit_terminal_line(guild_id, worker_id, f"[foreman → worker] {text_msg}")
    await broadcast(guild_id, {
        "type": "worker-message",
        "workerId": worker_id,
        "message": text_msg,
    })
    return {"status": "delivered"}


# ---------------------------------------------------------------------------
# Task endpoints
# ---------------------------------------------------------------------------

@app.get("/guilds/{guild_id}/tasks")
async def list_guild_tasks(guild_id: str):
    """List all tasks for a guild, most recent first."""
    db = await get_db()
    try:
        result = await db.execute(
            select(
                Task.id, Task.worker_id, Task.name, Task.description, Task.tool, Task.state,
                Task.phase, Task.parent_task_id, Task.branch, Task.pr_url,
                Task.issue_number, Task.issue_repo, Task.created_at, Task.finished_at,
            )
            .where(Task.guild_id == guild_id)
            .order_by(Task.created_at.desc())
            .limit(100)
        )
        return [dict(r._mapping) for r in result.fetchall()]
    finally:
        await db.close()


@app.get("/guilds/{guild_id}/tasks/{task_id}/logs")
async def get_task_logs(guild_id: str, task_id: str):
    """Get all saved log lines for a task."""
    db = await get_db()
    try:
        result = await db.execute(
            select(Task.id).where(Task.id == task_id, Task.guild_id == guild_id)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Task not found")
        result = await db.execute(
            select(TaskLog.timestamp, TaskLog.line, TaskLog.worker_id,
                   TaskLog.agent_id, TaskLog.log_id, TaskLog.data)
            .where(TaskLog.task_id == task_id)
            .order_by(TaskLog.id.asc())
        )
        rows = []
        for r in result.fetchall():
            row = dict(r._mapping)
            raw = row.pop("data", None)
            if raw:
                try:
                    row["detail"] = json.loads(raw)
                except Exception:
                    pass
            rows.append(row)
        return rows
    finally:
        await db.close()


@app.get("/guilds/{guild_id}/logs")
async def get_guild_logs(
    guild_id: str,
    worker_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    task_id: Optional[str] = None,
):
    """Get task_logs filtered by worker_id, agent_id, or task_id."""
    if not (worker_id or agent_id or task_id):
        raise HTTPException(status_code=400, detail="Specify worker_id, agent_id, or task_id")
    db = await get_db()
    try:
        result = await db.execute(select(Guild.id).where(Guild.id == guild_id))
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Guild not found")
        stmt = select(
            TaskLog.timestamp, TaskLog.line, TaskLog.worker_id, TaskLog.agent_id, TaskLog.task_id
        )
        if task_id:
            stmt = stmt.where(TaskLog.task_id == task_id)
        elif worker_id:
            stmt = stmt.where(TaskLog.worker_id == worker_id)
        else:
            stmt = stmt.where(TaskLog.agent_id == agent_id)
        stmt = stmt.order_by(TaskLog.id.asc())
        result = await db.execute(stmt)
        return [dict(r._mapping) for r in result.fetchall()]
    finally:
        await db.close()


class FollowupCreate(BaseModel):
    instructions: str


@app.post("/guilds/{guild_id}/tasks/{task_id}/followup")
async def create_task_followup(guild_id: str, task_id: str, data: FollowupCreate):
    """Send follow-up instructions to a worker — executed in the same worktree/branch."""
    db = await get_db()
    try:
        result = await db.execute(
            select(Task.worker_id).where(Task.id == task_id, Task.guild_id == guild_id)
        )
        worker_id = result.scalar_one_or_none()
        if not worker_id:
            raise HTTPException(status_code=404, detail="Task not found")
        await db.execute(
            update(Task).where(Task.id == task_id).values(state="working", phase="followup")
        )
        await db.commit()
    finally:
        await db.close()
    await broadcast(guild_id, {
        "type": "task-followup",
        "workerId": worker_id,
        "taskId": task_id,
        "instructions": data.instructions,
    })
    return {"status": "sent", "taskId": task_id}


@app.post("/guilds/{guild_id}/tasks/{task_id}/finalize")
async def finalize_task_endpoint(guild_id: str, task_id: str):
    """Signal a worker to finalize a task — no more follow-ups."""
    db = await get_db()
    try:
        result = await db.execute(
            select(Task.worker_id).where(Task.id == task_id, Task.guild_id == guild_id)
        )
        worker_id = result.scalar_one_or_none()
        if not worker_id:
            raise HTTPException(status_code=404, detail="Task not found")
        finished_at = datetime.now(timezone.utc).isoformat()
        await db.execute(
            update(Task).where(Task.id == task_id).values(state="done", finished_at=finished_at)
        )
        await db.commit()
    finally:
        await db.close()
    await broadcast(guild_id, {
        "type": "task-finalize",
        "workerId": worker_id,
        "taskId": task_id,
    })
    await broadcast(guild_id, {
        "type": "task-update",
        "taskId": task_id,
        "state": "done",
        "finishedAt": finished_at,
    })
    return {"status": "finalized", "taskId": task_id}


@app.post("/guilds/{guild_id}/tasks/{task_id}/cancel")
async def cancel_task_endpoint(guild_id: str, task_id: str):
    """Cancel a running or pending task — terminates the worker's Claude subprocess."""
    db = await get_db()
    try:
        result = await db.execute(
            select(Task.worker_id, Task.state).where(Task.id == task_id, Task.guild_id == guild_id)
        )
        row = result.one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        worker_id, state = row
        if state in ("done", "failed", "cancelled"):
            raise HTTPException(status_code=409, detail=f"Task is already {state}")
        finished_at = datetime.now(timezone.utc).isoformat()
        await db.execute(
            update(Task).where(Task.id == task_id).values(state="cancelled", finished_at=finished_at)
        )
        await db.commit()
    finally:
        await db.close()
    await broadcast(guild_id, {
        "type": "task-cancel",
        "workerId": worker_id,
        "taskId": task_id,
    })
    await broadcast(guild_id, {
        "type": "task-update",
        "taskId": task_id,
        "state": "cancelled",
        "finishedAt": finished_at,
    })
    return {"status": "cancelled", "taskId": task_id}


class RedirectCreate(BaseModel):
    instructions: str


@app.post("/guilds/{guild_id}/tasks/{task_id}/redirect")
async def redirect_task_endpoint(guild_id: str, task_id: str, data: RedirectCreate):
    """Redirect a running task: SIGTERM the Claude subprocess and resume it with new instructions."""
    instructions = data.instructions.strip()
    if not instructions:
        raise HTTPException(status_code=400, detail="instructions required")
    db = await get_db()
    try:
        result = await db.execute(
            select(Task.worker_id, Task.state).where(Task.id == task_id, Task.guild_id == guild_id)
        )
        row = result.one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        worker_id, state = row
        if state in ("done", "failed", "cancelled"):
            raise HTTPException(status_code=409, detail=f"Task is already {state}")
        await db.execute(
            update(Task).where(Task.id == task_id).values(state="working")
        )
        await db.commit()
    finally:
        await db.close()
    await broadcast(guild_id, {
        "type": "task-redirect",
        "workerId": worker_id,
        "taskId": task_id,
        "instructions": instructions,
    })
    await broadcast(guild_id, {
        "type": "task-update",
        "taskId": task_id,
        "state": "working",
    })
    return {"status": "redirected", "taskId": task_id}
