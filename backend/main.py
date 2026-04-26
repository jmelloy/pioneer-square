import asyncio
import json
import os
import re
import random
import string
import urllib.request
import urllib.error
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiosqlite
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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

DB_PATH = "pioneer_square.db"
REPOS_DIR = os.environ.get("PIONEER_REPOS", "/tmp/pioneer-repos")
WORK_DIR  = os.environ.get("PIONEER_WORK",  "/tmp/pioneer-work")

# In-memory WebSocket connections: session_id -> list of WebSocket
connections: Dict[str, List[WebSocket]] = {}

# Running agent subprocesses: agent_id -> asyncio.subprocess.Process
running_processes: Dict[str, asyncio.subprocess.Process] = {}

# Worker state (not persisted across restarts)
worker_configs:     Dict[str, dict]           = {}   # worker_id -> {session_id, repos, token}
worker_task_queues: Dict[str, asyncio.Queue]  = {}   # worker_id -> Queue[task_dict]
worker_loops:       Dict[str, asyncio.Task]   = {}   # worker_id -> asyncio.Task


async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db():
    db = await get_db()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            name TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'worker',
            state TEXT NOT NULL DEFAULT 'idle',
            joined_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            from_agent TEXT,
            to_agent TEXT,
            content TEXT NOT NULL,
            message_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            repos TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL DEFAULT 'idle',
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            description TEXT NOT NULL,
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
    await db.commit()
    await db.close()


def generate_session_id():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))


class SessionCreate(BaseModel):
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
    issue_number: Optional[int] = None
    issue_repo: Optional[str] = None


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

