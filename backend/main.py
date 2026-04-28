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

import aiosqlite
from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

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

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Workers run as standalone processes (see /worker package) and connect over
    # WebSocket; no in-process recovery is needed here.
    yield

app = FastAPI(title="Pioneer Square", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "pioneer_square.db"

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


async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db():
    db = await get_db()

    # Migrate: rename sessions table to guilds
    try:
        await db.execute("ALTER TABLE sessions RENAME TO guilds")
        await db.commit()
    except Exception:
        pass

    # Migrate: rename session_id columns to guild_id
    for table in ("agents", "messages", "workers", "tasks"):
        try:
            await db.execute(f"ALTER TABLE {table} RENAME COLUMN session_id TO guild_id")
            await db.commit()
        except Exception:
            pass

    await db.execute("""
        CREATE TABLE IF NOT EXISTS guilds (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            name TEXT,
            github_user_id TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            worker_id TEXT,
            name TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'worker',
            state TEXT NOT NULL DEFAULT 'idle',
            joined_at TEXT NOT NULL,
            FOREIGN KEY (guild_id) REFERENCES guilds(id),
            FOREIGN KEY (worker_id) REFERENCES workers(id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            from_agent TEXT,
            to_agent TEXT,
            content TEXT NOT NULL,
            message_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (guild_id) REFERENCES guilds(id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            repos TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL DEFAULT 'idle',
            created_at TEXT NOT NULL,
            FOREIGN KEY (guild_id) REFERENCES guilds(id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            guild_id TEXT NOT NULL,
            description TEXT NOT NULL,
            tool TEXT NOT NULL DEFAULT 'claude',
            issue_number INTEGER,
            issue_repo TEXT,
            state TEXT NOT NULL DEFAULT 'pending',
            branch TEXT,
            worktree_path TEXT,
            pr_url TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY (worker_id) REFERENCES workers(id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS github_tokens (
            github_user_id TEXT PRIMARY KEY,
            github_username TEXT,
            access_token TEXT NOT NULL,
            token_type TEXT NOT NULL DEFAULT 'bearer',
            scope TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            token TEXT PRIMARY KEY,
            github_user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (github_user_id) REFERENCES github_tokens(github_user_id)
        )
    """)
    for col_sql in [
        "ALTER TABLE guilds ADD COLUMN github_user_id TEXT",
        "ALTER TABLE agents ADD COLUMN worker_id TEXT REFERENCES workers(id)",
    ]:
        try:
            await db.execute(col_sql)
        except aiosqlite.OperationalError:
            pass
    try:
        await db.execute("ALTER TABLE tasks ADD COLUMN tool TEXT NOT NULL DEFAULT 'claude'")
        await db.commit()
    except Exception:
        pass
    await db.commit()
    await db.close()


def generate_guild_id():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _worker_name(worker_id: str) -> str:
    raw = worker_id[2:].upper()
    split = 2 + sum(ord(c) for c in raw) % 3
    return f"{raw[:split]}-{raw[split:]}"


class GuildCreate(BaseModel):
    name: Optional[str] = None


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
    tool: str = "claude"          # "claude" | "codex" | "pi"
    issue_number: Optional[int] = None
    issue_repo: Optional[str] = None


# ---------------------------------------------------------------------------
# Worker tasks live in the standalone /worker package; this backend only
# persists worker/task state and dispatches assignments over WebSocket.
# ---------------------------------------------------------------------------


FOREMAN_SYSTEM = """\
You are the Foreman AI in Pioneer Square, a multi-agent coding workshop.
You coordinate worker agents that autonomously clone repos, write code, and open PRs.

Your responsibilities:
- Understand what the human wants done and break it into concrete tasks
- Assign tasks to appropriate idle workers via assign_task
- Message a specific worker to give it mid-task context via message_worker
- Summarise worker status and recent task outcomes when asked
- Escalate back to the human only when you genuinely cannot decide

Workers are already configured with a list of repos. Prefer workers whose repo
list covers the task. If multiple workers are idle, split work across them.

Be concise — one short paragraph maximum per response unless detail is requested.\
"""

FOREMAN_TOOLS = [
    {
        "name": "assign_task",
        "description": (
            "Queue a coding task for a worker agent. The worker will create a git worktree, "
            "run the chosen coding agent (claude, codex, or pi) on the task description, then push and "
            "open a GitHub PR."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "worker_id": {
                    "type": "string",
                    "description": "Worker agent ID (e.g. w-abc123). Must be an idle worker.",
                },
                "description": {
                    "type": "string",
                    "description": "Detailed, self-contained task description the coding agent will receive.",
                },
                "tool": {
                    "type": "string",
                    "enum": ["claude", "codex", "pi"],
                    "description": "Which coding agent to use. Defaults to claude.",
                },
                "issue_number": {"type": "integer", "description": "GitHub issue number to close (optional)."},
                "issue_repo": {"type": "string", "description": "owner/repo for the issue (optional)."},
            },
            "required": ["worker_id", "description"],
        },
    },
    {
        "name": "message_worker",
        "description": "Send a message to a specific worker's terminal — useful to provide mid-task context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "worker_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["worker_id", "message"],
        },
    },
]


