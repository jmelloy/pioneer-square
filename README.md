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

Backend: http://localhost:8000 — Frontend: http://localhost:5173.
SQLite is persisted in the `backend-data` volume.

The worker is opt-in (it needs a `pioneer-worker.toml`):

```bash
cp worker/pioneer-worker.toml.example worker/pioneer-worker.toml
# edit guild_id, repos. Use backend_url = "http://backend:8000"
docker compose --profile worker up --build worker
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

### Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
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
cd worker
python -m venv venv
source venv/bin/activate
pip install -e .
cp pioneer-worker.toml.example pioneer-worker.toml
# edit pioneer-worker.toml: backend_url, session_id, repos
# github_token is optional — if omitted, the worker fetches the OAuth token
# stored in the backend DB (set after the user connects via GitHub OAuth)
pioneer-worker
```

See [`worker/README.md`](worker/README.md) for details.

## Architecture

- **Backend**: FastAPI + aiosqlite + WebSocket signaling
- **Frontend**: Vue 3 + Pinia + Vue Router + WebRTC
- **Worker**: Standalone Python process (`pioneer-worker` CLI) that runs
  Claude on assigned tasks and opens GitHub PRs.
- **Database**: SQLite (sessions, agents, messages, workers, tasks)

## Connecting Agents

Agents connect via WebSocket at `ws://localhost:8000/ws/{sessionId}` and send:

```json
{ "type": "join", "agentId": "agent-1", "agentName": "Builder", "agentType": "worker" }
{ "type": "agent-state", "agentId": "agent-1", "state": "working" }
{ "type": "terminal-output", "agentId": "agent-1", "line": "Building project..." }
```

States: `idle` | `thinking` | `working` | `busy` | `error`
