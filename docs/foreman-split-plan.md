# Foreman as a Standalone Service

**Status:** Phases 1–4 shipped. Phase 5 (remove embedded foreman) not yet done.
**Original plan date:** 2026-05-15

---

## Architecture

The Foreman AI runs as a standalone process that connects to the backend over
WebSocket and REST, with no direct database access — mirroring how workers
operate.

```
browser
  │  (chat / github webhook)
  ▼
backend ──── WS: foreman-trigger ────► standalone foreman process
  │                                     (pioneer foreman)
  │◄──────── WS: foreman-broadcast ───┘
  │                                     ↑ REST: state reads + tool exec
  └── REST: /guilds/{id}/foreman/…  ───┘
```

When a standalone foreman is connected, the backend sends `foreman-trigger`
WS messages and the foreman executes the AI turn, calling back via REST.
When no standalone foreman is connected, the backend falls back to the
embedded foreman (`backend/foreman/`).

---

## Packages and files

### Standalone foreman — `foreman/`

```
foreman/
  pioneer_foreman/
    cli.py           – argument parser + entry point
    config.py        – Config dataclass; reads TOML + env vars + CLI overrides
    foreman.py       – Foreman class: WS lifecycle, trigger dispatch, poll loop
    runner.py        – Claude API loop (thin wrapper around foreman_core)
    tools.py         – tool definitions; all execution delegated to /exec_tool
    http_client.py   – ForemanHTTPClient: typed methods for every backend endpoint
    jwt_auth.py      – JWTTokenManager: mints short-lived HS256 tokens from backend_key
    logging_config.py
  tests/
    test_config.py
    test_jwt_auth.py
    test_tools.py
    test_ws_integration.py
```

### Shared logic — `backend/foreman_core/`

Shared between the embedded foreman (`backend/foreman/`) and the standalone
process. Neither should duplicate LLM-call logic or constants.

```
backend/foreman_core/
  llm.py           – get_foreman_client(), get_foreman_model(), stream helpers
  prompt.py        – system prompt builders
  tools_schema.py  – canonical FOREMAN_TOOLS list (Anthropic tool-use dicts)
  message_utils.py – content-block helpers
  constants.py     – shared literals
```

### Backend additions — `backend/routes/foreman.py`

All new REST endpoints for the standalone foreman. Auth: JWT (preferred) or
static worker/member token.

**State reads:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/guilds/{id}/foreman/state` | Online workers, active tasks, guild metadata |
| `GET` | `/guilds/{id}/foreman/history` | Raw ForemanTurn rows for a user |
| `GET` | `/guilds/{id}/guild-key` | Ed25519 signing key for dnsid tool |
| `GET` | `/guilds/{id}/foreman/env-vars` | Guild-configured env vars (API keys, etc.) |

**State writes:**

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/guilds/{id}/foreman/history` | Persist one ForemanTurn |
| `PATCH` | `/guilds/{id}/foreman/turns/{turn_id}/tokens` | Update token counts |
| `POST` | `/guilds/{id}/tasks` | Create a task |
| `PATCH` | `/guilds/{id}/tasks/{task_id}` | Update task fields |
| `POST` | `/guilds/{id}/messages` | Persist a chat message |

**Tool execution:**

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/guilds/{id}/foreman/exec_tool` | Execute one tool call via backend |

All tool business logic (worker selection, DB writes, WS broadcasts) lives
in `backend/foreman/tools.py:_exec_one_tool()`. The standalone foreman POSTs
tool calls to `/exec_tool` rather than maintaining its own tool executor.

---

## WebSocket protocol additions

**Backend → foreman:**

| Type | Payload | Purpose |
|------|---------|---------|
| `foreman-trigger` | `{ guildId, humanMessage, event, userId?, taskId?, extraContext? }` | Trigger one foreman AI turn |
| `foreman-registered` | `{ agentId }` | Acknowledgement on join |
| `foreman-evicted` | `{ reason }` | Another foreman registered; this one should reconnect later |

**Foreman → backend:**

| Type | Payload | Purpose |
|------|---------|---------|
| `join` | `{ agentType: "foreman", external: true }` | Register as external foreman |
| `foreman-broadcast` | `{ guildId, payload: { type, ... } }` | Send any WS message to guild (task-assigned, chat, etc.) |

The `foreman-broadcast` wrapper is how the standalone foreman dispatches
`task-assigned`, `task-followup`, `task-finalize`, `terminal-output`, etc.
to workers — the backend fans these out to the relevant WebSocket connections.

---

## Configuration

**TOML file (`pioneer-foreman.toml`):**

```toml
backend_url = "ws://backend:8000"   # derives HTTP URL automatically
guild_id    = "abc123"
backend_key = ""                    # matches PIONEER_FOREMAN_KEY on the backend

