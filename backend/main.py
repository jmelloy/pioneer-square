import asyncio
import json
import logging
import os
import random
import secrets
import string
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from database import AsyncSessionLocal, get_db
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from models import (
    Agent,
    ClaudeCredentials,
    GithubToken,
    Guild,
    Message,
    Task,
    TaskLog,
    UserSession,
    Worker,
)
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

# Load .env (looked up from CWD upward, then alongside this file) before any
# code reads os.environ, so ANTHROPIC_API_KEY etc. are available.
try:
    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

from events import agent_owners, broadcast, connections, emit_terminal_line, pending_claude_auth
from foreman import (
    clear_foreman_history,
    get_foreman_history,
    maybe_post_plan_comment,
    run_foreman_ai,
)

logger = logging.getLogger(__name__)


def _row(obj) -> dict:
    """Convert a SQLAlchemy ORM model instance to a plain dict."""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Uvicorn sets the root logger to WARNING, so foreman.* logs would be silently
    # dropped without an explicit handler.  Wire up a StreamHandler on the foreman
    # package logger so debug output reaches the console regardless of root level.
    foreman_log = logging.getLogger("foreman")
    foreman_log.setLevel(logging.DEBUG)
    if not foreman_log.handlers:
        _h = logging.StreamHandler()
        _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        foreman_log.addHandler(_h)
    foreman_log.propagate = False
    foreman_log.info("foreman logger active (level=DEBUG)")
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
GITHUB_REDIRECT_URI = os.environ.get("GITHUB_REDIRECT_URI", "http://localhost:5173/")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")

# In-memory: short-lived state tokens for OAuth CSRF protection
oauth_states: set[str] = set()

_http_bearer = HTTPBearer(auto_error=False)

# Running agent subprocesses: agent_id -> asyncio.subprocess.Process
# Used only by /agents/{id}/run (one-off claude/codex/pi invocations).
# Worker subprocesses live in the standalone /worker process.
running_processes: dict[str, asyncio.subprocess.Process] = {}


async def init_db():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    def run_migrations():
        cfg = Config(Path(__file__).resolve().parent / "alembic.ini")
        db_url = os.environ.get(
            "DATABASE_URL", f"sqlite+aiosqlite:///{os.environ.get('DB_PATH', 'pioneer_square.db')}"
        )
        # Need a sync engine for inspection; strip aiosqlite async driver prefix.
        sync_url = db_url.replace("+aiosqlite", "")
        engine = create_engine(sync_url)
        try:
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
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _worker_name(worker_id: str, hostname: str | None = None) -> str:
    raw = worker_id[2:].upper()
    split = 2 + sum(ord(c) for c in raw) % 3
    droid = f"{raw[:split]}-{raw[split:]}"
    if hostname:
        return f"{hostname[:3].upper()}/{droid}"
    return droid


class GuildCreate(BaseModel):
    name: str | None = None


class GuildUpdate(BaseModel):
    name: str | None = None
    primary_repo: str | None = None


class RunAgentRequest(BaseModel):
    tool: str  # "claude" | "codex" | "pi"
    prompt: str
    model: str | None = None
    provider: str | None = None  # pi only


class WorkerCreate(BaseModel):
    repos: list[str]  # ["owner/repo", ...]
    github_token: str | None = None
    hostname: str | None = None


class SpawnWorkerRequest(BaseModel):
    repos: list[str]
    name: str | None = None


class ClaudeCredentialsRequest(BaseModel):
    guild_id: str
    credentials_blob: str  # base64-encoded tar.gz of ~/.claude/


class TaskCreate(BaseModel):
    description: str
    name: str | None = None
    tool: str = "claude"  # "claude" | "codex" | "pi"
    issue_number: int | None = None
    issue_repo: str | None = None
    parent_task_id: str | None = None
    phase: str | None = "execute"


# ---------------------------------------------------------------------------
# Worker tasks live in the standalone /worker package; this backend only
# persists worker/task state and dispatches assignments over WebSocket.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GitHub OAuth helpers (OAuth flow only — foreman GitHub tools are in foreman/tools.py)
# ---------------------------------------------------------------------------


