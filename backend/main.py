import asyncio
import json
import os
import random
import string
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiosqlite
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from foreman import ForemanService

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

# In-memory WebSocket connections: session_id -> list of WebSocket
connections: Dict[str, List[WebSocket]] = {}

# Running agent subprocesses: agent_id -> asyncio.subprocess.Process
running_processes: Dict[str, asyncio.subprocess.Process] = {}

# Active foreman services: session_id -> ForemanService
foremans: Dict[str, ForemanService] = {}


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
            name TEXT,
            github_token TEXT,
            github_repo TEXT,
            github_project_id TEXT
        )
    """)
    # Migrate existing sessions table if columns are missing
    for col, typedef in [
        ("github_token", "TEXT"),
        ("github_repo", "TEXT"),
        ("github_project_id", "TEXT"),
    ]:
        try:
            await db.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typedef}")
        except Exception:
            pass  # column already exists

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
    await db.commit()
    await db.close()


def generate_session_id():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SessionCreate(BaseModel):
    name: Optional[str] = None


class SessionConfig(BaseModel):
    github_token: Optional[str] = None
    github_repo: Optional[str] = None
    github_project_id: Optional[str] = None


class RunAgentRequest(BaseModel):
    tool: str          # "claude" | "codex" | "pi"
    prompt: str
    model: Optional[str] = None
    provider: Optional[str] = None   # pi only


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------

@app.post("/sessions")
async def create_session(data: Optional[SessionCreate] = None):
    if data is None:
        data = SessionCreate()
    created_at = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
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
        async with db.execute("SELECT * FROM sessions ORDER BY created_at DESC") as cursor:
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


@app.patch("/sessions/{session_id}/config")
async def update_session_config(session_id: str, config: SessionConfig):
    """Update GitHub integration settings for a session."""
    db = await get_db()
    try:
        async with db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Session not found")

        fields = {k: v for k, v in config.model_dump().items() if v is not None}
        if not fields:
            return {"ok": True}

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        await db.execute(
            f"UPDATE sessions SET {set_clause} WHERE id = ?",
            (*fields.values(), session_id),
        )
        await db.commit()

        # Invalidate cached GitHub field IDs if the project changed
        if "github_project_id" in fields and session_id in foremans:
            foremans[session_id]._gh_field_cache.clear()

        return {"ok": True}
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# WebSocket broadcast helper
# ---------------------------------------------------------------------------

async def broadcast(session_id: str, message: dict, exclude: WebSocket = None):
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


# ---------------------------------------------------------------------------
# Foreman factory
# ---------------------------------------------------------------------------

def _get_or_create_foreman(session_id: str) -> ForemanService:
    if session_id not in foremans:
        async def _spawn(sid, agent_id, tool, prompt, model):
            req = RunAgentRequest(tool=tool, prompt=prompt, model=model)
            asyncio.create_task(_stream_agent(sid, agent_id, req))

        foremans[session_id] = ForemanService(
            session_id=session_id,
            broadcast_fn=broadcast,
            get_db_fn=get_db,
            spawn_agent_fn=_spawn,
        )
    return foremans[session_id]


async def _ensure_foreman_registered(session_id: str):
    """Insert the foreman agent row if it doesn't already exist."""
    db = await get_db()
    try:
        async with db.execute(
            "SELECT id FROM agents WHERE id = 'foreman' AND session_id = ?", (session_id,)
        ) as cur:
            if await cur.fetchone():
                return
        joined_at = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO agents (id, session_id, name, type, state, joined_at)"
            " VALUES ('foreman', ?, 'Foreman', 'foreman', 'idle', ?)",
            (session_id, joined_at),
        )
        await db.commit()
        await broadcast(session_id, {
            "type": "agent-joined",
            "agentId": "foreman",
            "agentName": "Foreman",
            "agentType": "foreman",
            "state": "idle",
            "joinedAt": joined_at,
        })
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

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
                await broadcast(session_id, {
                    "type": "agent-joined",
                    "agentId": agent_id,
                    "agentName": agent_name,
                    "agentType": agent_type,
                    "state": "idle",
                    "joinedAt": joined_at
                })

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
                    "INSERT INTO messages (session_id, from_agent, to_agent, content, message_type, created_at)"
                    " VALUES (?, ?, ?, ?, 'chat', ?)",
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

                # Route chat messages addressed to the foreman
                if to_agent == "foreman" and from_agent != "foreman":
                    await _ensure_foreman_registered(session_id)
                    foreman = _get_or_create_foreman(session_id)
                    foreman.notify({"type": "chat", "content": content, "from": from_agent})

            elif msg_type == "terminal-output":
                agent_id = data.get("agentId")
                line = data.get("line", "")
                created_at = datetime.now(timezone.utc).isoformat()
                await db.execute(
                    "INSERT INTO messages (session_id, from_agent, content, message_type, created_at)"
                    " VALUES (?, ?, ?, 'terminal', ?)",
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
                await broadcast(session_id, data, exclude=websocket)

            else:
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
    """Return (cmd_list, needs_stdin_prompt)."""
    tool = req.tool.lower()

    if tool == "claude":
        cmd = ["claude", "-p", req.prompt, "--output-format", "stream-json", "--max-turns", "20"]
        if req.model:
            cmd += ["--model", req.model]
        return cmd, False

    if tool == "codex":
        cmd = ["codex", "exec", "--json", req.prompt]
        if req.model:
            cmd += ["--model", req.model]
        return cmd, False

    if tool == "pi":
        cmd = ["pi", "--mode", "rpc", "--no-session"]
        if req.provider:
            cmd += ["--provider", req.provider]
        if req.model:
            cmd += ["--model", req.model]
        return cmd, True

    raise ValueError(f"Unknown tool: {req.tool!r}")


async def _stream_agent(session_id: str, agent_id: str, req: RunAgentRequest):
    tool = req.tool.lower()

    try:
        cmd, needs_stdin = _build_command(req)
    except ValueError as exc:
        await _emit_terminal_line(session_id, agent_id, f"✗ {exc}")
        return

    # Look up agent name for the done notification
    db = await get_db()
    try:
        async with db.execute("SELECT name FROM agents WHERE id = ?", (agent_id,)) as cur:
            row = await cur.fetchone()
        agent_name = row["name"] if row else agent_id
    finally:
        await db.close()

    stdin_pipe = asyncio.subprocess.PIPE if needs_stdin else asyncio.subprocess.DEVNULL
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=stdin_pipe,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    if needs_stdin:
        rpc_msg = json.dumps({"type": "prompt", "content": req.prompt}) + "\n"
        proc.stdin.write(rpc_msg.encode())
        await proc.stdin.drain()

    running_processes[agent_id] = proc
    await _set_agent_state(session_id, agent_id, "working")

    pi_last_text = ""
    output_lines: list[str] = []  # collect for agent-done summary

    try:
        async for raw_line in proc.stdout:
            line_str = raw_line.decode(errors="replace").strip()
            if not line_str:
                continue

            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                await _emit_terminal_line(session_id, agent_id, line_str)
                output_lines.append(line_str)
                continue

            text = _parse_event(tool, event, pi_last_text)

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
                output_lines.append(text)

    finally:
        if needs_stdin and proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        exit_code = await proc.wait()
        running_processes.pop(agent_id, None)
        final_state = "idle" if exit_code == 0 else "error"
        await _set_agent_state(session_id, agent_id, final_state)

        # Build a summary for the foreman (last 30 non-empty lines)
        summary_lines = [l for l in output_lines if l.strip()][-30:]
        summary = "\n".join(summary_lines)

        # Broadcast agent-done so the UI can show a notification
        await broadcast(session_id, {
            "type": "agent-done",
            "agentId": agent_id,
            "agentName": agent_name,
            "tool": tool,
            "exitCode": exit_code,
            "summary": summary,
        })

        # Notify the foreman so it can review and follow up
        if session_id in foremans or exit_code == 0:
            foreman = _get_or_create_foreman(session_id)
            foreman.notify({
                "type": "agent_done",
                "agent_id": agent_id,
                "agent_name": agent_name,
                "tool": tool,
                "exit_code": exit_code,
                "summary": summary,
            })


def _parse_event(tool: str, event: dict, pi_last_text: str) -> Optional[str]:
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
            return f"[claude] tools: {', '.join(event.get('tools', [])[:6])}"

    elif tool == "codex":
        t = event.get("type")
        if t == "message" and event.get("role") == "assistant":
            return (event.get("content") or "").strip() or None
        if t == "function_call":
            return f"▶ {event.get('name', '')}({str(event.get('arguments', ''))[:80]})"
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
            preview = lines[0][:120] + (f" (+{len(lines) - 1} lines)" if len(lines) > 1 else "")
            return f"  → {preview}"
        if t == "agent_end":
            err = event.get("error")
            return f"✗ {err}" if err else None
        if t == "agent_start":
            return "[pi] agent started"

    return None


# ---------------------------------------------------------------------------
# Run / stop endpoints
# ---------------------------------------------------------------------------

@app.post("/sessions/{session_id}/agents/{agent_id}/run")
async def start_agent_run(session_id: str, agent_id: str, req: RunAgentRequest):
    old = running_processes.get(agent_id)
    if old:
        try:
            old.kill()
        except ProcessLookupError:
            pass
    # Register the foreman so it receives the agent-done notification
    await _ensure_foreman_registered(session_id)
    asyncio.create_task(_stream_agent(session_id, agent_id, req))
    return {"status": "started", "agentId": agent_id, "tool": req.tool}


@app.delete("/sessions/{session_id}/agents/{agent_id}/run")
async def stop_agent_run(session_id: str, agent_id: str):
    proc = running_processes.get(agent_id)
    if not proc:
        raise HTTPException(status_code=404, detail="No running process for this agent")
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    return {"status": "stopped", "agentId": agent_id}
