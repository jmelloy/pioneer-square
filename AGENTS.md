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
The opt-in standalone foreman lives in `foreman-proxy/`. `cli/` holds the unified `pioneer` launcher
(`cli/pioneer_cli/`) and the single `cli/pyproject.toml` that installs and serves all three
runtimes. Shared operational docs are in `docs/`, prompt text in `prompts/`, and helper scripts in
`scripts/`.

## Build, Test, and Development Commands

### Unified CLI

All three Python runtimes install from one package (`cli/pyproject.toml`) and run through a single
`pioneer` command with three modes: `pioneer serve` (HTTP backend), `pioneer foreman`, and
`pioneer worker`. `pioneer-worker` and `pioneer-foreman` remain as backward-compatible aliases.

```bash
uv venv && source .venv/bin/activate
uv pip install -e "cli[test]"      # one install serves all modes
```

The launcher keeps the existing source trees in place (`backend/`, `foreman-proxy/`, `worker/`) and
puts the right directory on `sys.path` for the selected mode, without moving source or rewriting
imports (repo root resolved from `cli/`'s parent, overridable via `PIONEER_ROOT`).

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
cp foreman-proxy/pioneer-foreman.toml.example foreman-proxy/pioneer-foreman.toml
# Edit: backend_url, guild_id, [llm] provider/model/base_url/api_key
pioneer foreman --config foreman-proxy/pioneer-foreman.toml
pioneer foreman --config foreman-proxy/pioneer-foreman.toml --log-level DEBUG   # verbose

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
`.github/workflows/ci.yml` runs ruff lint plus backend, worker, and frontend tests on push/PR.

## Testing Guidelines

Backend and worker tests use pytest with `asyncio_mode = auto`; run `python -m pytest` from
`backend/` or `worker/`. Frontend unit tests use Vitest: run `npm test` from `frontend/`, or
`npm run test:coverage` for coverage. Name Python tests `test_*.py`; place frontend specs as
`*.spec.ts` near the code or under `src/**/__tests__/`. Add tests for WebSocket flows, task
lifecycle changes, and store updates when touching those paths.

```bash
# Backend tests require the postgres-test container (localhost:5433):
docker compose up -d postgres-test
cd backend && python -m pytest         # currently ~1000 tests
cd worker && python -m pytest          # currently ~250 tests
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

**Backend** (`backend/main.py`) is a FastAPI app. `main.py` is currently a thin ~570-line wiring
module: routes live under `backend/routes/`, OAuth helpers in `backend/oauth.py`, WS message
handling in `backend/ws_handlers.py`/`ws_types.py`, and the Foreman AI in the `backend/foreman/`
package. Persists all state in `pioneer_square.db` (SQLite via aiosqlite, schema managed by
Alembic in `backend/alembic/versions/`), holds in-memory WebSocket connections per guild, and runs
the Foreman AI as background tasks via the `spawn()` helper in `backend/util/tasks.py` (a tracked
wrapper around `asyncio.create_task`).

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
- **Agent**: a WebSocket participant (`agents` table, id prefix `a-`). A worker process runs
  `max_agents` concurrent agent slots, each with its own `agent_id` (stable within a run, not
  across restarts). The `worker_id` is for DB routing; `agent_id` is the live WebSocket identity.
- **Task**: a unit of work (`tasks` table, id prefix `t-`). Foreman-created tasks have
  `worker_id=NULL` (unassigned) until the foreman's `assign_task` tool sets a real worker; worker
  tasks are owned by a real worker.

### Task lifecycle

`pending` → `working` → `awaiting-review` → follow-up loop → `done` / `failed`

After a worker sends `task-complete`, the backend triggers the Foreman AI. The foreman either
calls `send_followup` (worker re-runs Claude in the same worktree on the same branch) or
`finalize_task` (marks done). A 300-second timeout auto-finalizes if the foreman doesn't respond.
Independently, a GitHub webhook auto-finalizes a task to `done` when its PR is merged, without
waiting on the foreman.

### Foreman AI

Lives in `backend/foreman/` — key modules are `runner.py` (the Claude SDK loop), `tools.py` (tool
definitions), and `prompt.py` (system prompt); the package has grown to include auth, proxy, and
LLM-provider helpers too. Currently uses `claude-sonnet-4-6` (`FOREMAN_MODEL` env var). Conversation
history is DB-backed (loaded per guild/user from the `messages`/`foreman_turns` tables), windowed
to the last few human turns and capped at `MAX_HISTORY_MESSAGES` (currently 20) before each call.
The foreman is triggered by:
1. Human chat messages addressed to `foreman`
2. `task-complete` WS messages from workers
3. `task-followup-done` WS messages
4. `needs-input` worker escalations

**Standalone Foreman API proxy**: `foreman-proxy/pioneer_foreman/` (run via `pioneer foreman`) is an
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

Core tables include `guilds`, `agents`, `workers`, `tasks`, `task_logs`, `messages`,
`github_tokens`, `user_sessions`; the schema has grown well beyond this list
(Discord integration, GitHub issue/PR caching, usage tracking, etc.) — `backend/models.py`
(SQLModel ORM) is the authoritative source. Migrated by Alembic — see
`backend/alembic/versions/`. In docker-compose the backend container command runs
`alembic upgrade head` before `pioneer serve`; on ECS, migrations run as a dedicated
one-off `migrate` task during deploy (before the services roll — see `terraform/ecs.tf`
and `.github/workflows/deploy.yml`), so backend startup itself never runs migrations. On
every backend startup, all workers and worker agents are reset to `offline`.

### Worker internals

`worker/pioneer_worker/worker.py` runs several concurrent asyncio tasks:
- `_listen()` — processes incoming WS messages (task assignments, follow-ups, finalize signals,
  mid-task stdin injections via `worker-message`)
- `_agent_loop(slot)` — one instance per agent slot (`max_agents` config, default 4), so a single
  worker process can run that many tasks concurrently rather than strictly serially
- `_idle_puller()` — polls REST every `pull_interval` seconds to pick up tasks missed during
  downtime; also runs `git pull` on repos while idle
- `_worktree_sweeper()` and (when configured) `_s3_syncer()` — background maintenance tasks

Runners: `claude_runner.py` (primary), `codex_runner.py`, `pi_runner.py`. All return
`(success: bool, stop_reason: str, last_text: str)`. The claude runner uses a 16 MiB stdout line
limit to handle large `tool_result` payloads.

#### Model visibility: claude vs codex vs pi

`GET /api/models` (`backend/routes/models.py` + `backend/util/models_dev.py`) is the only
programmatic model catalog in this app, and it backs the model dropdown in the frontend's
`AgentActions.vue`. It recognizes exactly two provider ids — `anthropic` and `bedrock`
(`_PROVIDER_ALIASES` in `models_dev.py`), sourced from the models.dev catalog. The frontend's
`TOOL_PROVIDER` map (`claude: 'anthropic'`, `codex: 'openai'`) means **only Claude gets a live
model dropdown today**; Codex's `'openai'` id isn't in `_PROVIDER_ALIASES`, so `modelsForProvider`
returns `[]` for it and it silently falls back to the same free-text model input as Pi (which has
no `TOOL_PROVIDER` entry at all).

Pi itself supports far more providers than either catalog entry: `pi --help` lists ~25 provider
API-key env vars (Anthropic, OpenAI, Azure OpenAI, Google Gemini, AWS Bedrock, OpenRouter, Groq,
Mistral, xAI, DeepSeek, Cloudflare, Qwen, and more), and picks whichever is present in the
environment — `worker.py`'s `_tool_has_credentials()` deliberately returns `True` unconditionally
for `pi` ("pi and any other tool need no credentials") since the required key varies per
provider/model the caller requests, unlike Claude/Codex which are dropped from
`_available_tools` when their one required key is missing. Pi's own model list is queryable with
`pi --list-models [search]` (the optional arg fuzzy-filters model names, not providers), but that
list is filtered to whatever provider(s) currently have usable credentials in the environment —
it is not a static catalog. Confirmed live in this sandbox: with only `AWS_BEARER_TOKEN_BEDROCK`
set, `pi --list-models` returns 109 Bedrock-hosted models (including Bedrock's Anthropic/OpenAI/
Google-branded models) and nothing from any other provider.

Critically, this listing is CLI-only — Pi's `--mode rpc` protocol has no equivalent. Sending
`{"type": "list_models"}` over RPC returns `{"success": false, "error": "Unknown command:
list_models"}` (verified against the installed `pi` binary). So neither the backend nor the
worker can fetch Pi's live model list programmatically the way `/api/models` does for
Claude/Bedrock; surfacing it in the UI would require shelling out to `pi --list-models` and
parsing its tabular text output.

### Auth

GitHub OAuth flow: frontend calls `/auth/github/login` (gets an authorize URL) → GitHub redirects
back with `code`+`state`. By default `GITHUB_REDIRECT_URI` points at the frontend, which POSTs
`code`+`state` to `/auth/github/exchange`; the backend stores the token in `github_tokens` and
returns a `login_token` + GitHub profile fields as JSON. (`/auth/github/callback` is a legacy path
for setups where GitHub redirects to the backend instead — same exchange, then a redirect to the
frontend with the same fields in the query string.) Frontend stores the token in `localStorage`
and sends it as `Authorization: Bearer <token>` on REST calls. Workers fetch the OAuth token from
`/auth/github/token?guild_id=...` if none is in their config.

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