def _gh_exchange_code(code: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": GITHUB_REDIRECT_URI,
        }
    ).encode()
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
        raise HTTPException(
            status_code=500, detail="GitHub OAuth not configured (missing GITHUB_CLIENT_ID)"
        )
    state = secrets.token_urlsafe(16)
    oauth_states.add(state)
    params = urllib.parse.urlencode(
        {
            "client_id": GITHUB_CLIENT_ID,
            "redirect_uri": GITHUB_REDIRECT_URI,
            "scope": "repo read:org project",
            "state": state,
        }
    )
    return {"url": f"https://github.com/login/oauth/authorize?{params}"}


async def _gh_create_session(code: str, state: str) -> dict:
    """Exchange an OAuth code+state for a login session. Returns the session payload dict."""
    if state not in oauth_states:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    oauth_states.discard(state)

    try:
        token_data = await asyncio.to_thread(_gh_exchange_code, code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub token exchange failed: {exc}")

    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=502, detail=f"No access_token in GitHub response: {token_data}"
        )

    try:
        user_data = await asyncio.to_thread(_gh_get_user, access_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub user fetch failed: {exc}")

    github_user_id = str(user_data["id"])
    github_username = user_data.get("login", "")
    login_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC).isoformat()

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

    return {
        "login_token": login_token,
        "gh_token": access_token,
        "gh_user_id": github_user_id,
        "gh_login": github_username,
        "gh_name": user_data.get("name") or "",
        "gh_avatar": user_data.get("avatar_url") or "",
    }


class CodeExchangeRequest(BaseModel):
    code: str
    state: str


@app.post("/auth/github/exchange")
async def github_exchange(body: CodeExchangeRequest):
    """Exchange a GitHub OAuth code+state for a login session. Called by the frontend."""
    return await _gh_create_session(body.code, body.state)


@app.get("/auth/github/callback")
async def github_callback(code: str = Query(...), state: str = Query(...)):
    """Legacy backend OAuth callback — redirects to the frontend with session params in the query string."""
    payload = await _gh_create_session(code, state)
    qs = urllib.parse.urlencode(payload)
    return RedirectResponse(url=f"{FRONTEND_URL}/?{qs}")


@app.get("/auth/github/token")
async def get_github_token(guild_id: str = Query(...)):
    """Return the stored OAuth token for the guild's linked GitHub user. Used by workers."""
    db = await get_db()
    try:
        result = await db.execute(select(Guild.github_user_id).where(Guild.id == guild_id))
        github_user_id_val = result.scalar_one_or_none()
        if not github_user_id_val:
            raise HTTPException(status_code=404, detail="No GitHub account linked to this guild")
        result = await db.execute(
            select(GithubToken.access_token, GithubToken.github_username).where(
                GithubToken.github_user_id == github_user_id_val
            )
        )
        token_row = result.first()
        if not token_row:
            raise HTTPException(status_code=404, detail="GitHub token not found")
        return {"access_token": token_row.access_token, "username": token_row.github_username}
    finally:
        await db.close()


@app.get("/auth/claude/credentials")
async def get_claude_credentials(guild_id: str = Query(...)):
    """Return stored Claude credentials blob for a worker. Called by workers on startup."""
    db = await get_db()
    try:
        result = await db.execute(
            select(ClaudeCredentials).where(ClaudeCredentials.guild_id == guild_id)
        )
        row = result.scalar_one_or_none()
        if not row:
            raise HTTPException(
                status_code=404, detail="No Claude credentials stored for this guild"
            )
        return {"credentials_blob": row.credentials_blob}
    finally:
        await db.close()