[claude]
model    = "claude-sonnet-4-6"      # direct Anthropic API
provider = "anthropic"              # "anthropic" | "bedrock"
# Bedrock:
# provider       = "bedrock"
# bedrock_model  = "arn:aws:bedrock:..."
# aws_region     = "us-east-1"

[poll]
min_interval = 60      # seconds (doubles each idle tick)
max_interval = 14400   # cap (~4 hours)
```

**Environment variables (override TOML):**

| Variable | Purpose |
|----------|---------|
| `PIONEER_FOREMAN_KEY` | HMAC secret for JWT auth (required; same value on backend) |
| `PIONEER_BACKEND_URL` or `BACKEND_WS_URL` | Backend WebSocket URL |
| `PIONEER_GUILD_ID` or `GUILD_ID` | Guild to serve |
| `ANTHROPIC_API_KEY` | API key for direct Anthropic |
| `FOREMAN_MODEL` | Override Claude model |
| `FOREMAN_PROVIDER` | `anthropic` or `bedrock` |
| `FOREMAN_BEDROCK_MODEL` | Cross-region inference profile ARN (Bedrock only) |
| `AWS_DEFAULT_REGION`, `AWS_BEARER_TOKEN_BEDROCK`, etc. | Bedrock credentials |
| `LOG_LEVEL` | Log verbosity |

**CLI flags** (override env vars):

```
pioneer foreman [--config PATH] [--backend-url URL] [--guild-id ID]
                [--model MODEL] [--backend-key SECRET] [--log-level LEVEL]
```

---

## Key design decisions (vs. original plan)

### Tool execution via `/exec_tool`

The original plan proposed individual REST endpoints per tool action and a
full tool executor in the standalone foreman. What shipped instead: a single
`POST /exec_tool` endpoint that calls `backend/foreman/tools.py:_exec_one_tool()`
directly. All tool logic stays in one place in the backend; the standalone
foreman is a thin dispatcher.

### JWT auth

The original plan floated a static token. What shipped: `backend_key` (a
shared HMAC secret matching `PIONEER_FOREMAN_KEY`) generates short-lived
HS256 JWTs automatically via `JWTTokenManager`. A static token fallback
(`auth_token`) exists but JWT is the expected path.

### Message queue

Triggers that arrive while a foreman run is in progress are buffered in a
bounded asyncio queue (max 100). After each run completes, the queue is
drained in FIFO order before releasing the `_processing` lock.
`[periodic-check]` triggers are silently dropped when busy.

### Single-foreman enforcement

The backend tracks one active external foreman per guild. A second `join`
evicts the first via `foreman-evicted`. The evicted process waits and
reconnects.

### Poll backoff

Poll interval starts at `poll_min_interval` (default 60 s) and doubles each
idle tick up to `poll_max_interval` (default ~4 hours). Any run that makes
at least one tool call resets the interval to `poll_min_interval`.

---

## What's not done yet (Phase 5)

The embedded foreman (`backend/foreman/`) is still present and still the
fallback when no external foreman is connected. Phase 5 — deleting the
embedded foreman and making the standalone the only path — has not been done.

Before Phase 5:
- All webhook triggers (`backend/routes/webhooks.py`) currently call
  `run_foreman_ai()` directly as the embedded-foreman fallback; these would
  need to go through the `foreman-trigger` dispatch path exclusively.
- `backend/foreman/` can be deleted once the embedded fallback is removed
  from `ws_handlers.py` and `webhooks.py`.
- `ForemanTurn` model and its table stay (history is still stored there).
- `AGENTS.md` diagrams should be updated to reflect the three-process model
  as the only path.
