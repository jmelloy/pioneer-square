# Pioneer Square — Architecture Specification

This document reverse-engineers Pioneer Square's runtime architecture from the current codebase
(as of commit `c79b569`, 2026-07-18). It is a deeper, code-level companion to `AGENTS.md` —
read `AGENTS.md` first for terminology and quickstart commands; this document goes further into
data flow, control flow, and integration internals.

## 1. System Overview

Pioneer Square is a real-time, multi-agent coding-automation platform. A **Foreman AI** (an
LLM-driven orchestrator embedded in the backend) creates and assigns **tasks** to autonomous
**worker** processes. Each worker clones a repo into a git worktree, runs a coding agent
(Claude Code, Codex, or `pi`) against the task description, and opens a GitHub PR. The Foreman
reacts to task completion, GitHub webhook events, and human chat to decide whether to iterate
(`send_followup`), close out (`finalize_task`), or redirect the work.

```
                         ┌───────────────────────────────────────────┐
                         │              Backend (FastAPI)             │
  Browser ──WebSocket──► │  ws_handlers.py ─┬─► Foreman AI (foreman/) │
  (Vue 3 + Pinia)  ◄──── │                  │      runner/tools/llm   │◄── GitHub Webhooks
                  REST   │                  ├─► Postgres/SQLite DB    │       (routes/webhooks.py)
                         │                  │      (SQLModel+Alembic) │
  Worker(s) ──WebSocket──┤                  └─► Discord notifier      │──► Discord (optional)
  (git worktrees +       │                                             │
   claude/codex/pi) ◄────┴─────────────────────────────────────────────┘
                                       ▲
                                       │ optional: foreman-api-request/response
                         ┌─────────────┴─────────────┐
                         │  Standalone Foreman Proxy   │
                         │  (foreman-proxy/, opt-in)   │──► External LLM (Anthropic/Bedrock/OpenAI-compat)
                         └─────────────────────────────┘
```

Four processes, three required:

1. **Backend** (`backend/`) — FastAPI app; owns the database, WebSocket hub, REST API, the
   embedded Foreman AI loop, and GitHub webhook ingestion.
2. **Frontend** (`frontend/`) — Vue 3 + Pinia + Vite SPA; renders the factory-floor visualization,
   task tree, chat, and guild configuration UI. Talks to the backend via WebSocket + REST.
3. **Worker** (`worker/pioneer_worker/`) — standalone Python process; registers with the backend,
   receives task assignments, manages git worktrees, and spawns coding-agent subprocesses.
4. **Standalone Foreman proxy** (`foreman-proxy/`, opt-in) — a thin external process that lets the
   LLM call itself run outside the backend's network/credentials boundary (e.g. a different
   region, a local Ollama endpoint). The backend still runs all Foreman *logic*; the proxy only
   executes the raw LLM API call when connected.

**Note on the database**: `AGENTS.md` describes SQLite as the persistence layer (true for the
lightweight local/dev path and `docker-compose` quickstart script), but `backend/database.py`
reads `DATABASE_URL` from the environment with no fallback, and the primary `docker-compose.yml`
service wires up Postgres 18 (`postgresql+asyncpg://...`) with `pg_stat_statements`/`auto_explain`
enabled — Postgres is the production target; SQLite is a lighter-weight alternative for local
dev/tests, both migrated by the same Alembic revision chain.

## 2. Component Breakdown

### 2.1 Backend (`backend/`)

- `main.py` — thin (~570 line) FastAPI wiring module: mounts routers from `backend/routes/`,
  runs `init_db()` (`alembic upgrade head`) and `reset_connection_state()` on startup (all workers
  and agents flipped to `offline`), starts the worker-liveness sweeper
  (`WORKER_OFFLINE_AFTER_SECONDS=90`, probe timeout 10s, sweep interval 30s), and starts
  `worker_lifecycle.drain_stale_workers_on_startup()` for rolling-deploy safety.
- `routes/` — REST endpoints: `tasks.py`, `workers.py`, `agents.py`, `guilds.py`, `webhooks.py`,
  `foreman.py`, `discord.py`, `discord_connect.py`, `discord_users.py`, `auth.py`, `cost.py`,
  `usage.py`, `issues.py`, `models.py` (model catalog), `websocket.py`, `push.py`, `debug.py`,
  `debug_query.py`, `wellknown.py`.
- `ws_handlers.py` / `ws_types.py` — all WebSocket message handling; this is also where every
  Foreman AI trigger point lives (`_trigger_foreman`, task-complete/followup-done/needs-input
  handlers).
- `foreman/` — the Foreman AI package (§3).
- `models.py` — SQLModel ORM, single source of truth for the DB schema (§5).
- `alembic/versions/` — migration history.
- `worker_lifecycle.py` — server-side worker registration/drain/reconnect state machine (§4.1).
- `discord_notifier.py` — Discord bot/webhook client used by the Foreman for notifications.
- `oauth.py` — GitHub OAuth helpers.
- `util/` — shared helpers: `tasks.py` (`spawn()`, a tracked `asyncio.create_task` wrapper),
  `model_tiers.py` (tier→model resolution, §6.3), `api_latency.py` (per-call observability).

### 2.2 Frontend (`frontend/src/`)

Vite 8 + Vue 3.5 (Composition API / `<script setup>`) + Pinia 3 + strict TypeScript 6 + Vue
Router 5. See §7 for the full breakdown.

### 2.3 Worker (`worker/pioneer_worker/`)

Standalone async Python process. See §4 for internals.