async def _foreman_exec_tools(guild_id: str, tool_uses: list) -> list:
    """Execute tool calls from the foreman AI and return tool-result blocks."""
    results = []
    for tu in tool_uses:
        inp = tu.input
        result_text = ""

        if tu.name == "assign_task":
            wid  = inp["worker_id"]
            desc = inp["description"]
            task_id    = "t-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
            created_at = datetime.now(timezone.utc).isoformat()
            db = await get_db()
            try:
                async with db.execute(
                    "SELECT 1 FROM workers WHERE id=? AND guild_id=?", (wid, guild_id)
                ) as cur:
                    worker_row = await cur.fetchone()
                if not worker_row:
                    result_text = f"Worker {wid} not found — task NOT queued."
                else:
                    tool = inp.get("tool", "claude")
                    await db.execute(
                        "INSERT INTO tasks (id, worker_id, guild_id, description, tool, issue_number,"
                        " issue_repo, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                        (task_id, wid, guild_id, desc, tool, inp.get("issue_number"), inp.get("issue_repo"), created_at),
                    )
                    await db.commit()
                    await broadcast(guild_id, {
                        "type": "task-assigned",
                        "workerId": wid,
                        "taskId": task_id,
                        "description": desc,
                        "tool": tool,
                        "issueNumber": inp.get("issue_number"),
                        "issueRepo": inp.get("issue_repo"),
                    })
                    result_text = f"Task {task_id} queued for {wid}."
            finally:
                await db.close()

        elif tu.name == "message_worker":
            wid = inp["worker_id"]
            msg = inp["message"]
            await _emit_terminal_line(guild_id, wid, f"[foreman] {msg}")
            # The worker process picks this up over its guild WebSocket.
            await broadcast(guild_id, {
                "type": "worker-message",
                "workerId": wid,
                "message": msg,
            })
            result_text = f"Message delivered to {wid}."

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
        async with db.execute(
            "SELECT w.id, w.repos, w.state as worker_state,"
            " COUNT(a.id) as agent_count,"
            " GROUP_CONCAT(a.id || ':' || a.state) as agents"
            " FROM workers w"
            " LEFT JOIN agents a ON a.worker_id = w.id AND a.state != 'offline'"
            " WHERE w.guild_id=?"
            " GROUP BY w.id"
            " UNION ALL"
            " SELECT a.id, '[]', a.state, 1, a.id || ':' || a.state"
            " FROM agents a"
            " WHERE a.guild_id=? AND a.type='worker' AND a.worker_id IS NULL AND a.state != 'offline'",
            (guild_id, guild_id)
        ) as cur:
            worker_rows = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT id, worker_id, description, state, branch, pr_url FROM tasks"
            " WHERE guild_id=? ORDER BY created_at DESC LIMIT 10", (guild_id,)
        ) as cur:
            task_rows = [dict(r) for r in await cur.fetchall()]
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
    if len(history) > 40:
        history = history[-40:]

    client = _anthropic.AsyncAnthropic()

    try:
        # Turn 1 — foreman reasons and optionally calls tools
        resp1 = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=history,
            tools=FOREMAN_TOOLS,
        )
        history.append({"role": "assistant", "content": resp1.content})

        text_parts = [b.text for b in resp1.content if b.type == "text" and b.text.strip()]
        tool_uses  = [b for b in resp1.content if b.type == "tool_use"]

        if tool_uses:
            tool_results = await _foreman_exec_tools(guild_id, tool_uses)
            history.append({"role": "user", "content": tool_results})

            # Turn 2 — final reply after tool execution (no tools allowed)
            resp2 = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=system,
                messages=history,
            )
            history.append({"role": "assistant", "content": resp2.content})
            text_parts += [b.text for b in resp2.content if b.type == "text" and b.text.strip()]

        # Trim history
        foreman_conversations[guild_id] = history[-40:]

        response_text = "\n".join(text_parts).strip()
        if response_text:
            now = datetime.now(timezone.utc).isoformat()
            await broadcast(guild_id, {
                "type": "chat", "from": "foreman", "to": "user",
                "content": response_text, "createdAt": now,
            })
            db = await get_db()
            try:
                await db.execute(
                    "INSERT INTO messages (guild_id, from_agent, to_agent, content, message_type, created_at)"
                    " VALUES (?, 'foreman', 'user', ?, 'chat', ?)",
                    (guild_id, response_text, now),
                )
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
        async with db.execute(
            "SELECT github_user_id FROM user_sessions WHERE token=?", (token,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Invalid or expired session token")
        return row["github_user_id"]
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
        await db.execute(
            """
            INSERT INTO github_tokens (github_user_id, github_username, access_token, token_type, scope, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(github_user_id) DO UPDATE SET
                github_username=excluded.github_username,
                access_token=excluded.access_token,
                token_type=excluded.token_type,
                scope=excluded.scope,
                updated_at=excluded.updated_at
            """,
            (
                github_user_id, github_username, access_token,
                token_data.get("token_type", "bearer"),
                token_data.get("scope", ""),
                now, now,
            ),
        )
        await db.execute(
            "INSERT INTO user_sessions (token, github_user_id, created_at) VALUES (?, ?, ?)",
            (login_token, github_user_id, now),
        )
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
        async with db.execute(
            "SELECT github_user_id FROM guilds WHERE id=?", (guild_id,)
        ) as cur:
            guild_row = await cur.fetchone()
        if not guild_row or not guild_row["github_user_id"]:
            raise HTTPException(status_code=404, detail="No GitHub account linked to this guild")
        async with db.execute(
            "SELECT access_token, github_username FROM github_tokens WHERE github_user_id=?",
            (guild_row["github_user_id"],),
        ) as cur:
            token_row = await cur.fetchone()
        if not token_row:
            raise HTTPException(status_code=404, detail="GitHub token not found")
        return {"access_token": token_row["access_token"], "username": token_row["github_username"]}
    finally:
        await db.close()


@app.get("/auth/me")
async def get_me(github_user_id: str = Depends(require_user)):
    """Return the currently authenticated user's info."""
    db = await get_db()
    try:
        async with db.execute(
            "SELECT github_user_id, github_username, scope FROM github_tokens WHERE github_user_id=?",
            (github_user_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        return {
            "github_user_id": row["github_user_id"],
            "github_username": row["github_username"],
            "scope": row["scope"],
        }
    finally:
        await db.close()


@app.delete("/auth/logout")
async def logout(credentials: HTTPAuthorizationCredentials = Depends(_http_bearer)):
    """Invalidate the current login_token."""
    if credentials:
        db = await get_db()
        try:
            await db.execute("DELETE FROM user_sessions WHERE token=?", (credentials.credentials,))
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
                await db.execute(
                    "INSERT INTO guilds (id, created_at, name, github_user_id) VALUES (?, ?, ?, ?)",
                    (guild_id, created_at, data.name or f"Guild {guild_id}", github_user_id)
                )
                await db.commit()
                break
            except aiosqlite.IntegrityError:
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
        async with db.execute("""
            SELECT s.id, s.created_at, s.name,
                   COUNT(a.id) as agent_count
            FROM guilds s
            LEFT JOIN agents a ON a.guild_id = s.id AND a.type != 'foreman'
            WHERE s.github_user_id = ?
            GROUP BY s.id
            ORDER BY s.created_at DESC
        """, (github_user_id,)) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


@app.get("/guilds/{guild_id}")
async def get_guild(guild_id: str):
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM guilds WHERE id = ?", (guild_id,)) as cursor:
            guild = await cursor.fetchone()
        if not guild:
            raise HTTPException(status_code=404, detail="Guild not found")
        async with db.execute(
            "SELECT * FROM agents WHERE guild_id = ? AND state != 'offline' AND type != 'foreman'",
            (guild_id,)
        ) as cursor:
            agents = await cursor.fetchall()
        async with db.execute(
            "SELECT * FROM messages WHERE guild_id = ? ORDER BY created_at DESC LIMIT 100",
            (guild_id,)
        ) as cursor:
            messages = await cursor.fetchall()
        return {
            **dict(guild),
            "agents": [dict(a) for a in agents],
            "messages": [dict(m) for m in reversed(messages)]
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
                await db.execute(
                    """INSERT OR REPLACE INTO agents (id, guild_id, worker_id, name, type, state, joined_at)
                       VALUES (?, ?, ?, ?, ?, 'idle', ?)""",
                    (agent_id, guild_id, worker_id, agent_name, agent_type, joined_at)
                )
                # Mark worker online when it (re)connects.
                if agent_type == "worker" and worker_id:
                    await db.execute(
                        "UPDATE workers SET state = 'online' WHERE id = ? AND guild_id = ?",
                        (worker_id, guild_id),
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
                    "UPDATE agents SET state = ? WHERE id = ? AND guild_id = ?",
                    (state, agent_id, guild_id)
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
                await db.execute(
                    "INSERT INTO messages (guild_id, from_agent, to_agent, content, message_type, created_at) VALUES (?, ?, ?, ?, 'chat', ?)",
                    (guild_id, from_agent, to_agent, content, created_at)
                )
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
                agent_id = data.get("agentId")
                line = data.get("line", "")
                created_at = datetime.now(timezone.utc).isoformat()
                await broadcast(guild_id, {
                    "type": "terminal-output",
                    "agentId": agent_id,
                    "line": line,
                    "timestamp": created_at
                })

            elif msg_type == "worker-register":
                # A standalone worker process is announcing/refreshing its config.
                worker_id = data.get("workerId")
                repos = data.get("repos") or []
                if worker_id:
                    await db.execute(
                        "UPDATE workers SET repos=? WHERE id=? AND guild_id=?",
                        (json.dumps(repos), worker_id, guild_id),
                    )
                    await db.commit()

            elif msg_type == "task-update":
                # Worker is reporting a task state change; persist + rebroadcast.
                task_id = data.get("taskId")
                if task_id:
                    fields: list[tuple[str, object]] = []
                    for src, col in (
                        ("state", "state"),
                        ("branch", "branch"),
                        ("worktreePath", "worktree_path"),
                        ("prUrl", "pr_url"),
                        ("finishedAt", "finished_at"),
                    ):
                        if src in data:
                            fields.append((col, data[src]))
                    if fields:
                        sql = "UPDATE tasks SET " + ", ".join(f"{c}=?" for c, _ in fields) + " WHERE id=?"
                        params = [v for _, v in fields] + [task_id]
                        await db.execute(sql, params)
                        await db.commit()
                    await broadcast(guild_id, data, exclude=websocket)

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
                    "UPDATE agents SET state = 'offline' WHERE id = ? AND guild_id = ?",
                    (agent_id, guild_id),
                )
                # Mirror into workers table so foreman sees the worker as offline.
                await db.execute(
                    "UPDATE workers SET state = 'offline' WHERE id = ? AND guild_id = ?",
                    (agent_id, guild_id),
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
    """Broadcast a terminal output line."""
    now = datetime.now(timezone.utc).isoformat()
    await broadcast(guild_id, {
        "type": "terminal-output",
        "agentId": agent_id,
        "line": line,
        "timestamp": now,
    })


async def _set_agent_state(guild_id: str, agent_id: str, state: str):
    """Broadcast and persist an agent state change."""
    await broadcast(guild_id, {"type": "agent-state", "agentId": agent_id, "state": state})
    db = await get_db()
    try:
        await db.execute(
            "UPDATE agents SET state = ? WHERE id = ? AND guild_id = ?",
            (state, agent_id, guild_id),
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

            text = _parse_event(tool, event, pi_last_text)

            # Pi: update delta baseline
            if tool == "pi" and event.get("type") == "message_update":
                full = ""
                for blk in event.get("message", {}).get("content", []):
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        full += blk.get("text", "")
                pi_last_text = full
            elif tool == "pi" and event.get("type") == "agent_end":
                pi_last_text = ""

            if text:
                await _emit_terminal_line(guild_id, agent_id, text)

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
        await db.execute(
            "INSERT INTO workers (id, guild_id, repos, state, created_at) VALUES (?, ?, ?, 'offline', ?)",
            (worker_id, guild_id, json.dumps(data.repos), created_at),
        )
        await db.execute(
            "INSERT OR REPLACE INTO agents (id, guild_id, worker_id, name, type, state, joined_at)"
            " VALUES (?, ?, ?, ?, 'worker', 'offline', ?)",
            (worker_id, guild_id, worker_id, worker_name, created_at),
        )
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
        async with db.execute(
            "SELECT * FROM workers WHERE guild_id = ? ORDER BY created_at DESC", (guild_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@app.post("/guilds/{guild_id}/workers/{worker_id}/tasks")
async def assign_task(guild_id: str, worker_id: str, data: TaskCreate):
    """Persist a task and broadcast a task-assigned event for the worker process."""
    task_id    = "t-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    created_at = datetime.now(timezone.utc).isoformat()

    db = await get_db()
    try:
        async with db.execute(
            "SELECT 1 FROM workers WHERE id=? AND guild_id=?", (worker_id, guild_id)
        ) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Worker not found")
        await db.execute(
            "INSERT INTO tasks (id, worker_id, guild_id, description, tool, issue_number, issue_repo, state, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (task_id, worker_id, guild_id, data.description, data.tool, data.issue_number, data.issue_repo, created_at),
        )
        await db.commit()
    finally:
        await db.close()

    await broadcast(guild_id, {
        "type": "task-assigned",
        "workerId": worker_id,
        "taskId": task_id,
        "description": data.description,
        "tool": data.tool,
        "issueNumber": data.issue_number,
        "issueRepo": data.issue_repo,
    })

    return {"id": task_id, "worker_id": worker_id, "state": "pending"}


@app.get("/guilds/{guild_id}/workers/{worker_id}/tasks")
async def list_tasks(guild_id: str, worker_id: str):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT * FROM tasks WHERE worker_id = ? AND guild_id = ? ORDER BY created_at DESC",
            (worker_id, guild_id),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


class WorkerMessage(BaseModel):
    message: str


@app.post("/guilds/{guild_id}/workers/{worker_id}/message")
async def message_worker(guild_id: str, worker_id: str, data: WorkerMessage):
    """Forward a message to a worker process via its guild WebSocket."""
    text = data.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty message")

    await _emit_terminal_line(guild_id, worker_id, f"[foreman → worker] {text}")
    await broadcast(guild_id, {
        "type": "worker-message",
        "workerId": worker_id,
        "message": text,
    })
    return {"status": "delivered"}