async def _run_git(args: list, cwd: str = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def _ensure_repo(repo_full: str, token: str = None) -> Optional[str]:
    """Clone repo if not present; otherwise pull latest main. Returns local path or None."""
    parts = repo_full.split("/", 1)
    if len(parts) != 2:
        return None
    owner, name = parts
    local_path = os.path.join(REPOS_DIR, owner, name)

    if token:
        remote_url = f"https://{token}@github.com/{repo_full}.git"
    else:
        remote_url = f"https://github.com/{repo_full}.git"

    if not os.path.exists(os.path.join(local_path, ".git")):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        rc, _, err = await _run_git(["clone", remote_url, local_path])
        if rc != 0:
            return None
    else:
        # Fetch + fast-forward default branch
        await _run_git(["fetch", "origin"], cwd=local_path)
        await _run_git(["merge", "--ff-only", "origin/HEAD"], cwd=local_path)

    return local_path


async def _create_worktree(repo_path: str, wt_path: str, branch: str) -> bool:
    """Create a git worktree at wt_path on a new branch from origin/HEAD."""
    os.makedirs(os.path.dirname(wt_path), exist_ok=True)
    # Fetch so origin/HEAD is current
    await _run_git(["fetch", "origin"], cwd=repo_path)
    rc, _, _ = await _run_git(
        ["worktree", "add", "-b", branch, wt_path, "origin/HEAD"],
        cwd=repo_path,
    )
    return rc == 0


async def _pull_repos(session_id: str, worker_id: str, repos: list, token: str = None):
    for repo_full in repos:
        parts = repo_full.split("/", 1)
        if len(parts) != 2:
            continue
        owner, name = parts
        local_path = os.path.join(REPOS_DIR, owner, name)
        if not os.path.exists(os.path.join(local_path, ".git")):
            await _emit_terminal_line(session_id, worker_id, f"[worker] Cloning {repo_full}...")
            await _ensure_repo(repo_full, token)
        else:
            rc, _, err = await _run_git(["fetch", "origin"], cwd=local_path)
            await _run_git(["merge", "--ff-only", "origin/HEAD"], cwd=local_path)
            msg = f"[worker] Pulled {repo_full}" if rc == 0 else f"[worker] Pull warn {repo_full}: {err.strip()[:60]}"
            await _emit_terminal_line(session_id, worker_id, msg)


# ---------------------------------------------------------------------------
# Claude auto-mode runner
# ---------------------------------------------------------------------------

async def _run_claude_auto(
    session_id: str, worker_id: str, task_id: str, description: str, cwd: str
) -> bool:
    """Run `claude --dangerously-skip-permissions` on *description* in *cwd*.
    Streams output as terminal lines. Returns True on exit code 0."""
    cmd = [
        "claude",
        "--output-format", "stream-json",
        "--max-turns", "50",
        "--dangerously-skip-permissions",
        "-p", description,
    ]
    await _emit_terminal_line(session_id, worker_id, f"[claude] Starting: {description[:80]}")
    key = f"{worker_id}:{task_id}"
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        running_processes[key] = proc

        async for raw in proc.stdout:
            line_str = raw.decode(errors="replace").strip()
            if not line_str:
                continue
            try:
                event = json.loads(line_str)
                text = _parse_event("claude", event, "")
                if text:
                    await _emit_terminal_line(session_id, worker_id, text)
            except json.JSONDecodeError:
                await _emit_terminal_line(session_id, worker_id, line_str)

        exit_code = await proc.wait()
        return exit_code == 0
    except Exception as exc:
        await _emit_terminal_line(session_id, worker_id, f"[claude] ✗ {exc}")
        return False
    finally:
        running_processes.pop(key, None)


# ---------------------------------------------------------------------------
# Git push + GitHub PR creation
# ---------------------------------------------------------------------------

async def _push_and_pr(
    session_id: str,
    worker_id: str,
    task: dict,
    branch: str,
    worktree_path: str,
    token: str = None,
) -> Optional[str]:
    """Push branch to origin, create a GitHub PR. Returns PR URL or None."""
    await _emit_terminal_line(session_id, worker_id, f"[worker] Pushing {branch}...")
    rc, _, err = await _run_git(["push", "-u", "origin", branch], cwd=worktree_path)
    if rc != 0:
        await _emit_terminal_line(session_id, worker_id, f"[worker] ✗ Push failed: {err.strip()[:120]}")
        return None
    await _emit_terminal_line(session_id, worker_id, f"[worker] ✓ Pushed {branch}")

    if not token:
        return None

    # Resolve repo from task or worktree remote
    repo_full = task.get("issue_repo")
    if not repo_full:
        rc2, url, _ = await _run_git(["remote", "get-url", "origin"], cwd=worktree_path)
        if rc2 == 0:
            m = re.search(r"github\.com[:/](.+?/[^/\s]+?)(?:\.git)?$", url.strip())
            if m:
                repo_full = m.group(1)
    if not repo_full:
        return None

    issue_ref = f"\n\nCloses #{task['issue_number']}" if task.get("issue_number") else ""
    body = f"Automated by Pioneer Square worker agent.{issue_ref}"
    payload = json.dumps({
        "title": task["description"][:72],
        "body": body,
        "head": branch,
        "base": "main",
    }).encode()

    def _create_pr():
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo_full}/pulls",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    try:
        result = await asyncio.to_thread(_create_pr)
        pr_url = result.get("html_url", "")
        await _emit_terminal_line(session_id, worker_id, f"[worker] ✓ PR: {pr_url}")
        await broadcast(session_id, {
            "type": "task-complete",
            "workerId": worker_id,
            "taskId": task["id"],
            "prUrl": pr_url,
            "branch": branch,
            "description": task["description"],
        })
        return pr_url
    except Exception as exc:
        await _emit_terminal_line(session_id, worker_id, f"[worker] PR failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Worker loop + task execution
# ---------------------------------------------------------------------------

async def _execute_worker_task(session_id: str, worker_id: str, task: dict):
    task_id   = task["id"]
    desc      = task["description"]
    config    = worker_configs.get(worker_id, {})
    repos     = config.get("repos", [])
    token     = config.get("token")

    await _set_agent_state(session_id, worker_id, "working")
    db = await get_db()
    await db.execute("UPDATE tasks SET state='working' WHERE id=?", (task_id,))
    await db.commit()
    await db.close()

    # Build branch name from description
    slug   = re.sub(r"[^a-z0-9]+", "-", desc[:40].lower()).strip("-")
    branch = f"claude/{slug}-{task_id[:6]}"

    work_dir = os.path.join(WORK_DIR, session_id, worker_id, task_id)
    os.makedirs(work_dir, exist_ok=True)

    await _emit_terminal_line(session_id, worker_id, f"[worker] Task: {desc}")
    await _emit_terminal_line(session_id, worker_id, f"[worker] Branch: {branch}")

    # Clone/pull each repo and create a worktree
    worktree_entries: list[tuple[str, str, str]] = []  # (repo_full, repo_path, wt_path)
    primary_wt = None

    for repo_full in repos:
        await _emit_terminal_line(session_id, worker_id, f"[worker] Preparing {repo_full}...")
        repo_path = await _ensure_repo(repo_full, token)
        if not repo_path:
            await _emit_terminal_line(session_id, worker_id, f"[worker] ✗ Clone failed: {repo_full}")
            continue
        repo_name = repo_full.split("/")[-1]
        wt_path   = os.path.join(work_dir, repo_name)
        ok        = await _create_worktree(repo_path, wt_path, branch)
        if ok:
            worktree_entries.append((repo_full, repo_path, wt_path))
            if primary_wt is None:
                primary_wt = wt_path
        else:
            await _emit_terminal_line(session_id, worker_id, f"[worker] ✗ Worktree failed: {repo_full}")

    if not primary_wt:
        await _emit_terminal_line(session_id, worker_id, "[worker] ✗ No worktrees — aborting.")
        await _set_agent_state(session_id, worker_id, "error")
        db = await get_db()
        await db.execute(
            "UPDATE tasks SET state='failed', finished_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), task_id),
        )
        await db.commit()
        await db.close()
        return

    db = await get_db()
    await db.execute(
        "UPDATE tasks SET worktree_path=?, branch=? WHERE id=?",
        (primary_wt, branch, task_id),
    )
    await db.commit()
    await db.close()

    success = await _run_claude_auto(session_id, worker_id, task_id, desc, primary_wt)
    finished_at = datetime.now(timezone.utc).isoformat()

    if success:
        pr_url = await _push_and_pr(session_id, worker_id, task, branch, primary_wt, token)
        db = await get_db()
        await db.execute(
            "UPDATE tasks SET state='done', pr_url=?, finished_at=? WHERE id=?",
            (pr_url, finished_at, task_id),
        )
        await db.commit()
        await db.close()
        await _set_agent_state(session_id, worker_id, "idle")
    else:
        db = await get_db()
        await db.execute(
            "UPDATE tasks SET state='failed', finished_at=? WHERE id=?",
            (finished_at, task_id),
        )
        await db.commit()
        await db.close()
        await _set_agent_state(session_id, worker_id, "error")
        await broadcast(session_id, {
            "type": "task-failed",
            "workerId": worker_id,
            "taskId": task_id,
            "description": desc,
        })

    # Remove worktrees
    for repo_full, repo_path, wt_path in worktree_entries:
        await _run_git(["worktree", "remove", "--force", wt_path], cwd=repo_path)


async def _worker_loop(session_id: str, worker_id: str):
    """Persistent worker loop: process task queue; periodically pull repos."""
    queue = worker_task_queues[worker_id]
    PULL_INTERVAL = 300  # seconds
    last_pull: float = 0.0

    await _emit_terminal_line(session_id, worker_id, "[worker] Online. Watching for tasks.")

    while True:
        try:
            task = await asyncio.wait_for(queue.get(), timeout=60.0)
            await _execute_worker_task(session_id, worker_id, task)
        except asyncio.TimeoutError:
            loop_time = asyncio.get_event_loop().time()
            if loop_time - last_pull >= PULL_INTERVAL:
                config = worker_configs.get(worker_id, {})
                repos  = config.get("repos", [])
                token  = config.get("token")
                if repos:
                    await _pull_repos(session_id, worker_id, repos, token)
                last_pull = loop_time
        except asyncio.CancelledError:
            await _emit_terminal_line(session_id, worker_id, "[worker] Shutting down.")
            break


@app.post("/sessions")
async def create_session(data: Optional[SessionCreate] = None):
    if data is None:
        data = SessionCreate()
    created_at = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        # Retry on collision (UNIQUE constraint); negligible probability with 36^6 space
        for _ in range(5):
            session_id = generate_session_id()
            try:
                await db.execute(
                    "INSERT INTO sessions (id, created_at, name) VALUES (?, ?, ?)",
                    (session_id, created_at, data.name or f"Session {session_id}")
                )
                await db.commit()
                break
            except aiosqlite.IntegrityError:
                continue
        else:
            raise HTTPException(status_code=500, detail="Could not generate unique session ID")
    finally:
        await db.close()
    return {"id": session_id, "created_at": created_at, "name": data.name or f"Session {session_id}"}


@app.get("/sessions")
async def list_sessions():
    db = await get_db()
    try:
        async with db.execute("""
            SELECT s.id, s.created_at, s.name,
                   COUNT(a.id) as agent_count
            FROM sessions s
            LEFT JOIN agents a ON a.session_id = s.id
            GROUP BY s.id
            ORDER BY s.created_at DESC
        """) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)) as cursor:
            session = await cursor.fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        async with db.execute("SELECT * FROM agents WHERE session_id = ?", (session_id,)) as cursor:
            agents = await cursor.fetchall()
        async with db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at DESC LIMIT 100",
            (session_id,)
        ) as cursor:
            messages = await cursor.fetchall()
        return {
            **dict(session),
            "agents": [dict(a) for a in agents],
            "messages": [dict(m) for m in reversed(messages)]
        }
    finally:
        await db.close()


