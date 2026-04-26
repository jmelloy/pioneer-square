import asyncio
import json
import os
import random
import string
from datetime import datetime
from typing import Dict, List, Optional

import aiosqlite
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Pioneer Square")

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
    await db.commit()
    await db.close()


@app.on_event("startup")
async def startup():
    await init_db()


def generate_session_id():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))


class SessionCreate(BaseModel):
    name: Optional[str] = None


@app.post("/sessions")
async def create_session(data: SessionCreate = SessionCreate()):
    session_id = generate_session_id()
    created_at = datetime.utcnow().isoformat()
    db = await get_db()
    try:
        # Ensure unique
        while True:
            async with db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)) as cursor:
                if not await cursor.fetchone():
                    break
            session_id = generate_session_id()
        await db.execute(
            "INSERT INTO sessions (id, created_at, name) VALUES (?, ?, ?)",
            (session_id, created_at, data.name or f"Session {session_id}")
        )
        await db.commit()
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
                joined_at = datetime.utcnow().isoformat()
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
                to_agent = data.get("to", "overseer")
                content = data.get("content", "")
                created_at = datetime.utcnow().isoformat()
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
                created_at = datetime.utcnow().isoformat()
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
    except Exception as e:
        if session_id in connections and websocket in connections[session_id]:
            connections[session_id].remove(websocket)
    finally:
        await db.close()