@app.post("/auth/claude/credentials")
async def store_claude_credentials(data: ClaudeCredentialsRequest):
    """Store Claude credentials blob (called by worker after successful login)."""
    now = datetime.now(UTC).isoformat()
    db = await get_db()
    try:
        result = await db.execute(
            select(ClaudeCredentials).where(ClaudeCredentials.guild_id == data.guild_id)
        )
        row = result.scalar_one_or_none()
        if row:
            row.credentials_blob = data.credentials_blob
            row.updated_at = now
        else:
            db.add(
                ClaudeCredentials(
                    guild_id=data.guild_id,
                    credentials_blob=data.credentials_blob,
                    updated_at=now,
                )
            )
        await db.commit()
    finally:
        await db.close()
    return {"ok": True}


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
    data: GuildCreate | None = None,
    github_user_id: str = Depends(require_user),
):
    if data is None:
        data = GuildCreate()
    created_at = datetime.now(UTC).isoformat()
    db = await get_db()
    try:
        for _ in range(5):
            guild_id = generate_guild_id()
            try:
                db.add(
                    Guild(
                        id=guild_id,
                        created_at=created_at,
                        name=data.name or f"Guild {guild_id}",
                        github_user_id=github_user_id,
                    )
                )
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
        if data.name is not None:
            guild.name = data.name
        if "primary_repo" in data.model_fields_set:
            guild.primary_repo = data.primary_repo
        await db.commit()
    finally:
        await db.close()
    await broadcast(
        guild_id,
        {
            "type": "guild-updated",
            "id": guild_id,
            "name": guild.name,
            "primary_repo": guild.primary_repo,
        },
    )
    return {"id": guild_id, "name": guild.name, "primary_repo": guild.primary_repo}


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