async def broadcast(session_id: str, message: dict, exclude: WebSocket = None):
    """Broadcast a message to all connections in a session."""
    if session_id not in connections:
        return
    dead = []
    for ws in connections[session_id]:
        if ws is exclude:
            continue
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connections[session_id].remove(ws)


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    if session_id not in connections:
        connections[session_id] = []
    connections[session_id].append(websocket)
    db = await get_db()
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "join":
                agent_id = data.get("agentId")
                agent_name = data.get("agentName", "Unknown")
                agent_type = data.get("agentType", "worker")
                joined_at = datetime.now(timezone.utc).isoformat()
                await db.execute(
                    """INSERT OR REPLACE INTO agents (id, session_id, name, type, state, joined_at)
                       VALUES (?, ?, ?, ?, 'idle', ?)""",
                    (agent_id, session_id, agent_name, agent_type, joined_at)
                )
                await db.commit()
                broadcast_msg = {
                    "type": "agent-joined",
                    "agentId": agent_id,
                    "agentName": agent_name,
                    "agentType": agent_type,
                    "state": "idle",
                    "joinedAt": joined_at
                }
                await broadcast(session_id, broadcast_msg)

            elif msg_type == "agent-state":
                agent_id = data.get("agentId")
                state = data.get("state", "idle")
                await db.execute(
                    "UPDATE agents SET state = ? WHERE id = ? AND session_id = ?",
                    (state, agent_id, session_id)
                )
                await db.commit()
                await broadcast(session_id, {
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
                    "INSERT INTO messages (session_id, from_agent, to_agent, content, message_type, created_at) VALUES (?, ?, ?, ?, 'chat', ?)",
                    (session_id, from_agent, to_agent, content, created_at)
                )
                await db.commit()
                await broadcast(session_id, {
                    "type": "chat",
                    "from": from_agent,
                    "to": to_agent,
                    "content": content,
                    "createdAt": created_at
                })

            elif msg_type == "terminal-output":
                agent_id = data.get("agentId")
                line = data.get("line", "")
                created_at = datetime.now(timezone.utc).isoformat()
                await db.execute(
                    "INSERT INTO messages (session_id, from_agent, content, message_type, created_at) VALUES (?, ?, ?, 'terminal', ?)",
                    (session_id, agent_id, line, created_at)
                )
                await db.commit()
                await broadcast(session_id, {
                    "type": "terminal-output",
                    "agentId": agent_id,
                    "line": line,
                    "timestamp": created_at
                })

            elif msg_type in ("offer", "answer", "ice-candidate"):
                # WebRTC signaling - forward to all
                await broadcast(session_id, data, exclude=websocket)

            else:
                # Generic broadcast
                await broadcast(session_id, data)

    except WebSocketDisconnect:
        if session_id in connections and websocket in connections[session_id]:
            connections[session_id].remove(websocket)
    except Exception:
        if session_id in connections and websocket in connections[session_id]:
            connections[session_id].remove(websocket)
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Agent process management
# ---------------------------------------------------------------------------

async def _emit_terminal_line(session_id: str, agent_id: str, line: str):
    """Broadcast a terminal output line and persist it to the DB."""
    now = datetime.now(timezone.utc).isoformat()
    await broadcast(session_id, {
        "type": "terminal-output",
        "agentId": agent_id,
        "line": line,
        "timestamp": now,
    })
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO messages (session_id, from_agent, content, message_type, created_at)"
            " VALUES (?, ?, ?, 'terminal', ?)",
            (session_id, agent_id, line, now),
        )
        await db.commit()
    finally:
        await db.close()


