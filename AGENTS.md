# Repository Guidelines

This is the canonical guidance file for all coding agents and contributors working in
Pioneer Square. `CLAUDE.md` points here.

## What this is

Pioneer Square is a real-time multi-agent workspace: a pixel-art steampunk factory floor UI
where a **Foreman AI** (Claude) coordinates **worker processes** that autonomously clone repos,
run Claude on tasks, and open GitHub PRs. Three independent processes must all be running for
the full system to work (a fourth — the standalone foreman — is opt-in; see Architecture).

## Project Structure & Module Organization

Pioneer Square is split into three main runtimes. `backend/` contains the FastAPI app,
SQLite/Alembic schema, WebSocket handlers, and Foreman logic under `backend/foreman/`.
`frontend/` is a Vue 3 + Pinia + Vite app; source lives in `frontend/src/`, static files in
`frontend/public/`, and end-to-end tests in `frontend/tests/e2e/`. `worker/` contains the
standalone Python worker package in `worker/pioneer_worker/` and its tests in `worker/tests/`.
The opt-in standalone foreman lives in `foreman/`. `cli/` holds the unified `pioneer` launcher
(`cli/pioneer_cli/`) and the single `cli/pyproject.toml` that installs and serves all three
runtimes — there are no longer separate per-runtime `pyproject.toml` files. Shared operational docs
are in `docs/`, prompt text in `prompts/`, and helper scripts in `scripts/`.

## Build, Test, and Development Commands

### Unified CLI

All three Python runtimes install from one package (`cli/pyproject.toml`) and run through a single
`pioneer` command with three modes: `pioneer serve` (HTTP backend), `pioneer foreman`, and
`pioneer worker`. `pioneer-worker` and `pioneer-foreman` remain as backward-compatible aliases.

```bash
uv venv && source .venv/bin/activate
uv pip install -e "cli[test]"      # one install serves all modes
```

The launcher keeps the existing source trees in place (`backend/`, `foreman/`, `worker/`) and puts
the right directory on `sys.path` for the selected mode (resolving the repo root from `cli/`'s
parent, overridable via `PIONEER_ROOT`). It does not move source or rewrite imports.

### Backend (HTTP server)
```bash
# Requires GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET (see README) or backend/.env
pioneer serve --port 8000          # --reload for dev auto-reload
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
cp worker/pioneer-worker.toml.example worker/pioneer-worker.toml
# Edit: backend_url, guild_id, [github] repos and token
pioneer worker --config worker/pioneer-worker.toml
pioneer worker --config worker/pioneer-worker.toml --log-level DEBUG   # verbose
```

### Standalone Foreman (opt-in)
The embedded foreman (inside the backend process) owns trigger handling, state, history,
tool execution, and polling. A standalone foreman process can be run alongside the backend as
a thin LLM API proxy: it registers for a specific guild, receives `foreman-api-request`
messages, calls the configured provider (Anthropic, Bedrock, or an OpenAI-compatible endpoint
such as Ollama), and returns `foreman-api-response`.

```bash
cp foreman/pioneer-foreman.toml.example foreman/pioneer-foreman.toml
# Edit: backend_url, guild_id, [llm] provider/model/base_url/api_key
pioneer foreman --config foreman/pioneer-foreman.toml
pioneer foreman --config foreman/pioneer-foreman.toml --log-level DEBUG   # verbose

# Or via environment variables (no config file needed):
PIONEER_BACKEND_URL=ws://localhost:8000 \
PIONEER_GUILD_ID=<your-6-char-guild-id> \
FOREMAN_PROVIDER=anthropic \
ANTHROPIC_API_KEY=<key> \
pioneer foreman

# OpenAI-compatible local endpoint example:
PIONEER_BACKEND_URL=ws://localhost:8000 \
PIONEER_GUILD_ID=<your-6-char-guild-id> \
FOREMAN_PROVIDER=openai \
FOREMAN_MODEL=llama3.1 \
FOREMAN_BASE_URL=http://localhost:11434/v1 \
pioneer foreman

# Or via docker compose (profile "foreman"):
GUILD_ID=abc123 docker compose --profile foreman up --build foreman
```

