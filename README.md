# Pioneer Square

A real-time multi-agent workspace with a colorful pixel-art factory floor UI.

![Factory Floor](screenshots/factory-floor.png)

## Features

- 🏭 Pixel-art steampunk factory floor with animated gears, steam, and furnaces
- 🤖 Agent avatars with state-based animations (idle/thinking/working/busy/error)
- 💬 Real-time chat with the Foreman agent
- 🖥️ Terminal log panes per agent
- 📡 WebSocket signaling for real-time updates (the backend also relays WebRTC
  offer/answer/ICE messages, though no client currently establishes peer connections)
- 🔑 Short 6-char guild URLs for sharing

## Setup

### Docker Compose (quickstart)

```bash
cp .env.example .env
# fill in GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, ANTHROPIC_API_KEY
docker compose up --build
```

App (backend + SPA): http://localhost:8056. PostgreSQL data is persisted in the `postgres-data` volume.

`docker compose up` starts a `postgres:18` container automatically, waits for its health-check,
then runs `alembic upgrade head` before the backend accepts connections.

The worker is opt-in (it needs a `pioneer-worker.toml`):

```bash
cp worker/pioneer-worker.toml.example worker/pioneer-worker.toml
# edit guild_id, repos. Use backend_url = "http://backend:8000"
docker compose --profile worker up --build worker
```

The standalone foreman process is also opt-in — it offloads Foreman LLM API calls out of the
backend process, e.g. to scale or restart the foreman independently. As of this writing it
authenticates with a short-lived JWT signed using a shared secret, set as `PIONEER_FOREMAN_KEY`
on both sides (generate one with `openssl rand -hex 32`):

```bash
# Backend:
PIONEER_FOREMAN_KEY=<your-secret>

# Foreman (docker compose):
GUILD_ID=abc123 PIONEER_FOREMAN_KEY=<your-secret> docker compose --profile foreman up --build foreman
```

See [Standalone Foreman](#standalone-foreman-local-no-docker) below for the non-Docker setup.

### GitHub OAuth App

Pioneer Square authenticates users via GitHub OAuth rather than personal access tokens. Create an
OAuth App at **GitHub → Settings → Developer settings → OAuth Apps → New OAuth App**
(https://github.com/settings/applications/new) with:

- **Homepage URL**: `http://localhost:5173`
- **Authorization callback URL**: `http://localhost:8000/auth/github/callback`

Then generate a client secret and set both in `backend/.env` (or the shell environment) before
starting the backend:

```
GITHUB_CLIENT_ID=your_client_id_here
GITHUB_CLIENT_SECRET=your_client_secret_here
# Optional overrides (defaults shown):
# GITHUB_REDIRECT_URI=http://localhost:8000/auth/github/callback
# FRONTEND_URL=http://localhost:5173
```

### Install the CLI

The HTTP server, foreman, and worker all install from one package and run through a single
`pioneer` command:

```bash
uv venv                              # creates .venv/
source .venv/bin/activate            # Windows: .venv\Scripts\activate
uv pip install -e "cli[test]"        # one install for all modes
```

### Backend (HTTP server)

```bash
pioneer serve --port 8000            # --reload for auto-reload in dev
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 — a new guild will be created automatically.

### Worker

Worker agents run as standalone processes (one per worker) and connect to the
backend over WebSocket. They can run on any machine that has `claude`, `git`,
and access to the configured repos.

```bash
cp worker/pioneer-worker.toml.example worker/pioneer-worker.toml
# edit pioneer-worker.toml: backend_url, guild_id, repos
# github_token is optional — if omitted, the worker fetches the OAuth token
# stored in the backend DB (set after the user connects via GitHub OAuth)
pioneer worker --config worker/pioneer-worker.toml
```

See [`worker/README.md`](worker/README.md) for details.

### Standalone Foreman (local, no Docker)

As of this writing, the backend-owned foreman loop remains responsible for state, history,
tools, and task coordination; the standalone process is only an optional LLM API proxy for
calling Anthropic, Bedrock, or an OpenAI-compatible endpoint from a different network environment.

```bash
cp foreman-proxy/pioneer-foreman.toml.example foreman-proxy/pioneer-foreman.toml
# edit pioneer-foreman.toml: backend_url, guild_id, [llm] provider/model/base_url/api_key
pioneer foreman --config foreman-proxy/pioneer-foreman.toml
```

Or with environment variables only (no config file):

```bash
PIONEER_BACKEND_URL=ws://localhost:8000 \
PIONEER_GUILD_ID=<your-guild-id> \
FOREMAN_PROVIDER=anthropic \
ANTHROPIC_API_KEY=<key> \
pioneer foreman
```

When the proxy is connected the backend sends `foreman-api-request` messages to it
for provider calls. If it disconnects, the backend falls back to local provider calls
from the embedded foreman.

### Discord integration (optional)

As of this writing, Pioneer Square can post event notifications, per-PR/issue discussion threads,
a live Foreman chat mirror, and `/ps` slash commands into a Discord server. It's entirely
opt-in — with no `DISCORD_*` env vars set, nothing changes. See
[`docs/discord.md`](docs/discord.md) for setup.

## Migrating from SQLite

If you have an existing Pioneer Square SQLite database (`pioneer_square.db`)
and want to move its data to PostgreSQL, use the included migration script:

```bash
# Ensure the target PostgreSQL schema is up-to-date first:
cd backend && alembic upgrade head && cd ..

pip install psycopg2-binary

python scripts/migrate_sqlite_to_postgres.py \
    --sqlite-path /path/to/pioneer_square.db \
    --postgres-url postgresql://pioneer:pioneer_password@localhost/pioneer_square
```

The script accepts `SQLITE_PATH` and `DATABASE_URL` env vars as alternatives
to the flags. It is idempotent — running it multiple times is safe; rows that
already exist in PostgreSQL are silently skipped.

## Architecture

Three processes as of this writing: a FastAPI backend (WebSocket + REST, PostgreSQL via asyncpg),
a Vue 3/Pinia frontend, and standalone worker processes that run Claude on assigned tasks and
open GitHub PRs. A fourth, opt-in standalone foreman can offload LLM API calls from the backend.
See [AGENTS.md](AGENTS.md) for the full reference (terminology, task lifecycle, WebSocket
protocol, database schema, and store layout).

## Connecting Agents

Agents connect via WebSocket at `ws://localhost:8000/ws/{guild_id}` and send messages such as:

```json
{ "type": "join", "agentId": "agent-1", "agentName": "Builder", "agentType": "worker" }
{ "type": "agent-state", "agentId": "agent-1", "state": "working" }
```

States: `idle` | `thinking` | `working` | `busy` | `error`. See AGENTS.md for the full message
protocol table.

## A2A AgentCard

Pioneer Square currently implements the [A2A (Agent-to-Agent) protocol](https://google.github.io/A2A/)
AgentCard discovery document. Each guild exposes its identity at:

```
GET /.well-known/agent.json        # subdomain-routed (e.g. myguild.pioneer-square.melloy.life)
GET /guilds/{guild_id}/agent-card  # direct REST (requires auth)
```

The card describes the guild's Foreman AI and lists currently-online workers as skills
(`name`, `description`, `url`, `version`, `capabilities`, `skills[]`, `provider`).

Guild-level fields are editable via `PATCH /guilds/{guild_id}` with a JSON body of
`description`, `url`, and/or `version` (all optional; unset fields fall back to defaults).