@app.websocket("/ws/{guild_id}")
async def websocket_endpoint(websocket: WebSocket, guild_id: str):
    await websocket.accept()
    if guild_id not in connections:
        connections[guild_id] = []
    connections[guild_id].append(websocket)
    # Agents that joined via this websocket; marked offline when it disconnects.
    joined_agents: set[str] = set()

    # Identify the browser user from the optional ?token= query param.
    # Workers don't pass a token; ws_user_id stays None for them.
    ws_user_id: str | None = None
    _token = websocket.query_params.get("token")
    if _token:
        _auth_db = await get_db()
        try:
            _res = await _auth_db.execute(
                select(UserSession.github_user_id).where(UserSession.token == _token)
            )
            ws_user_id = _res.scalar_one_or_none()
        except Exception:
            pass
        finally:
            await _auth_db.close()

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
                joined_at = datetime.now(UTC).isoformat()
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
                    agent_owners[agent_id] = websocket
                broadcast_msg = {
                    "type": "agent-joined",
                    "agentId": agent_id,
                    "agentName": agent_name,
                    "agentType": agent_type,
                    "workerId": worker_id,
                    "state": "idle",
                    "joinedAt": joined_at,
                }
                await broadcast(guild_id, broadcast_msg)

            elif msg_type == "agent-state":
                agent_id = data.get("agentId")
                state = data.get("state", "idle")
                activity = data.get("activity")  # fine-grained activity (reading/editing/etc.)
                update_vals: dict = {"state": state}
                if activity is not None:
                    update_vals["activity"] = activity
                elif state in ("idle", "offline"):
                    update_vals["activity"] = None
                await db.execute(
                    update(Agent)
                    .where(Agent.id == agent_id, Agent.guild_id == guild_id)
                    .values(**update_vals)
                )
                await db.commit()
                broadcast_msg: dict = {"type": "agent-state", "agentId": agent_id, "state": state}
                if "activity" in update_vals:
                    broadcast_msg["activity"] = update_vals["activity"]
                await broadcast(guild_id, broadcast_msg)

            elif msg_type == "chat":
                from_agent = data.get("from", "user")
                to_agent = data.get("to", "foreman")
                content = data.get("content", "")
                created_at = datetime.now(UTC).isoformat()
                db.add(
                    Message(
                        guild_id=guild_id,
                        from_agent=from_agent,
                        to_agent=to_agent,
                        content=content,
                        message_type="chat",
                        created_at=created_at,
                        user_id=ws_user_id if from_agent == "user" else None,
                    )
                )
                await db.commit()
                await broadcast(
                    guild_id,
                    {
                        "type": "chat",
                        "from": from_agent,
                        "to": to_agent,
                        "content": content,
                        "createdAt": created_at,
                        **({"userId": ws_user_id} if ws_user_id and from_agent == "user" else {}),
                    },
                )
                # Route human messages addressed to foreman through the AI.
                # Each authenticated user gets their own foreman conversation thread.
                # Exception: if a worker is waiting for a Claude auth code, treat
                # the next user message as that code and bypass the foreman AI.
                if from_agent == "user" and to_agent == "foreman" and content:
                    pending_workers = pending_claude_auth.get(guild_id, {})
                    if pending_workers:
                        pending_worker_id = next(iter(pending_workers))
                        pending_workers.pop(pending_worker_id)
                        logger.info(
                            "chat intercepted as auth code for %s in guild %s code_len=%d",
                            pending_worker_id,
                            guild_id,
                            len(content),
                        )
                        await broadcast(
                            guild_id,
                            {
                                "type": "worker-auth-response",
                                "workerId": pending_worker_id,
                                "code": content,
                            },
                        )
                    else:
                        asyncio.create_task(run_foreman_ai(guild_id, content, user_id=ws_user_id))

            elif msg_type == "terminal-output":
                msg_agent_id = data.get("agentId")
                line = data.get("line", "")
                task_id = data.get("taskId")
                detail = data.get("detail")
                created_at = datetime.now(UTC).isoformat()
                # Look up worker_id for this agent to tag logs for cross-filter queries.
                worker_id_for_log = None
                if msg_agent_id:
                    result = await db.execute(
                        select(Agent.worker_id).where(Agent.id == msg_agent_id)
                    )
                    worker_id_for_log = result.scalar_one_or_none()
                if line:
                    db.add(
                        TaskLog(
                            task_id=task_id or None,
                            timestamp=created_at,
                            line=line,
                            worker_id=worker_id_for_log,
                            agent_id=msg_agent_id,
                            data=json.dumps(detail) if detail else None,
                        )
                    )
                    await db.commit()
                await broadcast(
                    guild_id,
                    {
                        "type": "terminal-output",
                        "agentId": msg_agent_id,
                        "workerId": worker_id_for_log,
                        "taskId": task_id,
                        "line": line,
                        "timestamp": created_at,
                        **({"detail": detail} if detail else {}),
                    },
                )

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
                        update(Agent)
                        .where(Agent.id == agent_id, Agent.guild_id == guild_id)
                        .values(state="offline")
                    )
                if worker_id:
                    await db.execute(
                        update(Worker)
                        .where(Worker.id == worker_id, Worker.guild_id == guild_id)
                        .values(state="offline")
                    )
                if joined_agents or worker_id:
                    await db.commit()
                for agent_id in joined_agents:
                    await broadcast(
                        guild_id,
                        {
                            "type": "agent-state",
                            "agentId": agent_id,
                            "state": "offline",
                        },
                    )

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
                finalized_by = data.get("finalizedBy", "")
                last_text = data.get("lastText", "")
                if task_id:
                    await db.execute(
                        update(Task)
                        .where(Task.id == task_id, Task.state == "working")
                        .values(state="awaiting-review")
                    )
                    await db.commit()
                await broadcast(guild_id, data, exclude=websocket)
                if task_id and not finalized_by:
                    asyncio.create_task(maybe_post_plan_comment(guild_id, task_id, last_text))
                if task_id:
                    if finalized_by == "timeout":
                        finished_at = datetime.now(UTC).isoformat()
                        await db.execute(
                            update(Task)
                            .where(Task.id == task_id)
                            .values(state="done", finished_at=finished_at)
                        )
                        await db.commit()
                        await broadcast(
                            guild_id,
                            {
                                "type": "task-update",
                                "taskId": task_id,
                                "state": "done",
                                "finishedAt": finished_at,
                            },
                        )
                        asyncio.create_task(
                            run_foreman_ai(
                                guild_id,
                                f"[timeout] Follow-up window expired for task {task_id} "
                                f"(worker {worker_id_msg}). The task has been auto-finalized "
                                "as done — no foreman action required.",
                            )
                        )
                    else:
                        asyncio.create_task(
                            run_foreman_ai(
                                guild_id,
                                f"[task-complete] Worker {worker_id_msg} finished task {task_id}: "
                                f'"{desc[:80]}" — branch: {branch}. '
                                "Review this result. Call send_followup if additional work is needed "
                                "(e.g. update tests, add docs, fix lint errors). "
                                "Otherwise call finalize_task to mark it complete.",
                            )
                        )

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
                    asyncio.create_task(
                        run_foreman_ai(
                            guild_id,
                            f"[followup-done] Worker {worker_id_msg} completed a follow-up for task {task_id}. "
                            "Decide: call send_followup for more work, or call finalize_task to mark it done.",
                        )
                    )

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
                asyncio.create_task(run_foreman_ai(guild_id, escalation))

            elif msg_type == "claude-auth-required":
                # Worker needs Claude authentication; broadcast URL to UI and loop foreman in.
                worker_id = data.get("workerId", "a worker")
                auth_url = data.get("url", "")
                logger.info(
                    "claude-auth-required from %s in guild %s url=%s",
                    worker_id,
                    guild_id,
                    auth_url[:80],
                )
                await broadcast(guild_id, data, exclude=websocket)
                # Track so new frontend connections can restore the auth panel.
                pending_claude_auth.setdefault(guild_id, {})[worker_id] = auth_url
                logger.info(
                    "pending_claude_auth now has %d entries for guild %s",
                    len(pending_claude_auth.get(guild_id, {})),
                    guild_id,
                )
                asyncio.create_task(
                    run_foreman_ai(
                        guild_id,
                        f"Worker {worker_id} needs Claude authentication. "
                        f"Auth URL: {auth_url}. "
                        "A human must visit this URL, complete authentication, then paste the "
                        "resulting code into the auth panel that has appeared in the chat UI "
                        "(or type it into the Foreman Comms input). The worker is waiting.",
                    )
                )

            elif msg_type == "worker-auth-response":
                # Human is submitting an auth code; clear pending state and
                # broadcast to all connections so the waiting worker receives it.
                worker_id = data.get("workerId", "")
                code_len = len(data.get("code", ""))
                logger.info(
                    "worker-auth-response for %s in guild %s code_len=%d",
                    worker_id,
                    guild_id,
                    code_len,
                )
                pending_claude_auth.get(guild_id, {}).pop(worker_id, None)
                peer_count = len(connections.get(guild_id, []))
                logger.info(
                    "broadcasting worker-auth-response to %d connections in guild %s",
                    peer_count,
                    guild_id,
                )
                await broadcast(guild_id, data)

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
            # Only mark agents offline if this WS is still the current owner.
            # If a reconnect already produced a newer WS that re-joined the
            # agent, that newer WS owns the agent now and we must not clobber it.
            stale_agents = [aid for aid in joined_agents if agent_owners.get(aid) is websocket]
            for agent_id in stale_agents:
                agent_owners.pop(agent_id, None)
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
            if stale_agents:
                await db.commit()
            for agent_id in stale_agents:
                await broadcast(
                    guild_id,
                    {
                        "type": "agent-state",
                        "agentId": agent_id,
                        "state": "offline",
                    },
                )
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Agent process management
# ---------------------------------------------------------------------------