When an external proxy is connected, the backend still runs the embedded Foreman loop and
delegates only LLM API calls to the proxy. If the proxy disconnects, subsequent turns use the
backend's direct provider configuration.

### Quickstart
- `docker compose up --build`: run the backend and SPA quickstart.

## Coding Style & Naming Conventions

Python targets 3.11 with Ruff configured in `ruff.toml` using 100-character lines, import
sorting, pyupgrade, and bugbear rules. Use `snake_case` for Python modules, functions, and tests.
Frontend code uses TypeScript, Vue single-file components, ESLint, and Prettier; use
`PascalCase.vue` for components, `camelCase` for utilities and store members, and keep Pinia
stores in `frontend/src/stores/`.

**Always run `ruff check . --fix && ruff format .` from the repo root before committing Python
changes.**

```bash
ruff check .                           # backend + worker
ruff format .
npm run lint        # eslint --fix (run from frontend/)
npm run format      # prettier --write (run from frontend/)
```

Config lives at `ruff.toml` (root) and `frontend/eslint.config.js` + `frontend/.prettierrc.json`.
There is no CI wired up yet — these are local guards.

## Testing Guidelines

Backend and worker tests use pytest with `asyncio_mode = auto`; run `python -m pytest` from
`backend/` or `worker/`. Frontend unit tests use Vitest: run `npm test` from `frontend/`, or
`npm run test:coverage` for coverage. Name Python tests `test_*.py`; place frontend specs as
`*.spec.ts` near the code or under `src/**/__tests__/`. Add tests for WebSocket flows, task
lifecycle changes, and store updates when touching those paths.

```bash
# Backend tests require the postgres-test container (localhost:5433):
docker compose up -d postgres-test
cd backend && python -m pytest         # 119 tests
cd worker && python -m pytest          # 49 tests
cd frontend && npm test && npm run type-check
```

## Commit & Pull Request Guidelines

Recent history uses concise imperative commits, often Conventional Commit style such as
`fix(worker): ...`, `style: ...`, or short maintenance messages like `ruff`. Keep commits focused
and mention the subsystem when helpful. Pull requests should include a brief problem/solution
summary, test commands run, linked issues or tasks, and screenshots for visible frontend changes.

## Security & Configuration Tips

Do not commit secrets or local state. Use `backend/.env` for GitHub OAuth values, copy
`worker/pioneer-worker.toml.example` before editing worker configuration, and keep
`pioneer_square.db` and generated build output out of review unless explicitly required.

## Architecture

### Three-process model

```
Browser ──WebSocket──► Backend (FastAPI/SQLite)
                            │
Worker ──WebSocket──────────┘
```

**Backend** (`backend/main.py`) is a FastAPI app — still mostly one large file (~1700 lines:
routes, WS handlers, OAuth) plus the `backend/foreman/` package (`runner.py`, `tools.py`,
`prompt.py`, `state.py`) and `backend/events.py` for WS broadcast helpers. Persists all state in
`pioneer_square.db` (SQLite via aiosqlite, schema managed by Alembic in
`backend/alembic/versions/`), holds in-memory WebSocket connections per guild, and runs the
Foreman AI inline as `asyncio.create_task` calls.

**Frontend** (`frontend/src/`) is Vue 3 + Pinia + Vite + TypeScript. It connects to the backend
WebSocket for real-time events and uses REST to fetch initial state. Stores in `src/stores/`
(`*.ts`) mirror backend state; `guild.ts` owns the WebSocket connection and fan-out to other
stores.

**Worker** (`worker/pioneer_worker/`) is a standalone Python process. It registers with the
backend via REST, then connects via WebSocket and listens for `task-assigned` events. For each
task it creates a git worktree, runs `claude --dangerously-skip-permissions --output-format
stream-json`, pushes the branch, and opens a GitHub PR. Workers reconnect automatically if the
backend restarts.

### Key terminology

- **Guild**: a workspace (was called "session" — DB migration renames the table; the 6-char ID
  appears in URLs and `pioneer-worker.toml` as `guild_id`).
- **Worker**: a registered worker entity in the DB (`workers` table, id prefix `w-`). Persisted
  across restarts.
- **Agent**: a WebSocket participant (`agents` table, id prefix `a-`). A worker process creates
  one `agent_id` per process lifetime (stable within a run, not across restarts). The `worker_id`
  is for DB routing; `agent_id` is the live WebSocket identity.