### 2.4 Standalone Foreman proxy (`foreman-proxy/pioneer_foreman/`)

Opt-in external LLM-call executor. See §6.4.

### 2.5 CLI (`cli/`)

Unified `pioneer` launcher (`cli/pioneer_cli/`) with three modes — `pioneer serve`, `pioneer
worker`, `pioneer foreman` — all installed from one `cli/pyproject.toml`. It puts the right
existing source tree (`backend/`, `worker/`, `foreman-proxy/`) on `sys.path` without moving code
or rewriting imports; repo root is resolved from `cli/`'s parent (overridable via `PIONEER_ROOT`).

## 3. Foreman AI (`backend/foreman/`)

### 3.1 Triggers and entry point

Every Foreman invocation funnels through `ws_handlers._trigger_foreman`, which `spawn()`s
`runner.run_foreman_ai(event, ...)` in the background. Trigger events:

| Event | Origin |
|---|---|
| `chat` | Human message addressed to `foreman`, or a `[github-event]`/`[discord-thread-reply]` synthetic message |
| `task-complete` | Worker finished a task (`ws_handlers.handle_task_complete`) |
| `followup-done` | Worker finished a follow-up run |
| `needs-input` | Worker's coding agent stopped and needs human/foreman input |
| `task-error` | Worker reported a crash |
| `worker-online` / `worker-offline` | Worker connect/disconnect |
| `claude-auth` | A worker's Claude auth flow completed |
| `periodic-check` | Self-rescheduling backoff poll (`runner._poll_loop`), 60s → 4h, refreshes PR/issue status proactively |

`foreman/classify.is_human_event` is the single source of truth for whether a trigger counts as
"human" (used for queuing policy, below).

### 3.2 Concurrency model

`run_foreman_ai` serializes per `(guild_id, user_id)` or `(guild_id, "task:<id>")` via an
in-process `_GuildRunLock` busy-flag table (safe because asyncio only yields at `await`).
Automated (non-human) triggers are **dropped** if the relevant lock is busy; human triggers are
queued in a bounded FIFO and drained once the in-flight run completes. Events in
`_CHILD_FOREMAN_EVENTS = {task-complete, followup-done, needs-input, task-error}` run as an
isolated single-task **child** conversation (when `FOREMAN_CHILD_CONTEXTS` is enabled) rather than
the whole-guild **parent** conversation — this keeps a single task's back-and-forth from
polluting the guild-wide chat history, and vice versa.

### 3.3 Conversation history

Persisted per-turn to the `ForemanTurn` table. Each new run loads the last 5 human turns
(`_HUMAN_TURN_WINDOW`) plus their associated tool-exchange turns, always trimmed to start on a
`user`-role message (an Anthropic API requirement), then further pruned/capped
(`message_utils.prune_history`, `strip_orphaned_tool_results`) before every LLM call. The system
prompt is split into a stable, cache-controlled prefix (`prompt.build_system_blocks`, marked
`cache_control: ephemeral`) and a per-turn `<state>` preamble (online workers, non-terminal tasks,
current time) injected only into the live user turn — this keeps the system prompt 100%
cacheable across calls while state stays fresh.

### 3.4 One turn's control flow (`runner._run_foreman_ai`)

1. Build live `<state>` context + cached system blocks.
2. Loop up to `MAX_FOREMAN_ROUNDS` (10) rounds:
   a. Call the LLM (`_call_llm` — direct `call_anthropic`, or via the external proxy if connected).
   b. Persist the assistant turn; stream any `text` blocks to chat + Discord immediately
      (`_emit_foreman_chat`).
   c. If there are no `tool_use` blocks, the turn ends (`end_turn`).
   d. Otherwise, broadcast tool-use events, run all requested tools concurrently
      (`tools.exec_tools`, `asyncio.gather`), persist truncated tool results, append to the
      message list, and loop.
3. If the round cap is hit mid-tool-call, force one final `tool_choice: none` call so the Foreman
   always produces a clean wrap-up message rather than trailing off.

### 3.5 Tools (`foreman/tools.py` + `tools_schema.py`)

| Tool | Effect |
|---|---|
| `create_task` | Creates an unassigned `Task` row (`worker_id=NULL`) |
| `assign_task` | Assigns a task to a worker: resolves tool/tier/model/provider, validates repo access, locks per-worker to avoid double-assignment, broadcasts `task-assigned`, notifies Discord |
| `send_followup` | Re-dispatches an existing task on the same branch/worktree; picks a worker (original → preferred → any idle); locks per-task (queues a `pending-followup` `TaskEvent` if busy) |
| `finalize_task` | Marks a task `done`/`failed`, computes soft-delete TTL, cascades to terminal descendants for issue-rooted task trees, posts an issue-close summary comment |
| `redirect_task` | SIGTERMs the running agent and resumes with new instructions (`state → working`) |
| `cancel_task` | Marks `cancelled`, releases the task lock |
| `message_worker` | Sends a message into a worker's active agent terminal/stdin |
| `shutdown_worker` | Marks a worker disabled; schedules a force-kill if it doesn't drain |
| `spawn_worker` | Spawns a Docker worker container (implemented but currently excluded from the tool schema pending follow-up work) |
| `list_github_issues` / `get_github_issue` / `list_github_prs` / `search_github_issues` / `claim_github_issue` / `create_github_issue` / `get_pr_status` | Thin GitHub REST wrappers |
| `review_pr` | Delegates to an external A2A code-review agent, posts its verdict as a GitHub PR review |
| `review_pr_internal` | Fetches the PR diff directly and asks the Foreman's own LLM to produce a verdict + inline comments, posted via the GitHub Reviews API |
| `dnsid` | Resolve/sign/verify DNSid identities for A2A auth |
| `call_agent` | Generic A2A skill invocation against any agent-card URL |