async def _set_agent_state(guild_id: str, agent_id: str, state: str):
    """Broadcast and persist an agent state change (clears activity)."""
    await broadcast(
        guild_id, {"type": "agent-state", "agentId": agent_id, "state": state, "activity": None}
    )
    db = await get_db()
    try:
        await db.execute(
            update(Agent)
            .where(Agent.id == agent_id, Agent.guild_id == guild_id)
            .values(state=state, activity=None)
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
        cmd = [
            "claude",
            "-p",
            req.prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            "20",
        ]
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
        await emit_terminal_line(guild_id, agent_id, f"✗ {exc}")
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
        await emit_terminal_line(guild_id, agent_id, f"✗ command not found: {cmd[0]}")
        await _set_agent_state(guild_id, agent_id, "error")
        return
    except Exception as exc:
        await emit_terminal_line(guild_id, agent_id, f"✗ failed to start process: {exc}")
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
                await emit_terminal_line(guild_id, agent_id, f"[stderr] {line}")

    stderr_task = asyncio.create_task(_drain_stderr())

    try:
        async for raw_line in proc.stdout:
            line_str = raw_line.decode(errors="replace").strip()
            if not line_str:
                continue

            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                await emit_terminal_line(guild_id, agent_id, line_str)
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
                await emit_terminal_line(guild_id, agent_id, text_out)

    finally:
        if needs_stdin and proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        exit_code = await proc.wait()
        await stderr_task
        running_processes.pop(agent_id, None)
        await _set_agent_state(guild_id, agent_id, "idle" if exit_code == 0 else "error")


def _parse_event(tool: str, event: dict, pi_last_text: str) -> str | None:
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
                        content = "\n".join(
                            b.get("text", "") for b in content if b.get("type") == "text"
                        )
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
            delta = full[len(pi_last_text) :]
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
    worker_id = "w-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    created_at = datetime.now(UTC).isoformat()
    worker_name = _worker_name(worker_id, data.hostname)

    db = await get_db()
    try:
        db.add(
            Worker(
                id=worker_id,
                guild_id=guild_id,
                repos=json.dumps(data.repos),
                state="offline",
                created_at=created_at,
            )
        )
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

    await broadcast(
        guild_id,
        {
            "type": "agent-joined",
            "agentId": worker_id,
            "agentName": worker_name,
            "agentType": "worker",
            "state": "offline",
            "joinedAt": created_at,
        },
    )
    return {"id": worker_id, "name": worker_name, "repos": data.repos, "created_at": created_at}


@app.post("/guilds/{guild_id}/spawn-worker")
async def spawn_worker_container(guild_id: str, data: SpawnWorkerRequest):
    """Start a new worker container via Docker. Requires the Docker socket to be mounted."""
    try:
        import docker as docker_sdk
    except ImportError:
        raise HTTPException(status_code=503, detail="Docker SDK not installed in backend")

    try:
        client = docker_sdk.from_env()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Docker socket unavailable: {e}")

    image = os.environ.get("WORKER_IMAGE", "pioneer-square-worker")
    backend_url = os.environ.get("WORKER_BACKEND_URL", "http://backend:8000")

    env = {
        "PIONEER_BACKEND_URL": backend_url,
        "PIONEER_GUILD_ID": guild_id,
        "PIONEER_REPOS": ",".join(data.repos),
        "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
    }
    if data.name:
        env["PIONEER_WORKER_NAME"] = data.name

    # Join the same Docker network as the backend so the worker can reach it,
    # and inherit the compose project so `docker compose ps` lists the worker.
    network = None
    project = os.environ.get("COMPOSE_PROJECT_NAME")
    try:
        me = client.containers.get(os.environ.get("HOSTNAME", ""))
        network = next(iter(me.attrs["NetworkSettings"]["Networks"].keys()), None)
        if not project:
            project = me.labels.get("com.docker.compose.project")
    except Exception:
        pass

    labels: dict[str, str] = {}
    if project:
        # oneoff=True mirrors `docker compose run`, which keeps compose from
        # warning about orphan containers on subsequent up/down commands.
        labels["com.docker.compose.project"] = project
        labels["com.docker.compose.service"] = "worker"
        labels["com.docker.compose.oneoff"] = "True"

    try:
        run_kwargs: dict = dict(image=image, environment=env, detach=True, remove=True)
        if network:
            run_kwargs["network"] = network
        if labels:
            run_kwargs["labels"] = labels
        container = client.containers.run(**run_kwargs)
    except docker_sdk.errors.ImageNotFound:
        raise HTTPException(
            status_code=404,
            detail=f"Worker image '{image}' not found — run: docker compose build worker",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start container: {e}")

    return {"container_id": container.id[:12], "image": image}


@app.get("/guilds/{guild_id}/pending-auth")
async def get_pending_auth(guild_id: str):
    """Return workers currently waiting for a Claude auth code.

    The frontend calls this on mount so the auth panel is restored after a
    page refresh even if the original claude-auth-required broadcast was missed.
    """
    pending = pending_claude_auth.get(guild_id, {})
    return [{"workerId": wid, "url": url} for wid, url in pending.items()]


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
    task_id = "t-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    created_at = datetime.now(UTC).isoformat()

    db = await get_db()
    try:
        result = await db.execute(
            select(Worker.id).where(Worker.id == worker_id, Worker.guild_id == guild_id)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Worker not found")
        name = data.name or data.description[:60]
        db.add(
            Task(
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
            )
        )
        await db.commit()
    finally:
        await db.close()

    await broadcast(
        guild_id,
        {
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
        },
    )

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

    await emit_terminal_line(guild_id, worker_id, f"[foreman → worker] {text_msg}")
    await broadcast(
        guild_id,
        {
            "type": "worker-message",
            "workerId": worker_id,
            "message": text_msg,
        },
    )
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
                Task.id,
                Task.worker_id,
                Task.name,
                Task.description,
                Task.tool,
                Task.state,
                Task.phase,
                Task.parent_task_id,
                Task.branch,
                Task.pr_url,
                Task.issue_number,
                Task.issue_repo,
                Task.created_at,
                Task.finished_at,
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
            select(
                TaskLog.timestamp, TaskLog.line, TaskLog.worker_id, TaskLog.agent_id, TaskLog.data
            )
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
    worker_id: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
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
            TaskLog.timestamp,
            TaskLog.line,
            TaskLog.worker_id,
            TaskLog.agent_id,
            TaskLog.task_id,
            TaskLog.data,
        )
        if task_id:
            stmt = stmt.where(TaskLog.task_id == task_id)
        elif worker_id:
            stmt = stmt.where(TaskLog.worker_id == worker_id)
        else:
            stmt = stmt.where(TaskLog.agent_id == agent_id)
        stmt = stmt.order_by(TaskLog.id.asc())
        result = await db.execute(stmt)
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
    await broadcast(
        guild_id,
        {
            "type": "task-followup",
            "workerId": worker_id,
            "taskId": task_id,
            "instructions": data.instructions,
        },
    )
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
        finished_at = datetime.now(UTC).isoformat()
        await db.execute(
            update(Task).where(Task.id == task_id).values(state="done", finished_at=finished_at)
        )
        await db.commit()
    finally:
        await db.close()
    await broadcast(
        guild_id,
        {
            "type": "task-finalize",
            "workerId": worker_id,
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
        },
    )
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
        finished_at = datetime.now(UTC).isoformat()
        await db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(state="cancelled", finished_at=finished_at)
        )
        await db.commit()
    finally:
        await db.close()
    await broadcast(
        guild_id,
        {
            "type": "task-cancel",
            "workerId": worker_id,
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
        await db.execute(update(Task).where(Task.id == task_id).values(state="working"))
        await db.commit()
    finally:
        await db.close()
    await broadcast(
        guild_id,
        {
            "type": "task-redirect",
            "workerId": worker_id,
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
    return {"status": "redirected", "taskId": task_id}


# ---------------------------------------------------------------------------
# Foreman context endpoints
# ---------------------------------------------------------------------------


@app.get("/guilds/{guild_id}/foreman/context")
async def get_foreman_context(
    guild_id: str,
    github_user_id: str = Depends(require_user),
):
    """Return the stored foreman conversation turns for this guild+user (debug view)."""
    db = await get_db()
    try:
        result = await db.execute(select(Guild.id).where(Guild.id == guild_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Guild not found")
    finally:
        await db.close()
    turns = await get_foreman_history(guild_id, github_user_id)
    return {"messages": turns, "count": len(turns)}


@app.post("/guilds/{guild_id}/foreman/clear-context")
async def clear_foreman_context(
    guild_id: str,
    github_user_id: str = Depends(require_user),
):
    """Delete all stored foreman turns for this guild+user. Chat history in messages table is preserved."""
    db = await get_db()
    try:
        result = await db.execute(select(Guild.id).where(Guild.id == guild_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Guild not found")
    finally:
        await db.close()
    removed = await clear_foreman_history(guild_id, github_user_id)
    logger.info(
        "Foreman context cleared for guild %s user %s (%d turns removed)",
        guild_id,
        github_user_id,
        removed,
    )
    return {"status": "cleared", "removed": removed}
