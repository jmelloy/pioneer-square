# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Pioneer Square is a real-time multi-agent workspace: a pixel-art steampunk factory floor UI where a **Foreman AI** (Claude) coordinates **worker processes** that autonomously clone repos, run Claude on tasks, and open GitHub PRs. Three independent processes must all be running for the full system to work (a fourth — the standalone foreman — is opt-in; see below).

## Commands

### Backend
```bash
cd backend
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt
# Requires GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET (see README) or backend/.env
uvicorn main:app --reload --port 8000
```

### Frontend
```bash
cd frontend
npm install
npm run dev       # dev server at http://localhost:5173
npm run build     # production build
```

### Worker
```bash
cd worker
uv venv && source .venv/bin/activate
uv pip install -e .
cp pioneer-worker.toml.example pioneer-worker.toml
# Edit: backend_url, guild_id, [github] repos and token
pioneer-worker
pioneer-worker --log-level DEBUG   # verbose
```

### Standalone Foreman (Phase 2 — opt-in)
The embedded foreman (inside the backend process) is the default.  A standalone
foreman process can be run alongside the backend; it registers as an external
foreman and takes over trigger handling for a specific guild, allowing independent
scaling and model changes without restarting the backend.

```bash
cd foreman
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# Bedrock support (optional): pip install "anthropic[bedrock]"

# foreman/main.py imports backend/ modules directly, so the backend source
# must be on the path.  Set DATABASE_URL to the same DB the backend uses.
DATABASE_URL=sqlite+aiosqlite:///path/to/pioneer_square.db \
BACKEND_WS_URL=ws://localhost:8000 \
GUILD_ID=<your-6-char-guild-id> \
ANTHROPIC_API_KEY=<key> \
python main.py

# Amazon Bedrock instead of Anthropic API:
DATABASE_URL=sqlite+aiosqlite:///... \
BACKEND_WS_URL=ws://localhost:8000 \
GUILD_ID=<your-6-char-guild-id> \
FOREMAN_PROVIDER=bedrock \
AWS_DEFAULT_REGION=us-east-1 \
FOREMAN_MODEL=us.anthropic.claude-sonnet-4-5-20251001-v2:0 \
python main.py

# Or via docker compose (profile "foreman"):
GUILD_ID=abc123 docker compose --profile foreman up --build foreman
```

When an external foreman is connected, the backend routes `foreman-trigger` events
to it instead of running the embedded loop.  If the external foreman disconnects,
the backend falls back to the embedded foreman automatically.

### Tests and lint

```bash
# Backend tests require the postgres-test container (localhost:5433):
docker compose up -d postgres-test

# Backend (pytest, in backend/)
python -m pytest                       # 119 tests
# Worker (pytest, in worker/)
python -m pytest                       # 49 tests
# Frontend (Vitest, in frontend/)
npm test
npm run type-check

# Lint / format (run from repo root for Python, frontend/ for JS)
ruff check .                           # backend + worker
ruff format .
npm run lint        # eslint --fix
npm run format      # prettier --write
```

Config lives at `ruff.toml` (root) and `frontend/eslint.config.js` + `frontend/.prettierrc.json`. There is no CI wired up yet — these are local guards.

**Always run `ruff check . --fix && ruff format .` from the repo root before committing Python changes.**

## Architecture

### Three-process model

```
Browser ──WebSocket──► Backend (FastAPI/SQLite)
                            │
Worker ──WebSocket──────────┘
```

**Backend** (`backend/main.py`) is a FastAPI app — still mostly one large file (~1700 lines: routes, WS handlers, OAuth) plus the `backend/foreman/` package (`runner.py`, `tools.py`, `prompt.py`, `state.py`) and `backend/events.py` for WS broadcast helpers. Persists all state in `pioneer_square.db` (SQLite via aiosqlite, schema managed by Alembic in `backend/alembic/versions/`), holds in-memory WebSocket connections per guild, and runs the Foreman AI inline as `asyncio.create_task` calls.

**Frontend** (`frontend/src/`) is Vue 3 + Pinia + Vite + TypeScript. It connects to the backend WebSocket for real-time events and uses REST to fetch initial state. Stores in `src/stores/` (`*.ts`) mirror backend state; `guild.ts` owns the WebSocket connection and fan-out to other stores.

**Worker** (`worker/pioneer_worker/`) is a standalone Python process. It registers with the backend via REST, then connects via WebSocket and listens for `task-assigned` events. For each task it creates a git worktree, runs `claude --dangerously-skip-permissions --output-format stream-json`, pushes the branch, and opens a GitHub PR. Workers reconnect automatically if the backend restarts.

### Key terminology

- **Guild**: a workspace (was called "session" — DB migration renames the table; the 6-char ID appears in URLs and `pioneer-worker.toml` as `guild_id`).
- **Worker**: a registered worker entity in the DB (`workers` table, id prefix `w-`). Persisted across restarts.
- **Agent**: a WebSocket participant (`agents` table, id prefix `a-`). A worker process creates one `agent_id` per process lifetime (stable within a run, not across restarts). The `worker_id` is for DB routing; `agent_id` is the live WebSocket identity.
- **Task**: a unit of work (`tasks` table, id prefix `t-`). Foreman-created tasks have `worker_id=NULL` (unassigned) until the foreman's `assign_task` tool sets a real worker; worker tasks are owned by a real worker.

