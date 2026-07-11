# Pioneer Square

A real-time multi-agent workspace with a colorful pixel-art factory floor UI.

![Factory Floor](screenshots/factory-floor.png)

## Features

- 🏭 Pixel-art steampunk factory floor with animated gears, steam, and furnaces
- 🤖 Agent avatars with state-based animations (idle/thinking/working/busy/error)
- 💬 Real-time chat with the Foreman agent
- 🖥️ Terminal log panes per agent
- 🔗 WebRTC peer connections for agent-to-agent communication
- 📡 WebSocket signaling for real-time updates
- 🔑 Short 6-char session URLs for sharing

## Setup

### Docker Compose (quickstart)

```bash
cp .env.example .env
# fill in GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, ANTHROPIC_API_KEY
docker compose up --build
```

App (backend + SPA): http://localhost:8056. PostgreSQL data is persisted in the `postgres-data` volume.

`docker compose up` starts a `postgres:17` container automatically. The backend waits for it
to pass its health-check, then runs `alembic upgrade head` before accepting connections.

The worker is opt-in (it needs a `pioneer-worker.toml`):

```bash
cp worker/pioneer-worker.toml.example worker/pioneer-worker.toml
# edit guild_id, repos. Use backend_url = "http://backend:8000"
docker compose --profile worker up --build worker
```

The standalone foreman process (Phase 2) is also opt-in.  It offloads the Foreman
AI out of the backend process — useful if you want to scale or restart the foreman
independently.  The backend falls back to its embedded foreman when no external one
is connected.

The foreman authenticates to the backend with a shared HMAC secret
(`PIONEER_FOREMAN_KEY`).  Generate one with:

```bash
openssl rand -hex 32
```

Set the **same value** on both sides — `PIONEER_FOREMAN_KEY` on the backend and
`GUILD_ID` + the key for the foreman:

```bash
# Backend (.env or shell):
PIONEER_FOREMAN_KEY=<your-secret>

# Foreman (docker compose):
GUILD_ID=abc123 PIONEER_FOREMAN_KEY=<your-secret> \
  docker compose --profile foreman up --build foreman
```

### GitHub OAuth App

Pioneer Square uses GitHub OAuth instead of personal access tokens. You need to create a GitHub OAuth App and set the credentials as environment variables before starting the backend.

1. Go to **GitHub → Settings → Developer settings → OAuth Apps → New OAuth App**
   (direct link: https://github.com/settings/applications/new)
2. Fill in:
   - **Application name**: Pioneer Square (or anything you like)
   - **Homepage URL**: `http://localhost:5173`
   - **Authorization callback URL**: `http://localhost:8000/auth/github/callback`
3. Click **Register application**, then generate a **Client secret**.

Set the credentials before running the backend:

```bash
export GITHUB_CLIENT_ID=your_client_id_here
export GITHUB_CLIENT_SECRET=your_client_secret_here
# Optional overrides (defaults shown):
# export GITHUB_REDIRECT_URI=http://localhost:8000/auth/github/callback
# export FRONTEND_URL=http://localhost:5173
```

Or put them in `backend/.env`:

```
GITHUB_CLIENT_ID=your_client_id_here
GITHUB_CLIENT_SECRET=your_client_secret_here
```

### Install the CLI

All three Python runtimes (HTTP server, foreman, worker) install from a single
package and are launched through one `pioneer` command:

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

Open http://localhost:5173 — a new session will be created automatically.

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

The backend-owned foreman loop remains responsible for state, history, tools, and task
coordination. The standalone process is only an optional LLM API proxy for calling Anthropic,
Bedrock, or an OpenAI-compatible endpoint from a different network environment.

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

Pioneer Square can post event notifications, per-PR/issue discussion threads, a live Foreman
chat mirror, and `/ps` slash commands into a Discord server. It's entirely opt-in — with no
`DISCORD_*` env vars set, nothing changes. See [`docs/discord.md`](docs/discord.md) for setup.

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

- **Backend**: FastAPI + asyncpg + WebSocket signaling
- **Frontend**: Vue 3 + Pinia + Vue Router + WebRTC
- **Worker**: Standalone Python process (`pioneer-worker` CLI) that runs
  Claude on assigned tasks and opens GitHub PRs.
- **Database**: PostgreSQL (guilds, agents, messages, workers, tasks)

## Connecting Agents

Agents connect via WebSocket at `ws://localhost:8000/ws/{sessionId}` and send:

```json
{ "type": "join", "agentId": "agent-1", "agentName": "Builder", "agentType": "worker" }
{ "type": "agent-state", "agentId": "agent-1", "state": "working" }
{ "type": "terminal-output", "agentId": "agent-1", "line": "Building project..." }
```

States: `idle` | `thinking` | `working` | `busy` | `error`

## A2A AgentCard

Pioneer Square implements the [A2A (Agent-to-Agent) protocol](https://google.github.io/A2A/)
AgentCard discovery document. Each guild exposes its identity at:

```
GET /.well-known/agent.json        # subdomain-routed (e.g. myguild.pioneer-square.melloy.life)
GET /guilds/{guild_id}/agent-card  # direct REST (requires auth)
```

The card describes the guild's Foreman AI and lists all currently-online workers
as skills. Example response:

```json
{
  "name": "My Factory",
  "description": "A real-time multi-agent workspace...",
  "url": "https://myguild.pioneer-square.melloy.life",
  "version": "1.0.0",
  "capabilities": { "streaming": true },
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [
    { "id": "foreman", "name": "Foreman", "tags": ["orchestration", "planning", "review"], ... },
    { "id": "w-abc123", "name": "Worker (my-org/my-repo)", "tags": ["coding", "github"], ... }
  ],
  "provider": { "organization": "Pioneer Square", "url": "https://github.com/jmelloy/pioneer-square" }
}
```

### Customising the card

Update a guild's AgentCard fields via `PATCH /guilds/{id}`:

```bash
curl -X PATCH http://localhost:8000/guilds/{guild_id} \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"description": "My factory description", "url": "https://my-instance.example.com", "version": "1.0.0"}'
```

All three fields (`description`, `url`, `version`) are optional — the endpoint
falls back to sensible defaults when they are unset.