Tools excluded from **child** (single-task) contexts: `create_task`, `assign_task` — a child owns
one already-assigned task and shouldn't spin up new ones.

### 3.6 System prompt (`foreman/prompt.py`)

Two personas — `FOREMAN_SYSTEM` (whole-guild) and `CHILD_FOREMAN_SYSTEM` (single-task) — cover:
task-lifecycle rules (`create_task`+`assign_task` pairing, finalize timing), the **plan / execute
/ review** phase workflow, PR-review verdict policy (APPROVE / COMMENT / REQUEST_CHANGES),
reacting to `[github-event]` / `[discord-thread-reply]` / `[worker-online/offline]` synthetic
messages, the devReady issue-pickup flow (search-before-create, dedup against existing
non-terminal tasks for the same issue), and reviewer-feedback-vs-issue-intent policy.

### 3.7 GitHub integration

All raw HTTP lives in `tools.py` (`_gh_api`/`_gh_api_post`/`_gh_api_diff`/`_gh_graphql`, run
off-thread). Token resolution (`_guild_github_token`) prefers the acting human's own OAuth token,
falls back to the GitHub App installation token for background operations, then the guild owner's
token. Reading issues/PRs, creating issues, claiming issues, and posting PR reviews (both the
external-agent path and the self-hosted-LLM path) all go through this layer; `_supersede_prior_bot_reviews`
resolves stale inline review threads via GraphQL before posting a new one. There is no tool for
the Foreman to *request* a human review — that direction is inbound-only: a
`pull_request.review_requested` webhook becomes a `[github-event]` chat message that the Foreman
reacts to by creating and assigning a review task.

### 3.8 Discord notifications

`backend/discord_notifier.py` is the actual bot/webhook client. Inside `foreman/`,
`runner._emit_foreman_chat` mirrors every plain-text narration line to Discord; `tools.py` fires
event-specific notifications (task assigned, followup, redirect, finalized/failed/cancelled) as
fire-and-forget background tasks (via `spawn()`) so a slow Discord call never blocks a tool call.

### 3.9 Other `foreman/` modules

- `a2a_client.py` / `auth.py` / `oidc.py` — Agent-to-Agent protocol client, DNSid
  challenge-response signing, and JWT/JWKS/Ed25519 verification, used by `review_pr`/`call_agent`.
- `proxy.py` — coordinates delegating one LLM call to an externally-connected standalone Foreman
  proxy over the existing WebSocket (§6.4).
- `github_url_parser.py` — rewrites pasted GitHub issue/PR URLs in chat into explicit
  owner/repo/number references.
- `message_utils.py` / `constants.py` / `state.py` — history windowing/pruning helpers, shared
  tunables (`MAX_FOREMAN_ROUNDS`, `MAX_HISTORY_MESSAGES`, terminal-state sets, TTLs), and an
  empty stub kept for import compatibility (history is fully DB-backed now).

## 4. Worker System

### 4.1 Server-side lifecycle (`backend/worker_lifecycle.py`)

The `workers` table tracks `id` (`w-`+6 hex), `guild_id`, `repos`/`tools` (JSON), `state` (default
`idle`), `last_seen`, `container_id`, `spawned_version`, `drain_requested_at`, `disabled`.
`get_current_version()` reads `PIONEER_VERSION`, falling back to a deterministic
`git show` committer-date+short-hash string.

On backend startup:
1. `drain_stale_workers_on_startup()` finds workers whose `spawned_version` doesn't match the
   current version (rolling-deploy detection) and soft-signals them (`WorkerShutdownMsg`).
2. `reset_connection_state()` flips every non-offline `Worker`/`Agent` row to `offline` — this is
   the literal "workers reset to offline on restart" behavior.
3. `reconcile_stale_workers()` runs a background two-phase state machine per drained worker:
   **reconnect** (wait up to `PIONEER_WORKER_RECONNECT_GRACE`, default 120s, for it to come back
   online; timeout ⇒ force-kill + replace) → **drain** (wait up to
   `PIONEER_WORKER_DRAIN_TIMEOUT`, default 1800s, for it to finish its task and go offline on its
   own; timeout ⇒ force-kill). A replacement worker is only spawned once the old one is
   confirmed down, so there's never two live workers for one logical "slot".

A separate liveness sweeper (`main.py`) independently probes any worker that hasn't sent a WS
frame in `WORKER_OFFLINE_AFTER_SECONDS` (90s) with a ping, marking it offline if unanswered within
`WORKER_PROBE_TIMEOUT_SECONDS` (10s). Sweep runs every `WORKER_SWEEP_INTERVAL_SECONDS` (30s).

### 4.2 Worker process internals (`worker/pioneer_worker/worker.py`)