### Task lifecycle

`pending` → `working` → `awaiting-review` → follow-up loop → `done` / `failed`

After a worker sends `task-complete`, the backend triggers the Foreman AI. The foreman either calls `send_followup` (worker re-runs Claude in the same worktree on the same branch) or `finalize_task` (marks done). A 300-second timeout auto-finalizes if the foreman doesn't respond.

### Foreman AI

Lives in `backend/foreman/` (`runner.py` for the Claude SDK loop, `tools.py` for tool definitions, `prompt.py` for the system prompt, `state.py` for in-memory conversation history). Uses `claude-sonnet-4-6`. Conversation history is kept in-memory (trimmed to 40 messages). The foreman is triggered by:
1. Human chat messages addressed to `foreman`
2. `task-complete` WS messages from workers
3. `task-followup-done` WS messages
4. `needs-input` worker escalations

**Phase 2 — standalone foreman**: `foreman/main.py` is an opt-in external foreman process.  It connects to the backend WS with `agentType="foreman"` and `external=true`; the backend routes triggers to it and the embedded loop becomes a fallback.  See `foreman/Dockerfile` and the `foreman` service in `docker-compose.yml`.

### WebSocket message protocol

All real-time communication is JSON over `ws://localhost:8000/ws/{guild_id}`. Key message types:

| Type | Direction | Purpose |
|------|-----------|---------|
| `join` | worker→backend | Register agent on connect |
| `worker-register` | worker→backend | Announce repos |
| `agent-state` | worker→backend | State change: idle/thinking/working/busy/error/offline |
| `terminal-output` | worker→backend | Log line (with optional `taskId` for persistence) |
| `task-assigned` | backend→worker | New task dispatch |
| `task-update` | worker→backend | Persist state/branch/PR fields |
| `task-complete` | worker→backend | Task done, triggers foreman review |
| `task-followup` | backend→worker | Follow-up instructions for same worktree |
| `task-followup-done` | worker→backend | Follow-up finished |
| `task-finalize` | backend→worker | No more follow-ups needed |
| `needs-input` | worker→backend | Claude stopped and needs human input |
| `foreman-trigger` | backend→foreman | Trigger an external foreman AI run |
| `foreman-broadcast` | foreman→backend | External foreman relays a broadcast to frontend clients |
| `foreman-registered` | backend→foreman | Confirms external foreman registration |
| `foreman-evicted` | backend→foreman | Another foreman connected; this one should exit |
| `offer`/`answer`/`ice-candidate` | any→backend | WebRTC signaling (forwarded to all peers) |

### Database schema

Tables: `guilds`, `agents`, `workers`, `tasks`, `task_logs`, `messages`, `github_tokens`, `claude_credentials`, `user_sessions`. Schema is defined in `backend/models.py` (SQLAlchemy ORM) and migrated by Alembic — see `backend/alembic/versions/`. `init_db()` runs `alembic upgrade head` on startup; pre-Alembic databases are stamped to `head` so the upgrade is a no-op. On every backend startup, all workers and worker agents are reset to `offline`.

### Worker internals

`worker/pioneer_worker/worker.py` has three concurrent asyncio tasks:
- `_listen()` — processes incoming WS messages (task assignments, follow-ups, finalize signals, mid-task stdin injections via `worker-message`)
- `_task_runner()` — serial task execution loop (one task at a time per worker process)
- `_idle_puller()` — polls REST every `pull_interval` seconds to pick up tasks missed during downtime; also runs `git pull` on repos while idle

Runners: `claude_runner.py` (primary), `codex_runner.py`, `pi_runner.py`. All return `(success: bool, stop_reason: str, last_text: str)`. The claude runner uses a 16 MiB stdout line limit to handle large `tool_result` payloads.

### Auth

GitHub OAuth flow: frontend triggers `/auth/github/login` → GitHub redirects to `/auth/github/callback` → backend stores token in `github_tokens`, issues a `login_token`, redirects to frontend with params in query string. Frontend stores the token in `localStorage`; sends it as `Authorization: Bearer <token>` on REST calls. Workers fetch the OAuth token from `/auth/github/token?guild_id=...` if no token is in their config.

### Frontend stores

| Store | Owns |
|-------|------|
| `auth.ts` | Login token, GitHub user info, OAuth flow |
| `guild.ts` | WebSocket connection, guild list/current guild, message history |
| `agents.ts` | Agent list, agent states, terminal output buffers |
| `tasks.ts` | Task list, task logs (in-memory + fetched), WS event handler |
| `github.ts` | GitHub issue/PR fetching for the UI |

`guild.ts` is the WS fan-out point — other stores register handlers via `addMessageHandler` and process events they care about.
