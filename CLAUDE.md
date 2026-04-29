# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Pioneer Square is a real-time multi-agent workspace: a pixel-art steampunk factory floor UI where a **Foreman AI** (Claude) coordinates **worker processes** that autonomously clone repos, run Claude on tasks, and open GitHub PRs. Three independent processes must all be running for the full system to work.

## Commands

### Backend
```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
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
python -m venv venv && source venv/bin/activate
pip install -e .
cp pioneer-worker.toml.example pioneer-worker.toml
# Edit: backend_url, guild_id, [github] repos and token
pioneer-worker
pioneer-worker --log-level DEBUG   # verbose
```

There are no automated tests. There is no linter configuration.

## Architecture

### Three-process model

```
Browser ──WebSocket──► Backend (FastAPI/SQLite)
                            │
Worker ──WebSocket──────────┘
```

**Backend** (`backend/main.py`) is a single-file FastAPI app. It is the hub: persists all state in `pioneer_square.db` (SQLite via aiosqlite), holds in-memory WebSocket connections per guild, and runs the Foreman AI inline as `asyncio.create_task` calls.

**Frontend** (`frontend/src/`) is Vue 3 + Pinia + Vite. It connects to the backend WebSocket for real-time events and uses REST to fetch initial state. Stores in `src/stores/` mirror backend state; `guild.js` owns the WebSocket connection and fan-out to other stores.

**Worker** (`worker/pioneer_worker/`) is a standalone Python process. It registers with the backend via REST, then connects via WebSocket and listens for `task-assigned` events. For each task it creates a git worktree, runs `claude --dangerously-skip-permissions --output-format stream-json`, pushes the branch, and opens a GitHub PR. Workers reconnect automatically if the backend restarts.

### Key terminology

- **Guild**: a workspace (was called "session" — DB migration renames the table; the 6-char ID appears in URLs and `pioneer-worker.toml` as `guild_id`).
- **Worker**: a registered worker entity in the DB (`workers` table, id prefix `w-`). Persisted across restarts.
- **Agent**: a WebSocket participant (`agents` table, id prefix `a-`). A worker process creates one `agent_id` per process lifetime (stable within a run, not across restarts). The `worker_id` is for DB routing; `agent_id` is the live WebSocket identity.
- **Task**: a unit of work (`tasks` table, id prefix `t-`). Foreman tasks have `worker_id='foreman'`; worker tasks are owned by a real worker.

### Task lifecycle

`pending` → `working` → `awaiting-review` → follow-up loop → `done` / `failed`

After a worker sends `task-complete`, the backend triggers the Foreman AI. The foreman either calls `send_followup` (worker re-runs Claude in the same worktree on the same branch) or `finalize_task` (marks done). A 300-second timeout auto-finalizes if the foreman doesn't respond.

### Foreman AI

Defined entirely in `backend/main.py`. Uses `claude-sonnet-4-6` with five tools: `create_task`, `assign_task`, `send_followup`, `finalize_task`, `message_worker`. Conversation history is kept in-memory (`foreman_conversations` dict, trimmed to 40 messages). The foreman is triggered by:
1. Human chat messages addressed to `foreman`
2. `task-complete` WS messages from workers
3. `task-followup-done` WS messages
4. `needs-input` worker escalations

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
| `offer`/`answer`/`ice-candidate` | any→backend | WebRTC signaling (forwarded to all peers) |

### Database schema

Tables: `guilds`, `agents`, `workers`, `tasks`, `task_logs`, `github_tokens`, `user_sessions`. All migrations are inline in `init_db()` — `ALTER TABLE ... ADD COLUMN` calls are wrapped in try/except to be idempotent. On every backend startup, all workers and worker agents are reset to `offline`.

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
| `auth.js` | Login token, GitHub user info, OAuth flow |
| `guild.js` | WebSocket connection, guild list/current guild, message history |
| `agents.js` | Agent list, agent states, terminal output buffers |
| `tasks.js` | Task list, task logs (in-memory + fetched), WS event handler |
| `github.js` | GitHub issue/PR fetching for the UI |

`guild.js` is the WS fan-out point — other stores register handlers via `addMessageHandler` and process events they care about.