Several concurrent asyncio tasks, all started from `run()` after registration → GitHub
token/env fetch → repo refresh → tool-availability checks → WebSocket connect → `_listen()` start
→ Claude auth check → `_join()` (only now does the backend see this worker's agents) → repo
clone/fetch → a hard-timeout (30s) initial worktree sweep → agent loops + auxiliary tasks:

- **`_listen()`** — single dispatch loop over incoming WS messages. Before `_joined`, only
  `pong`/`worker-message`/`worker-auth-response` are processed. Handles `task-assigned`,
  `task-followup`, `task-finalize` (releases worktrees early), `task-cancel`, `task-redirect`
  (SIGTERM + `--resume` with new instructions, or re-queue if nothing is running),
  `worker-message` (auth-code relay or live stdin injection), `worker-shutdown`.
- **`_agent_loop(slot)`** — one loop per fixed `Agent` slot (created once at startup for the
  process's whole lifetime, count = `max_agents`, default 4). Blocks on a task queue; a shutdown
  sentinel breaks the loop. Any crash marks the task `failed` and always clears
  `current_task_id` in `finally`.
- **`_idle_puller()`** — polls REST every `pull_interval` seconds for tasks missed during
  downtime; refreshes the worker's GitHub org repo list every 20 minutes; runs `git pull` across
  idle repos when the queue is empty and no agent is active. All exceptions are caught/logged,
  never propagated.
- **`_worktree_sweeper()`** — hourly; removes worktrees for tasks not currently running on any
  agent whose entries are all older than `WORKTREE_TTL_SECONDS` (24h).
- **`_s3_syncer()`** — optional (only if `s3_bucket` configured); syncs configured paths
  (e.g. `~/.claude`, `~/.codex`) to S3 immediately at startup and then every `s3_sync_interval`
  (default 600s).

Startup and shutdown use `asyncio.wait(..., FIRST_COMPLETED)` across agent runners and auxiliary
tasks so any crash triggers a coordinated shutdown; `worker-disconnect` is sent under a shielded
cancel scope so it's delivered even mid-cancellation.

### 4.3 Agent spawning (coding-agent runners)

All three runners share the return contract `(success: bool, stop_reason: str, last_text: str,
session_id: str | None)`, with `stop_reason` in `{success, max_turns, error_during_execution,
interrupted, no_events}`.

- **Claude** (`claude_runner.py`) — invokes
  `claude --output-format stream-json --verbose --max-turns <N> --dangerously-skip-permissions
  [--model M] [--resume <id>] -p <description>`, reading newline-delimited JSON stream events.
  Session id comes from the `system/init` event; stop reason from the `result` event. Subprocess
  stdout uses a 16 MiB line limit (`STDOUT_LINE_LIMIT`) — well above asyncio's default 64 KiB
  `StreamReader` cap — specifically to avoid truncating large `tool_result` payloads.
- **Codex** (`codex_runner.py`) — invokes
  `codex exec -C <cwd> [resume <id>] --json --output-last-message <tmpfile> [--model M]
  <description>` over a PTY stdin (codex treats non-TTY stdin as a piped prompt otherwise); final
  text is read back from the temp file. 8 MiB stdout limit. Retries once with a fresh session if
  `--resume` fails.
- **pi** (`pi_runner.py`) — RPC over stdin/stdout: `pi [--session <id>] --mode rpc [--provider P]
  [--model M]`, exchanging JSON lines (`prompt` → `message_update`/`tool_execution_start/end`
  → `agent_end`). 10 MB stream limit, with an explicit resync-by-draining path if a line overruns
  the limit rather than crashing. Same resume-once-then-fallback pattern as Codex.

### 4.4 Git worktree management (`git_ops.py`)

Repo mirrors live under `<repos_dir>/<owner>/<repo>`; per-task worktrees under
`<work_dir>/<guild_id>/<worker_id>/<task_id>/<repo_name>`. New tasks get branch
`claude/{slugified-name}-{task_id}`; follow-ups reuse the existing branch; review-phase tasks
resolve the PR's actual head branch and check it out via `gh pr checkout` (handling cross-fork
PRs) rather than creating a new branch. Worktrees are reused across a task's lifetime when
possible (fetch + hard-reset to pick up remote changes) rather than recreated. Cleanup happens at
three points: immediately on `task-finalize`, hourly via the sweeper for TTL-expired entries, and
at process startup (walking the worker's work directory for orphans left by a crashed prior run,
re-registering ones still within TTL, deleting the rest) — the whole startup sweep is
timeout-guarded since a stale git lock can otherwise hang indefinitely.

### 4.5 Configuration (`worker/pioneer-worker.toml.example`)

`backend_url`, `guild_id`, optional `worker_name`/`user`, `pull_interval` (default 300s),
`max_agents` (default 4, 1–16 in the UI). `[github]`: `repos` and/or `org` (accept any task under
`org/*`, cloning lazily), `token` (supports `env:VAR_NAME` indirection). `[paths]`:
`repos_dir`/`work_dir` plus per-tool executable overrides. `[claude]`: `max_turns`. `[codex]`:
`api_key`, extra `args` (e.g. sandbox/approval flags for unattended runs), `doctor` (run `codex
doctor` at startup). `[s3]`: optional session-log sync (`bucket`, `prefix`, `interval`, `paths`).

## 5. Data Model

### 5.1 `tasks` (id prefix `t-`)

| Field | Purpose |
|---|---|
| `id` | `t-` + 6 random lowercase/digit chars |
| `worker_id` | Owning worker; `NULL` = foreman-created and not yet assigned |
| `guild_id` | Tenant scoping |
| `description` | Instructions given to the coding agent |
| `tool` | `claude` \| `codex` \| `pi` |
| `model` / `provider` | Explicit dispatch override |
| `issue_number` / `issue_repo` / `issue_title` / `issue_state` | GitHub issue linkage (denormalized cache) |
| `state` | Lifecycle status (§5.3) |
| `branch` / `worktree_path` | Git working state |
| `pr_url` / `pr_number` / `pr_repo` | PR linkage — the structured `pr_number`/`pr_repo` fields let webhooks match without URL parsing |
| `name` | Short sidebar label |
| `parent_task_id` | Links sub-tasks (e.g. a review task) to a parent — a plain string, no FK constraint |
| `phase` | `plan` \| `execute` (default) \| `review` \| `issue` (root of an issue-anchored task tree) |
| `deleted_at` | Soft-delete instant; `live_tasks_filter()` hides rows past this timestamp |
| `user_id` | GitHub user id of the human who initiated the task |
| `model_tier` | `cheap` \| `standard` \| `powerful`, resolved at assignment time |
| `claude_session_id` | Agent session id, enabling `send_followup` to resume the same conversation |

Related tables: `TaskLog` (per-task streamed console lines, with a `level` used for frontend
styling and a JSON `data` field for structured tool input/output), `TaskEvent` (queued follow-up
instructions that arrived while a task's lock was held, replayed on release), `GithubEvent`
(append-only webhook delivery log, unique on `delivery_id` for idempotency), `GithubIssue` /
`GithubPullRequest` (local caches of GitHub state for the sidebar tree), and nullable `task_id`
foreign keys on `ApiRequestLog`, `ForemanTurn`, and `LlmUsage` for cost/usage attribution.

### 5.2 `workers` / `agents`

`workers`: `id` (`w-`+6 hex), `guild_id`, `repos`/`tools` (JSON), `state`, `last_seen`,
`container_id`, `spawned_version`, `drain_requested_at`, `disabled`. `agents`: a WebSocket
participant (`a-` prefix), one per worker agent slot, `state` ∈
`idle/thinking/working/busy/error/offline`, `current_task_id`.

### 5.3 Task states and phases (two orthogonal axes)

**State** — where a task is in its lifecycle: `pending` → `working` → `awaiting-review` →
(`done` | `failed` | `cancelled` | `error`). Not a DB enum — plain text columns, checked in
application code. Key transitions:

- `create_task` → `pending`.
- Worker begins running → `working` (also forced by `redirect_task`).
- Worker goes idle/errors while `working` → `awaiting-review` (guarded so a terminal state is
  never clobbered).
- `task-complete` WS message → `awaiting-review` (PR/session fields persisted regardless of the
  state guard).
- Foreman `finalize_task` → `done` or `failed`.
- `cancel_task` → `cancelled`.
- GitHub webhook: PR merged → `done` automatically, no Foreman round-trip.
- GitHub webhook: PR closed unmerged → `failed` automatically.
- A stale-task watchdog in `main.py` forces stuck `working` tasks back to `awaiting-review` and
  releases their lock.

**Phase** — what *kind* of work item this is: `plan` (worker produces an outline/spec, posted as
an issue comment, no PR), `execute` (default — normal implementation work), `review` (worker
checks out an existing PR branch, runs tests/lint, posts a `gh pr review`, never commits or opens
a PR), `issue` (a root node with no worker, representing an issue-anchored task tree for grouping
in the sidebar). The "follow-up loop" described in `AGENTS.md` is implemented via `send_followup`
re-dispatching an `awaiting-review` task back to `working` **without** changing its `phase`.

### 5.4 GitHub linkage population

`create_task`/`assign_task` write `issue_number`/`issue_repo`/`pr_number`/`pr_repo` directly from
tool-call input (and backfill from an existing task row on follow-up). When a worker reports
completion, `ws_handlers.handle_task_complete`/`handle_task_followup_done` parse the reported
`prUrl` into `pr_number`/`pr_repo`. GitHub webhooks consume those structured fields to find the
task to auto-finalize/auto-fail, and separately upsert the `GithubIssue`/`GithubPullRequest`
caches. The tasks-tree endpoint (`GET /guilds/{id}/tasks/tree`) groups tasks by a resolved
`(issue|pr, repo, number)` key, including resolving "Closes #N" references from cached PR bodies
and following `parent_task_id` links in both directions.

### 5.5 REST endpoints (`backend/routes/tasks.py`)

`GET /guilds/{id}/tasks` (list, optional issue filter) · `GET /guilds/{id}/tasks/{id}/logs` ·
`GET /guilds/{id}/logs` (guild-wide stream) · `POST /guilds/{id}/tasks/{id}/followup` (human
follow-up — just asks the Foreman to call `send_followup`) · `POST
/guilds/{id}/tasks/{id}/finalize` (force `done` with a soft-delete TTL, default 3 days) · `POST
/guilds/{id}/tasks/{id}/cancel` · `GET /guilds/{id}/tasks/tree` (sidebar grouping) · `POST
/guilds/{id}/tasks/{id}/redirect`. Task *creation* is not a REST endpoint — it only happens via
the Foreman's `create_task`/`assign_task` tools (or worker-registration/Discord-triggered flows).

### 5.6 Notable migrations

`e7f8a9b0c1d2` added `pr_number`/`pr_repo` and the `github_events` table (webhook linkage without
URL parsing) · a later migration indexed `parent_task_id` · `add_model_tier_to_tasks` added
`model_tier` · `add_issue_phase_and_index` introduced the `issue` phase value (a pure
application-level enum extension — `phase` has no DB constraint) · `add_claude_session_id_to_tasks`
added `claude_session_id`, directly enabling `send_followup`'s session-resume behavior.

## 6. LLM Provider System (`backend/foreman/llm.py`, `backend/foreman/providers/`)

### 6.1 Abstraction

There is no formal base class — the abstraction is a pair of stateless call functions that any
provider is translated into or out of:

- `call_anthropic(client, *, model, max_tokens, system, messages, tools, tool_choice)` — the path
  for anything Anthropic-Messages-API-shaped: direct Anthropic, Claude-on-Bedrock, and (via an
  adapter) non-Anthropic Bedrock models.
- `call_openai_compatible(client: httpx.AsyncClient, ...)` — POSTs to `/chat/completions`,
  translating Anthropic-shaped args in and an Anthropic-shaped response back out.
- `BedrockNativeClient` (`providers/bedrock.py`) — adapter exposing the same
  `.messages.with_raw_response.create()` surface but backed by the Bedrock Runtime Converse API,
  used transparently by `call_anthropic` for non-Anthropic Bedrock foundation models (Nova, Kimi
  K2), detected by model-ID prefix.

No provider path implements streaming or a separate token-counting method — usage is read off the
parsed response after each non-streaming call.

### 6.2 Providers

- **Anthropic (direct)** — default. `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` (OAuth,
  precedence), optional `ANTHROPIC_BASE_URL`. Model default via `FOREMAN_MODEL`
  (`claude-sonnet-4-6`).
- **Bedrock** — `FOREMAN_PROVIDER=bedrock`. Auth resolution order: `AWS_BEARER_TOKEN_BEDROCK` →
  explicit access/secret/session keys → `AWS_PROFILE` → default boto3 chain. Region via
  `AWS_DEFAULT_REGION`/`AWS_REGION` (default `us-east-1`). Model must be explicit
  (`FOREMAN_BEDROCK_MODEL`, an inference-profile ARN, or the guild's `model` field) — there is
  deliberately no cross-account default model. A credentials probe replicates boto3's resolution
  at client-creation time to surface *why* auth failed instead of an opaque SDK error.
- **OpenAI-compatible (e.g. Ollama)** — only reachable via the standalone proxy (the embedded
  backend never calls it directly). Config: `provider="openai"`, `openai_base_url` (default
  `http://localhost:11434/v1`), `api_key`, `model` (default `llama3.1`).

Guild-level settings (via the frontend's Foreman config UI) can override provider/model/env vars
for the API-client construction path only — not for spawned workers.

### 6.3 Model tier selection

Defined in `backend/util/model_tiers.py`: `select_model_tier(phase, complexity_hint=None)` is
**agent-agnostic** — the tier depends only on task `phase` and an optional explicit hint, never on
which coding tool executes it. Priority: explicit hint wins; else phase mapping (`issue→cheap,
plan→standard, execute→standard, review→cheap`, default `standard`). `get_model_for_tier(tier,
provider, catalog)` then walks a per-provider preference list against the models.dev catalog to
pick a concrete model ID. Invoked from `assign_task`/follow-up tool handlers; the resolved tier is
persisted as `Task.model_tier` and surfaced via `/api/cost`. This is distinct from
`FOREMAN_MODEL`/`get_foreman_model()`, which resolves the model for the Foreman's *own*
conversational loop — a separate selection axis from worker/task dispatch tiers.

### 6.4 Standalone proxy vs. embedded call

The proxy (`foreman-proxy/pioneer_foreman/`) reuses `backend/foreman/llm.py` directly — it holds
no provider-specific logic of its own, only "which machine" plumbing (translating the backend's
camelCase WS request into `llm.py`'s snake_case kwargs, then dispatching by configured provider).
`backend/foreman/proxy.py` tracks live proxy WS connections; `runner.resolve_foreman_client()`
picks: if a proxy is connected, the LLM call is delegated (`call_foreman_api_proxy`, sends a
`foreman-api-request` frame, awaits a future resolved by the matching `foreman-api-response`); if
not connected and the configured provider is outside the SDK-callable set, it raises rather than
silently falling back. If the proxy disconnects mid-flight, all its outstanding requests are
failed immediately.

### 6.5 Error handling

No custom retry/backoff wraps individual LLM calls — reliance is on the Anthropic SDK's built-in
retry for transient errors. The proxy path has a hard timeout (`FOREMAN_PROXY_API_TIMEOUT`,
default 600s). At the application level, the entire Foreman round-loop is wrapped in one
catch-all that surfaces `"Foreman error: {exc}"` to guild chat — there's no per-call retry or
backoff on 429/529 specifically. `util/api_latency.track_api_call` records latency/status/token
counts per call for observability, not for retry decisions.

## 7. GitHub Webhook Handling (`backend/routes/webhooks.py`)

### 7.1 Endpoints

`POST /webhooks/github/{guild_id}` — the webhook receiver. `POST
/guilds/{guild_id}/foreman/ci-notify` — a separate GitHub-Actions-injected CI completion notice
(bearer-token auth via `PIONEER_CI_KEY`, not HMAC) used to push CI results faster than waiting on
the default `check_run`/`check_suite` webhook cadence.

### 7.2 Security

HMAC-SHA256 signature check (`X-Hub-Signature-256`) against a per-guild secret
(`Guild.webhook_secret`, lazily generated with `secrets.token_hex(32)` on first access, rotatable
by the guild owner). Missing guild or missing secret → 404 (indistinguishable, to avoid leaking
existence); bad signature → 401. Payload capped at 256 KB; the persisted copy is truncated to 64
KB. Idempotency: each delivery is inserted into `GithubEvent` keyed by `X-GitHub-Delivery` with
`ON CONFLICT DO NOTHING` — duplicates return 202 without re-running side effects.

### 7.3 Guild resolution

There is **no payload-derived** guild mapping — the guild is determined entirely by the URL path.
Each repo's webhook is registered (by the worker, at PR-open time) pointing at that specific
guild's `/webhooks/github/{guild_id}` URL, so the guild is baked into the webhook configuration
itself, not inferred from `repository.owner`/`full_name` in the payload.

### 7.4 Event handling

- **`issues`** — caches the issue; on `opened/closed/reopened/edited`, updates `issue_state`/
  `issue_title` on all linked tasks; on `closed`, directly finalizes/sweeps linked tasks
  (`finalize_closed_issue`, no LLM round-trip). A devReady trigger fires on `labeled` with a
  devReady-family label (`devready`/`dev-ready`/`ready-for-dev`/`ready`, case-insensitive) or on
  `opened`/`reopened` if that label is already present — this schedules a Foreman dispatch that
  runs the "claim → create_task → assign_task" pickup flow, gated only by the label set and the
  Foreman's own dedup check (no repo allowlist beyond "this guild's secret matched").
- **`pull_request`** — caches the PR; backfills `pr_url` on `opened` and (by branch-name match)
  on `opened`/`synchronize`/`reopened` when still null; `closed` triggers deterministic
  finalize/fail (§7.5); `review_requested` creates a review task gated on the requested reviewer
  matching the guild owner's GitHub login — the only access filter on webhook-triggered task
  creation.
- **`pull_request_review`** / **`pull_request_review_comment`** / **`issue_comment`** — built into
  a Foreman-facing summary suggesting `send_followup`.
- **`check_run`** / **`check_suite`** — only dispatched once `status == completed` and
  `conclusion` isn't `neutral`/`skipped`; summary prompts `send_followup` on failure or
  "you can finalize_task" on success.
- **`status`** (legacy commit-status API) — summary from `state`/`context`/`description`.

Non-devReady/non-review_requested events all require a matched `task_id` and filter out `[bot]`
senders (except for check/status events, which are bot-authored by nature).

### 7.5 Deterministic auto-finalize (no Foreman round-trip)

- **PR merged** → `_auto_finalize_task_on_pr_merge`: a single conditional
  `UPDATE tasks SET state='done' WHERE id=... AND state NOT IN ('done','failed','cancelled')`
  (avoids a TOCTOU race), `deleted_at = now + 3 days`, releases the task lock, deletes queued
  follow-up events, broadcasts `task-finalize`/`task-update`. The event is still forwarded to the
  Foreman afterward as informational-only context.
- **PR closed unmerged** → `_auto_fail_task_on_pr_close`: symmetric, `state='failed'`,
  `deleted_at = now + 1 day`.

Everything else routes through a per-`(guild, task)` debounce queue (default 30s window) that
coalesces rapid events before calling `run_foreman_ai(..., child=True)`, letting the Foreman
decide `send_followup`/`finalize_task`/no-op with full context instead of reacting to every event
one at a time.

### 7.6 Webhook registration

No server-side GitHub App/webhook provisioning — registration happens client-side from the
**worker**, right after opening a PR: it fetches the guild's webhook secret, lists existing hooks
on the repo, and idempotently creates or PATCHes one matching on target URL, with events
`pull_request, pull_request_review, pull_request_review_comment, issue_comment, check_run,
status`. Notably, **`issues` and `check_suite` are not in this auto-registered event list** —
devReady issue pickup and `check_suite`-based CI feedback only work if the repo owner has also
enabled those events manually (or uses GitHub's "send me everything" webhook default).

## 8. Frontend (`frontend/src/`)

Vite 8 + Vue 3.5 + Pinia 3 + strict TypeScript 6 + Vue Router 5, Vitest for unit tests.

### 8.1 Layout

`views/` (`LandingView`, `AppView`, `DiscordConnectView`), `stores/` (§8.3), `components/` (shell:
`TopBar`, `MainView`, `GuildSidebar`, `ChatPane`, `FactoryFloor`, `IssueViewer`, `LogPane`,
`GuildMembers`, `GitHubConfigModal`, plus `landing/`, `chat-pane/`, `log-pane/`, `sidebar/`,
`sprites/` subfolders and a `factory-layout.json` coordinate file for the pixel-art visualization),
`composables/`, `utils/` (REST wrapper, formatting, markdown, repo grouping), `types.ts` (shared
types including the `WSInbound`/`WSOutbound` discriminated unions).

### 8.2 Guild configuration UI

- `landing/NewGuildModal.vue` — guild creation (name only).
- `TopBar.vue`'s Guild Settings popover — rename, **primary repo** select, Claude credential
  management, and a **Foreman config** block (provider, model, base URL, system-prompt textarea,
  arbitrary env-var pairs).
- `GuildMembers.vue` — invite UI (username/email + role, owner-gated, pending-invite list).
- `sidebar/SpawnWorkerForm.vue` — the actual repo-to-worker wiring: repo checkboxes grouped by
  org/owner, worker name, tool checklist, agent-count (1–16), dynamic env-var rows — distinct from
  the guild-level settings in `TopBar`.

### 8.3 Pinia stores

| Store | Owns |
|---|---|
| `auth.ts` | Login token, GitHub user info, OAuth flow |
| `guild.ts` | WebSocket connection, guild list/current guild, message fan-out (`addMessageHandler`) |
| `agents.ts` | Agent list/states, terminal output buffers |
| `tasks.ts` | Task list/logs, `STATE_LABELS`/`TERMINAL_STATES` (single source of truth for state display, shared with `FactoryFloor`) |
| `github.ts` | GitHub issue/PR fetching for the UI |
| `usage.ts` | Per-task/guild API-call counts and cost (`costUsd`) — not yet documented in `AGENTS.md`'s store table |

`guild.ts` owns the single WebSocket. Two fan-out mechanisms coexist: `AppView.vue` passes a
closure into `connectWebSocket` that drives `agents`/`tasks`/`usage` store updates directly, while
`ChatPane.vue` separately registers via `addMessageHandler` to append system chat lines for
task/agent lifecycle events.

### 8.4 Task visualization

- **Sidebar tree** (`sidebar/TaskTree.vue`/`TaskTreeRow.vue`) — hierarchical list with a
  state-colored pill/dot per task.
- **Detail/logs** (`LogPane.vue` + `log-pane/*`) — header, actions, and a scrolling terminal-style
  log viewer fed by the `tasks`/`agents` store log buffers, plus a usage panel.
- **Factory floor** (`FactoryFloor.vue` + `factory-layout.json` + `sprites/RobotWorker.vue` +
  `AgentAvatar.vue`) — the pixel-art visualization. Active tasks render as positioned
  "work-station" elements with an animated monitor and a state-colored badge; agents render as
  sprites that walk to their assigned station or patrol idle, with think/work/error overlay icons
  driven by `agent-state` WS updates.

### 8.5 Routing

Two route sets depending on hostname: guild-subdomain mode (single catch-all → `AppView` with
`guildId` from the subdomain) or path mode (`/` → `LandingView`, `/auth/discord/connect` →
`DiscordConnectView`, `/:guildId` → `AppView` — `TopBar` + `GuildSidebar` + `MainView` + `ChatPane`
+ a teleported `DebugSidebar`).

## 9. Configuration Reference

| Concern | Variable(s) |
|---|---|
| Database | `DATABASE_URL` (required, no fallback — Postgres in production, SQLite for lightweight local dev) |
| GitHub OAuth | `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_REDIRECT_URI` |
| Foreman LLM (direct) | `FOREMAN_MODEL`, `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` |
| Foreman LLM (Bedrock) | `FOREMAN_PROVIDER=bedrock`, `FOREMAN_BEDROCK_MODEL`, `AWS_BEARER_TOKEN_BEDROCK` / `AWS_ACCESS_KEY_ID`+`AWS_SECRET_ACCESS_KEY`+`AWS_SESSION_TOKEN` / `AWS_PROFILE`, `AWS_DEFAULT_REGION` |
| Standalone foreman proxy | `PIONEER_BACKEND_URL`, `PIONEER_GUILD_ID`, `FOREMAN_PROVIDER`, `FOREMAN_MODEL`, `FOREMAN_BASE_URL`, `FOREMAN_PROXY_API_TIMEOUT` |
| Worker rolling deploys | `PIONEER_VERSION`, `PIONEER_WORKER_RECONNECT_GRACE` (120s), `PIONEER_WORKER_DRAIN_TIMEOUT` (1800s), `PIONEER_SHUTDOWN_TIMEOUT` (1800s) |
| Worker liveness sweep | `WORKER_OFFLINE_AFTER_SECONDS` (90), `WORKER_PROBE_TIMEOUT_SECONDS` (10), `WORKER_SWEEP_INTERVAL_SECONDS` (30) |
| Session-log S3 sync | `PIONEER_S3_BUCKET`, `PIONEER_S3_PREFIX`, `PIONEER_S3_SYNC_INTERVAL` |
| Discord | `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_APPLICATION_ID`, `DISCORD_PUBLIC_KEY`, `DISCORD_ALLOWED_ROLE_IDS`, `DISCORD_STREAM_TASKS`, `DISCORD_GATEWAY_ENABLED` |
| CI fast-path | `PIONEER_CI_KEY` (bearer auth for `/foreman/ci-notify`) |
| Worker config file | `worker/pioneer-worker.toml` — `backend_url`, `guild_id`, `[github]`, `[paths]`, `[claude]`/`[codex]`, `[s3]` |

## 10. Summary of Key Design Decisions

- **State vs. phase are orthogonal.** `state` tracks lifecycle position; `phase` tracks work-item
  kind (plan/execute/review/issue-root). Conflating them would make the plan→execute→review
  pipeline unrepresentable without new state values.
- **Deterministic auto-finalize bypasses the LLM entirely** for the highest-confidence signal (PR
  merged/closed) — a single conditional `UPDATE` rather than a Foreman round-trip, both faster and
  immune to LLM misjudgment on the one case where the answer is unambiguous.
- **Guild resolution from URL, not payload**, for webhooks — avoids any ambiguity from
  multi-guild repos or forks, at the cost of requiring the worker to register a per-guild webhook
  per repo.
- **System-prompt/state-preamble split** in the Foreman keeps the (large, static) system prompt
  100% cache-eligible while still injecting fresh live context every turn — a direct cost/latency
  optimization for a system that calls the LLM on nearly every WebSocket event.
- **Tier selection is phase-driven and agent-agnostic** — deliberately decoupled from which coding
  tool executes the task, so switching the default coding agent doesn't require re-deriving cost
  tiers.
- **Child vs. parent Foreman contexts** isolate one task's back-and-forth from whole-guild chat,
  preventing unrelated task noise from crowding the human-facing conversation window while still
  letting periodic/webhook-driven events reach a focused, single-task LLM call.