async def _set_agent_state(session_id: str, agent_id: str, state: str):
    """Broadcast and persist an agent state change."""
    await broadcast(session_id, {"type": "agent-state", "agentId": agent_id, "state": state})
    db = await get_db()
    try:
        await db.execute(
            "UPDATE agents SET state = ? WHERE id = ? AND session_id = ?",
            (state, agent_id, session_id),
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
        cmd = ["claude", "-p", req.prompt, "--output-format", "stream-json", "--max-turns", "20"]
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


async def _stream_agent(session_id: str, agent_id: str, req: RunAgentRequest):
    """Spawn the agent subprocess and stream its output as terminal-output events."""
    tool = req.tool.lower()

    try:
        cmd, needs_stdin = _build_command(req)
    except ValueError as exc:
        await _emit_terminal_line(session_id, agent_id, f"✗ {exc}")
        return

    stdin_pipe = asyncio.subprocess.PIPE if needs_stdin else asyncio.subprocess.DEVNULL
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=stdin_pipe,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    if needs_stdin:
        # Pi RPC: send the initial prompt as a JSON command, then leave stdin open
        rpc_msg = json.dumps({"type": "prompt", "content": req.prompt}) + "\n"
        proc.stdin.write(rpc_msg.encode())
        await proc.stdin.drain()

    running_processes[agent_id] = proc
    await _set_agent_state(session_id, agent_id, "working")

    # For Pi message_update we track accumulated text to emit only deltas
    pi_last_text = ""

    try:
        async for raw_line in proc.stdout:
            line_str = raw_line.decode(errors="replace").strip()
            if not line_str:
                continue

            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                await _emit_terminal_line(session_id, agent_id, line_str)
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
                await _emit_terminal_line(session_id, agent_id, text)

    finally:
        if needs_stdin and proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        exit_code = await proc.wait()
        running_processes.pop(agent_id, None)
        await _set_agent_state(session_id, agent_id, "idle" if exit_code == 0 else "error")


def _parse_event(tool: str, event: dict, pi_last_text: str) -> Optional[str]:
    """Extract a human-readable line from one stream-JSON / RPC event."""

    if tool == "claude":
        t = event.get("type")
        if t == "assistant":
            parts = []
            for blk in event.get("message", {}).get("content", []):
                if blk.get("type") == "text":
                    txt = blk.get("text", "").strip()
                    if txt:
                        parts.append(txt)
                elif blk.get("type") == "tool_use":
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


@app.post("/sessions/{session_id}/agents/{agent_id}/run")
async def start_agent_run(session_id: str, agent_id: str, req: RunAgentRequest):
    """Spawn an AI coding agent subprocess and stream its output over WebSocket."""
    # Kill any existing run for this agent
    old = running_processes.get(agent_id)
    if old:
        try:
            old.kill()
        except ProcessLookupError:
            pass

    asyncio.create_task(_stream_agent(session_id, agent_id, req))
    return {"status": "started", "agentId": agent_id, "tool": req.tool}


@app.delete("/sessions/{session_id}/agents/{agent_id}/run")
async def stop_agent_run(session_id: str, agent_id: str):
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

@app.post("/sessions/{session_id}/workers")
async def create_worker(session_id: str, data: WorkerCreate):
    """Register a worker agent and start its persistent loop."""
    worker_id   = "w-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    created_at  = datetime.now(timezone.utc).isoformat()
    worker_name = f"Worker-{worker_id[2:6]}"

    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO workers (id, session_id, repos, state, created_at) VALUES (?, ?, ?, 'idle', ?)",
            (worker_id, session_id, json.dumps(data.repos), created_at),
        )
        await db.execute(
            "INSERT OR REPLACE INTO agents (id, session_id, name, type, state, joined_at)"
            " VALUES (?, ?, ?, 'worker', 'idle', ?)",
            (worker_id, session_id, worker_name, created_at),
        )
        await db.commit()
    finally:
        await db.close()

    worker_configs[worker_id] = {
        "session_id": session_id,
        "repos": data.repos,
        "token": data.github_token,
    }
    worker_task_queues[worker_id] = asyncio.Queue()
    worker_loops[worker_id] = asyncio.create_task(_worker_loop(session_id, worker_id))

    await broadcast(session_id, {
        "type": "agent-joined",
        "agentId": worker_id,
        "agentName": worker_name,
        "agentType": "worker",
        "state": "idle",
        "joinedAt": created_at,
    })
    return {"id": worker_id, "name": worker_name, "repos": data.repos, "created_at": created_at}