- **Task**: a unit of work (`tasks` table, id prefix `t-`). Foreman-created tasks have
  `worker_id=NULL` (unassigned) until the foreman's `assign_task` tool sets a real worker; worker
  tasks are owned by a real worker.

### Task lifecycle

`pending` → `working` → `awaiting-review` → follow-up loop → `done` / `failed`

After a worker sends `task-complete`, the backend triggers the Foreman AI. The foreman either
calls `send_followup` (worker re-runs Claude in the same worktree on the same branch) or
`finalize_task` (marks done). A 300-second timeout auto-finalizes if the foreman doesn't respond.

### Foreman AI

Lives in `backend/foreman/` (`runner.py` for the Claude SDK loop, `tools.py` for tool
definitions, `prompt.py` for the system prompt, `state.py` for in-memory conversation history).
Uses `claude-sonnet-4-6`. Conversation history is kept in-memory (trimmed to 40 messages). The
foreman is triggered by:
1. Human chat messages addressed to `foreman`
2. `task-complete` WS messages from workers
3. `task-followup-done` WS messages
4. `needs-input` worker escalations

**Standalone Foreman API proxy**: `foreman/pioneer_foreman/` (run via `pioneer foreman`) is an
opt-in external LLM API proxy. It connects to the backend WS with `agentType="foreman"` and
`external=true`; the backend still runs the embedded Foreman loop and sends only
`foreman-api-request` calls to the proxy. See the `foreman` build target in the root
`Dockerfile` and the `foreman` service in `docker-compose.yml`.

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
| `foreman-api-request` | backend→foreman | Ask external proxy to execute one LLM API request |
| `foreman-api-response` | foreman→backend | Return one proxied LLM API response or error |
| `foreman-registered` | backend→foreman | Confirms external foreman registration |
| `foreman-evicted` | backend→foreman | Another foreman connected; this one should exit |
| `offer`/`answer`/`ice-candidate` | any→backend | WebRTC signaling (forwarded to all peers) |

### Database schema

Tables: `guilds`, `agents`, `workers`, `tasks`, `task_logs`, `messages`, `github_tokens`,
`claude_credentials`, `user_sessions`. Schema is defined in `backend/models.py` (SQLAlchemy ORM)
and migrated by Alembic — see `backend/alembic/versions/`. `init_db()` runs `alembic upgrade head`
on startup; pre-Alembic databases are stamped to `head` so the upgrade is a no-op. On every
backend startup, all workers and worker agents are reset to `offline`.

### Worker internals

`worker/pioneer_worker/worker.py` has three concurrent asyncio tasks:
- `_listen()` — processes incoming WS messages (task assignments, follow-ups, finalize signals,
  mid-task stdin injections via `worker-message`)
- `_task_runner()` — serial task execution loop (one task at a time per worker process)
- `_idle_puller()` — polls REST every `pull_interval` seconds to pick up tasks missed during
  downtime; also runs `git pull` on repos while idle

Runners: `claude_runner.py` (primary), `codex_runner.py`, `pi_runner.py`. All return
`(success: bool, stop_reason: str, last_text: str)`. The claude runner uses a 16 MiB stdout line
limit to handle large `tool_result` payloads.

### Auth

GitHub OAuth flow: frontend triggers `/auth/github/login` → GitHub redirects to
`/auth/github/callback` → backend stores token in `github_tokens`, issues a `login_token`,
redirects to frontend with params in query string. Frontend stores the token in `localStorage`;
sends it as `Authorization: Bearer <token>` on REST calls. Workers fetch the OAuth token from
`/auth/github/token?guild_id=...` if no token is in their config.

### Frontend stores

| Store | Owns |
|-------|------|
| `auth.ts` | Login token, GitHub user info, OAuth flow |
| `guild.ts` | WebSocket connection, guild list/current guild, message history |
| `agents.ts` | Agent list, agent states, terminal output buffers |
| `tasks.ts` | Task list, task logs (in-memory + fetched), WS event handler |
| `github.ts` | GitHub issue/PR fetching for the UI |

`guild.ts` is the WS fan-out point — other stores register handlers via `addMessageHandler` and
process events they care about.
