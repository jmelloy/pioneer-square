# Plan: Split Foreman into a Standalone Service

**Status:** Draft — open for discussion before implementation  
**Date:** 2026-05-15  
**Branch:** `claude/plan-split-foreman-into-standalone-service-t-ysim`

---

## 1. Current Architecture: How the Foreman Is Embedded Today

### Where it lives

The Foreman AI lives entirely inside the backend process:

```
backend/
  foreman/
    runner.py   – Claude API loop, conversation history, poll loop
    tools.py    – 19 tool definitions + executor (DB access + WS dispatch)
    prompt.py   – System prompt + state preamble builders
    state.py    – Empty stub (history is now DB-backed)
  main.py       – Trigger points: WS handlers call run_foreman_ai()
```

### How it is triggered

The foreman is invoked as `asyncio.create_task(run_foreman_ai(...))` from five places inside `main.py`/WS handlers:

| Trigger | Source | Spawned as |
|---|---|---|
| Human chat message | WS chat handler | `foreman.chat:{guild_id}` |
| `task-complete` from worker | WS message handler | `foreman.task-complete:{task_id}` |
| `task-followup-done` from worker | WS message handler | `foreman.followup-done:{task_id}` |
| `needs-input` worker escalation | WS message handler | (inline) |
| GitHub webhook arrival | HTTP webhook route | (inline async task) |
| Periodic background poll | `_poll_loop()` inside `runner.py` | `foreman.poll:{guild_id}` |

### What the Foreman accesses directly

**Direct DB reads (aiosqlite/SQLAlchemy, same connection pool as the backend):**
- `ForemanTurn` — conversation history (read + write)
- `Task` — all non-terminal tasks (read for prompt; write via tool executor)
- `Worker` — online workers for the state preamble (read)
- `Agent` — agent state for idle-worker selection (read + write)
- `Guild` — primary_repo hint (read)
- `Message` — final chat response persistence (write)
- `GithubToken` — access token + username for GitHub tool calls (read)
- `GuildKey` — private key for dnsid signing (read)
- `TaskLog` — recent task logs for `get_task_status` (read)

