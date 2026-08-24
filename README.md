# Pioneer Square

A real-time multi-agent workspace where a Foreman AI coordinates worker processes that clone
repos, run coding agents, and open GitHub PRs — presented as a pixel-art steampunk factory floor.

![Factory floor](screenshots/factory-floor.png)

## Screenshots

| Factory | Sidebar | Foreman chat |
| --- | --- | --- |
| ![Factory floor closeup](screenshots/agents-closeup.png) | ![Task and worker sidebar](screenshots/sidebar.png) | ![Foreman chat pane](screenshots/chat-pane.png) |

## Features

- 🏭 Pixel-art factory workspace with live worker avatars and state animations
- 🤖 Worker slots backed by real processes; workers can run `claude`, `codex`, or `pi`
- 🧠 Foreman AI that plans work, assigns tasks, reviews completions, and requests follow-ups
- 🌳 GitHub issue/PR-oriented task tree with plan / execute / review / follow-up phases
- 💬 Real-time Foreman chat, conversation threads, task logs, and per-agent log tabs
- 🔌 WebSocket protocol for workers, browser clients, and optional external Foreman API proxy
- 🔐 GitHub OAuth login; workers can use stored OAuth tokens or configured tokens to push PRs
- 🐳 Docker Compose quickstart with PostgreSQL, backend + built SPA, optional worker/foreman/tools profiles
- 🔔 Optional Discord notifications, Foreman chat mirror, and `/ps` commands

## Quickstart with Docker Compose

```bash
cp .env.example .env
# Fill at least POSTGRES_PASSWORD, GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, ANTHROPIC_API_KEY
docker compose up --build
```

Open http://localhost:8056.

`docker compose up` starts PostgreSQL, waits for its health check, runs `alembic upgrade head`, and
then serves the FastAPI backend plus the built Vue SPA. PostgreSQL data lives in the
`postgres-data` volume.

### Optional Docker profiles

Worker container (requires `worker/pioneer-worker.toml`):

```bash
cp worker/pioneer-worker.toml.example worker/pioneer-worker.toml
# edit guild_id and repos; for Docker use backend_url = "http://backend:8000"
docker compose --profile worker up --build worker
```

Standalone Foreman API proxy:

```bash
GUILD_ID=<your-6-char-guild-id> docker compose --profile foreman up --build foreman
```

Metabase/PostgreSQL inspection tools:

```bash
METABASE_DB_PASSWORD=<password> docker compose --profile tools up pgweb
```

## GitHub OAuth App

Create an OAuth App at **GitHub → Settings → Developer settings → OAuth Apps → New OAuth App**:

- **Homepage URL**: `http://localhost:8056`
- **Authorization callback URL**: `http://localhost:8056/auth/github/callback`

Set these in `.env`:

```dotenv
GITHUB_CLIENT_ID=your_client_id_here
GITHUB_CLIENT_SECRET=your_client_secret_here
```

For local source development with Vite, run the backend on `8000` and frontend on `5173`; the
backend defaults are:

```dotenv
GITHUB_REDIRECT_URI=http://localhost:5173/
FRONTEND_URL=http://localhost:5173
```

## Local development

Install the unified CLI package once; it provides all Python runtimes:

```bash
uv venv
source .venv/bin/activate
uv pip install -e "cli[test]"
```

Backend:

```bash
# DATABASE_URL must point at PostgreSQL; Docker Compose's postgres service is fine.
pioneer serve --port 8000 --reload
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Worker:

```bash
cp worker/pioneer-worker.toml.example worker/pioneer-worker.toml
# edit backend_url, guild_id, [github] repos/org, and optional token
pioneer worker --config worker/pioneer-worker.toml --log-level DEBUG
```

Standalone Foreman API proxy:

```bash
cp foreman-proxy/pioneer-foreman.toml.example foreman-proxy/pioneer-foreman.toml
# edit backend_url, guild_id, and [llm] provider/model credentials
pioneer foreman --config foreman-proxy/pioneer-foreman.toml
```

Environment-only example for an OpenAI-compatible local endpoint:

```bash
PIONEER_BACKEND_URL=ws://localhost:8000 \
PIONEER_GUILD_ID=<your-6-char-guild-id> \
FOREMAN_PROVIDER=openai \
FOREMAN_MODEL=llama3.1 \
FOREMAN_BASE_URL=http://localhost:11434/v1 \
pioneer foreman
```

The external Foreman process is only an LLM API proxy. The backend still owns trigger handling,
conversation history, tool execution, task state, and polling. If the proxy disconnects, the backend
falls back to its direct provider configuration.

## Testing and formatting

```bash
ruff check . --fix && ruff format .
cd backend && python -m pytest
cd worker && python -m pytest
cd frontend && npm test && npm run type-check
```

Backend and worker tests expect a PostgreSQL test database; the compose file includes one:

```bash
docker compose --profile test up -d postgres-test
```

## Migrating from SQLite

Older installs used `pioneer_square.db`. To migrate it into PostgreSQL:

```bash
cd backend && alembic upgrade head && cd ..
pip install psycopg2-binary
python scripts/migrate_sqlite_to_postgres.py \
  --sqlite-path /path/to/pioneer_square.db \
  --postgres-url postgresql://pioneer:pioneer_password@localhost/pioneer_square
```

The script is idempotent; rows that already exist are skipped.

## Architecture

Pioneer Square normally runs three independent processes:

```text
Browser ──WebSocket/REST──► Backend (FastAPI + PostgreSQL)
Worker  ──WebSocket/REST──► Backend
```

The optional fourth process, `pioneer foreman`, connects as an external Foreman API proxy. See
[AGENTS.md](AGENTS.md) for the full reference: terminology, task lifecycle, WebSocket protocol,
database schema, worker internals, frontend stores, and model-provider notes.

## Discord integration (optional)

Pioneer Square can post event notifications, per-PR/issue discussion threads, a live Foreman chat
mirror, and `/ps` slash commands into a Discord server. With no `DISCORD_*` environment variables
set, it is disabled. See [`docs/discord.md`](docs/discord.md).

## A2A AgentCard

Each guild exposes an A2A AgentCard:

```text
GET /.well-known/agent.json        # subdomain routed
GET /guilds/{guild_id}/agent-card  # direct REST, requires auth
```

The card describes the guild Foreman and currently-online workers as skills. Guild-level fields are
editable with `PATCH /guilds/{guild_id}`.