@app.get("/sessions/{session_id}/workers")
async def list_workers(session_id: str):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT * FROM workers WHERE session_id = ? ORDER BY created_at DESC", (session_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@app.post("/sessions/{session_id}/workers/{worker_id}/tasks")
async def assign_task(session_id: str, worker_id: str, data: TaskCreate):
    """Queue a task for the specified worker."""
    task_id    = "t-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    created_at = datetime.now(timezone.utc).isoformat()

    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO tasks (id, worker_id, session_id, description, issue_number, issue_repo, state, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (task_id, worker_id, session_id, data.description, data.issue_number, data.issue_repo, created_at),
        )
        await db.commit()
    finally:
        await db.close()

    task = {
        "id": task_id,
        "worker_id": worker_id,
        "session_id": session_id,
        "description": data.description,
        "issue_number": data.issue_number,
        "issue_repo": data.issue_repo,
    }

    if worker_id in worker_task_queues:
        await worker_task_queues[worker_id].put(task)
        await broadcast(session_id, {
            "type": "task-assigned",
            "workerId": worker_id,
            "taskId": task_id,
            "description": data.description,
        })

    return {"id": task_id, "worker_id": worker_id, "state": "pending"}


@app.get("/sessions/{session_id}/workers/{worker_id}/tasks")
async def list_tasks(session_id: str, worker_id: str):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT * FROM tasks WHERE worker_id = ? AND session_id = ? ORDER BY created_at DESC",
            (worker_id, session_id),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()