**Direct WS broadcast:**  
`tools.py` calls `broadcast(guild_id, {...})` directly (in-process function over the backend's in-memory WS connection table). This pushes task dispatch messages (`task-assigned`, `task-followup`, `task-finalize`, `task-redirect`, `terminal-output`) to worker WebSocket connections.

### What is shared with the Worker today

Nothing at the Python level — the worker is an entirely separate package (`worker/`). The coupling is only through the backend's DB and WebSocket hub. The key difference is:

- **Workers** have zero direct DB access; they communicate exclusively over WebSocket + a small number of REST endpoints.
- **The Foreman** has full DB access and in-process WS broadcast; it is not a WebSocket client at all.

---

## 2. Proposed Separation: Foreman as a Standalone Process

### Design principle

Model the standalone Foreman after the Worker: it is a process that **registers with the backend via HTTP, then drives all state changes through REST + WebSocket messages**, never touching the database directly.

### New package: `foreman/`

```
foreman/
  pioneer_foreman/
    __init__.py
    __main__.py         – delegates to cli.main()
    cli.py              – argument parsing + Config build + Foreman().run()
    config.py           – Config dataclass (mirrors worker/config.py style)
    foreman.py          – Main class: WS connection, event dispatch, poll loop
    runner.py           – Claude API loop (ported from backend/foreman/runner.py)
    tools.py            – Tool definitions + HTTP-backed executor
    prompt.py           – System prompt builders (no changes needed)
  pyproject.toml        – entry_point: pioneer-foreman = pioneer_foreman.cli:main
  pioneer-foreman.toml.example
```

### Process lifecycle

```
1. pioneer-foreman starts
2. HTTP POST /guilds/{guild_id}/foreman/register
     → receives foreman_id (e.g. "frm-<6chars>") + auth_token
3. WebSocket connect to ws://{backend_url}/ws/{guild_id}
     → sends join { agentType: "foreman", foremanId, … }
4. Background: starts _poll_loop() (mirrors current runner.py behavior)
5. Listen loop: receives trigger events from backend over WS
6. On trigger: calls run_foreman_ai() → Claude API → tool calls → REST/WS back to backend
7. On SIGTERM/SIGINT: sends foreman-disconnect WS message, exits cleanly
```

Only **one foreman process per guild** should be active at a time. If a second tries to register, the backend should reject it (or the first one is considered superseded — TBD; see §6).

---

## 3. Communication Layer

### Recommendation: WebSocket (events in) + REST (state reads and mutations)

This is already the worker's model and requires no new infrastructure. The key additions are:

1. **New WS message types** (backend → foreman) that carry trigger events.
2. **New REST endpoints** on the backend so the foreman can read state and write results without direct DB access.

#### 3a. WebSocket: events delivered to the standalone Foreman

The backend stops calling `run_foreman_ai()` directly. Instead it sends a new WS message type to the connected foreman agent:

| Type | Direction | Payload | Purpose |
|---|---|---|---|
| `foreman-trigger` | backend → foreman | `{ event, guild_id, task_id?, user_id?, extra_context }` | Replaces all `asyncio.create_task(run_foreman_ai(...))` calls |
| `foreman-registered` | backend → foreman | `{ foreman_id, auth_token }` | Response to `join` |
| `foreman-disconnect` | foreman → backend | `{ foreman_id }` | Graceful shutdown |

The `event` field on `foreman-trigger` mirrors the current trigger types: `chat`, `task-complete`, `followup-done`, `needs-input`, `github-event`, `periodic-check`.

The standalone foreman also continues to **send** the existing WS messages that workers already understand (`task-assigned`, `task-followup`, `task-finalize`, `task-redirect`, `terminal-output`). The backend forwards these to the relevant worker connections — no change to the worker protocol.

#### 3b. New REST endpoints (backend additions)

All endpoints require `Authorization: Bearer <auth_token>` (same mechanism as workers).

**State reads (replaces direct DB queries in `runner.py` and `tools.py`):**

```
GET  /guilds/{guild_id}/foreman/state
     → { workers: [...], tasks: [...], guild: { primary_repo, ... } }
     Used by: build_state_preamble(), _poll_loop()

GET  /guilds/{guild_id}/foreman/history?user_id=&limit=
     → [ { role, content_json, is_tool_response, parent_id, created_at }, … ]
     Used by: _load_history()

GET  /guilds/{guild_id}/github/token          (already exists for workers)
     → { access_token, github_username }
     Used by: GitHub tools in tools.py

GET  /guilds/{guild_id}/guild-key             (new, if dnsid tool is kept)
     → { private_key_pem }
```

**State writes (replaces direct DB writes in `tools.py`):**

```
POST   /guilds/{guild_id}/foreman/history
       body: { role, content_json, is_tool_response, parent_id? }
       Used by: _save_turn()

POST   /guilds/{guild_id}/tasks
       body: { description, repo?, priority? }
       → { task_id }
       Used by: create_task tool (may already exist)

PATCH  /guilds/{guild_id}/tasks/{task_id}
       body: { state?, worker_id?, branch?, pr_url?, finished_at? }
       Used by: assign_task, finalize_task, cancel_task, send_followup, redirect_task tools

POST   /guilds/{guild_id}/messages
       body: { role, content, user_id? }
       Used by: run_foreman_ai() final chat response persistence

GET    /guilds/{guild_id}/tasks/{task_id}/logs?limit=
       → [ { line, timestamp }, … ]
       Used by: get_task_status tool
```

**Worker control (replaces in-process `broadcast()` calls in `tools.py`):**

The foreman sends WS messages directly to the backend WS hub from its own connected socket. The backend's WS hub already fans out to all connected agents in the guild — so `task-assigned`, `task-followup`, `task-finalize`, and `task-redirect` messages sent by the foreman's WS client will be received by workers just as today. No new REST endpoints needed for this path.

`message_worker` (terminal-output injection) and `shutdown_worker` similarly become outbound WS messages from the foreman client.

---

## 4. Configuration

### Config file: `pioneer-foreman.toml`

```toml
backend_url = "ws://localhost:8000"    # same base as workers
guild_id    = "abc123"

[claude]
model       = "claude-sonnet-4-6"      # or any Anthropic model ID
api_key     = ""                       # falls back to ANTHROPIC_API_KEY env var
max_rounds  = 10
history_limit = 40

[poll]
min_interval = 60     # seconds (exponential backoff base)
max_interval = 3600
```

### Environment variables (override config file)

| Variable | Purpose |
|---|---|
| `PIONEER_BACKEND_URL` | Backend WebSocket/HTTP base URL |
| `PIONEER_GUILD_ID` | Guild to serve |
| `ANTHROPIC_API_KEY` | Claude API key for foreman reasoning |
| `FOREMAN_MODEL` | Override Claude model (e.g. `claude-opus-4-7`) |
| `FOREMAN_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` |

### CLI flags

```
pioneer-foreman
  --config            Path to TOML file (default ./pioneer-foreman.toml)
  --backend-url       Override backend URL
  --guild-id          Override guild ID
  --model             Override Claude model
  --log-level         Log verbosity
```

This makes it straightforward to run the foreman on a different host with a different Claude model:

```bash
# Workers on a local GPU box with a small model
FOREMAN_MODEL=claude-haiku-4-5 pioneer-worker ...

# Foreman on a cloud box pointing at the same backend
FOREMAN_MODEL=claude-opus-4-7 pioneer-foreman \
  --backend-url wss://backend.example.com \
  --guild-id abc123
```

---

## 5. Migration Path

The migration is designed to be additive: the embedded foreman stays functional until Phase 4 is complete.

### Phase 1 — Backend: add foreman REST endpoints (1–2 days)

Add the new REST endpoints listed in §3b to `backend/main.py` (or a new `backend/routes/foreman.py`). These endpoints are gated by the same auth-token mechanism as workers and are no-ops until the standalone foreman connects.

No changes to the existing foreman code or worker protocol.

**Deliverable:** Backend can serve foreman state/history/mutation requests. Existing embedded foreman continues to function unchanged.

### Phase 2 — Backend: add `foreman-trigger` WS dispatch (1 day)

Refactor all `asyncio.create_task(run_foreman_ai(...))` call sites:
- If a foreman agent is currently connected → send `foreman-trigger` WS message.
- If no foreman connected → fall back to the embedded `run_foreman_ai()`.

This makes the backend foreman-agnostic without breaking anything.

**Deliverable:** Backend supports both embedded and standalone foreman simultaneously.

### Phase 3 — Build standalone Foreman package (3–5 days)

Create the `foreman/` package:
1. Copy `backend/foreman/runner.py`, `tools.py`, `prompt.py` into `foreman/pioneer_foreman/`.
2. Replace every direct DB call with the new REST endpoints.
3. Replace every `broadcast()` call with an outbound WS message.
4. Add `cli.py`, `config.py`, `foreman.py` (WS client + event dispatch + poll loop).
5. Add `pyproject.toml` with `pioneer-foreman` entry point.
6. Write tests (unit-test tool executor with a mock HTTP server; integration-test WS registration).

**Deliverable:** `pioneer-foreman` binary runnable in parallel with embedded foreman.

### Phase 4 — Testing and cut-over (1–2 days)

1. Run both embedded and standalone foreman against a test guild. Verify:
   - Tasks are assigned and finalized correctly.
   - Conversation history is persisted and loaded correctly.
   - GitHub tools work (token fetched via REST).
   - Poll loop triggers on schedule.
2. Confirm worker protocol is unchanged (no worker changes needed).
3. Switch the fallback in Phase 2 to **standalone-first** (no embedded fallback).

**Deliverable:** Standalone foreman is the production path.

### Phase 5 — Remove embedded Foreman from backend (1 day)

1. Delete `backend/foreman/` directory.
2. Remove foreman trigger call sites from `main.py` (replaced by `foreman-trigger` dispatch).
3. Remove `ForemanTurn` model (already served via new REST endpoints; table kept for history).
4. Update `CLAUDE.md` and deployment docs.

**Deliverable:** Backend no longer imports or runs the Foreman AI. Three independently deployable services: backend, worker(s), foreman.

---

## 6. Open Questions and Risks

### Q1 — Single foreman per guild or multi-guild?

The current embedded foreman handles all guilds (keyed by `guild_id`). The standalone version could either:
- **One process per guild** (simpler, mirrors how workers configure a single `guild_id`). Recommended.
- **One process for all guilds** (more complex: register once per guild, multiplex events).

_Recommendation:_ Start with one-per-guild. If the operator runs dozens of guilds, add a multi-guild mode later.

### Q2 — Preventing duplicate foremans

If two `pioneer-foreman` processes register for the same guild simultaneously, they will both receive `foreman-trigger` events and both act — potentially creating duplicate tasks or conflicting state mutations.

_Options:_
- Backend tracks the "active foreman" registration and rejects a second `join`.
- Backend sends `foreman-trigger` to **all** registered foremans (idempotent execution required).
- Last-register wins (new registration supersedes old).

_Recommendation:_ Backend enforces a single active foreman per guild (reject or evict). Emit a `foreman-evicted` WS message to the old one so it exits cleanly.

### Q3 — GitHub webhook routing

GitHub webhooks currently arrive at the backend HTTP server, which parses them and calls `run_foreman_ai()`. In the standalone world the backend needs to emit `foreman-trigger` events for webhooks. This is already handled by Phase 2 — no separate change needed.

### Q4 — Conversation history ownership

`ForemanTurn` currently lives in the backend's SQLite DB. The standalone foreman will write turns via REST. If the foreman is replaced or restarted mid-conversation, history is preserved in the DB and the new process picks it up.

_Risk:_ The in-flight Claude API call (mid-conversation) cannot be resumed across restarts. Tasks that were in a `working` state when the foreman crashed will be re-queued by the next `periodic-check`. This is the same behaviour as today.

### Q5 — Auth token scope

Workers currently receive an `auth_token` that scopes to `/auth/github/token` and `/auth/claude/credentials`. The foreman needs a broader token (access to history, task mutations, etc.). Either:
- Same token mechanism, wider route allowlist for `agent_type=foreman`.
- A separate secret in `pioneer-foreman.toml` (`foreman_secret`).

_Recommendation:_ Reuse the same token mechanism with a wider allowlist. The backend can check `agent_type` on the token to decide which routes are accessible.

### Q6 — `_poll_loop` ownership during migration

Currently `_poll_loop` is started once per guild at backend startup (`reset_foreman_poll`). During Phase 2–3, both the backend poll loop and the standalone foreman's poll loop could fire concurrently.

_Fix:_ In Phase 2, when a standalone foreman is connected, the backend suppresses its own poll loop for that guild. The standalone foreman owns the poll entirely.

### Q7 — Latency

Today the foreman is in-process with the backend — no network hops for DB reads. The standalone foreman will add REST round-trips for each tool call. Typical tool calls make 1–3 DB queries; at 10 ms per REST round-trip over localhost this is negligible. On a remote host (across a datacenter) it may add 100–500 ms per tool execution, which is acceptable given foreman runs are already multi-second LLM calls.

### Q8 — LLM provider swap

Because the standalone foreman owns its own `ANTHROPIC_API_KEY` and `FOREMAN_MODEL` config, swapping the provider is straightforward for models on the Anthropic API. Supporting a different provider entirely (e.g. OpenAI, local Ollama) would require making `runner.py` provider-agnostic (currently uses `anthropic.AsyncAnthropic` and the Anthropic tool-use schema). That is out of scope for this plan but the standalone structure makes it tractable as a follow-on.

---

## Appendix: File-by-file change summary

| File | Change |
|---|---|
| `backend/main.py` | Add `foreman-trigger` dispatch; fallback to embedded if no foreman connected (Phase 2); remove fallback (Phase 5) |
| `backend/routes/foreman.py` | New file: all foreman REST endpoints (Phase 1) |
| `backend/foreman/` | No changes in Phase 1–3; deleted in Phase 5 |
| `backend/models.py` | Add `ForemanRegistration` or reuse `Agent` with `agent_type=foreman` |
| `foreman/` | New top-level package (Phase 3) |
| `worker/` | **No changes** — worker protocol is unchanged throughout |
| `frontend/` | **No changes** — frontend talks to backend only |
| `CLAUDE.md` | Update "Three-process model" diagram and commands (Phase 5) |
| `docker-compose.yml` (if added) | Add `foreman` service alongside `backend` and `worker` |
