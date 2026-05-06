# GitHub PR Event Subscriptions — Design Document

## 1. Current State

### What happens today

When a worker finishes a task it calls `open_pr()` in
`worker/pioneer_worker/github_pr.py` (lines 88–151) via the GitHub REST API
(`POST /repos/{repo}/pulls`) and returns the PR URL. The URL is sent back to
the backend in the `task-complete` WebSocket message
(`worker/pioneer_worker/worker.py` line 1289–1300):

```json
{
  "type": "task-complete",
  "workerId": "w-abc123",
  "taskId": "t-xyz789",
  "branch": "claude/fix-login-bug",
  "prUrl": "https://github.com/acme/backend/pull/42",
  "lastText": "..."
}
```

The backend handler (`backend/ws_handlers.py`, `handle_task_complete()` ~line
377) sets the task state to `awaiting-review`, persists the PR URL in
`tasks.pr_url`, broadcasts the update to all guild WebSocket connections, and
spawns the foreman AI (`run_foreman_ai()`, ~line 427). The foreman receives a
natural-language summary: "Worker finished task, branch is X — review it, call
`send_followup` or `finalize_task`."

### What the foreman can do about a PR today

The foreman has read-only GitHub tools — `list_github_prs`, `get_github_issue`,
`search_github_issues` — but **no ability to watch a PR for post-creation
events**. After `finalize_task` is called, the system has no further visibility
into that PR.

### What is missing

| Event | Currently received? |
|---|---|
| PR comments (human/bot) | No |
| Review requests / approvals / change requests | No |
| CI / status check results (pass, fail, pending) | No |
| PR merged / closed | No |

There is no webhook endpoint in the backend and no background polling for PR
state changes. Once a task is finalized the system is blind to what happens next
on GitHub.

---

## 2. Options Considered

### 2a. GitHub Webhooks (push model)

GitHub sends an HTTP POST to a configurable URL whenever repository events
occur. Relevant event types: `pull_request`, `pull_request_review`,
`pull_request_review_comment`, `issue_comment` (on PRs), `check_run`,
`check_suite`, `status`.

**Delivery mechanism:** A new FastAPI route — e.g.
`POST /webhooks/github/{guild_id}` — receives the raw payload. GitHub sends an
`X-Hub-Signature-256` header (HMAC-SHA256 of the body with a shared secret) for
authentication. The handler verifies the signature before processing.

**How the foreman gets notified:** After signature verification and filtering
(only events for PRs this guild opened), the handler persists the event and
calls `run_foreman_ai()` directly (or enqueues it via the existing
`reset_foreman_poll()` pattern in `backend/foreman/runner.py` line 414).

**Pros:**
- Real-time; no polling latency
- No wasted API calls
- Official GitHub-recommended approach

**Cons:**
- Requires a publicly reachable URL (problematic for local/dev deployments
  without a tunnel like ngrok or Cloudflare Tunnel)
- Per-repo webhook registration (one webhook per watched repo, or an org-level
  webhook for org owners)
- Must handle delivery retries and idempotency (GitHub retries on 5xx)
- Secret management per guild or per repo

**Scope of webhook secret:** The simplest model is one webhook secret per
guild, stored in a new `github_webhook_secrets` table (or a column on
`guilds`). Each guild registers its repos' webhooks with the same secret;
`/webhooks/github/{guild_id}` validates against the guild's stored secret.

---

### 2b. Polling via GitHub REST/GraphQL API

A background task periodically calls GitHub to check the state of open PRs.

**Where it runs:** A new coroutine started per guild alongside the existing
`_poll_loop()` in `backend/foreman/runner.py` (line 37), or a single
process-wide scheduler.

**What it queries:** For each task in `awaiting-review` or `done` (with a
non-null `pr_url`), call:
- `GET /repos/{repo}/pulls/{pr_number}` — merged/closed state, review decision
- `GET /repos/{repo}/pulls/{pr_number}/reviews` — approvals/change requests
- `GET /repos/{repo}/pulls/{pr_number}/comments` — inline review comments
- `GET /repos/{repo}/statuses/{sha}` or `GET /repos/{repo}/check-runs?head_sha={sha}`
  — CI results

**Pros:**
- Works with any network topology (no public URL needed)
- No webhook registration step; easier for self-hosters
- Single GitHub OAuth token already in place (fetched via `_guild_github_token()`
  in `backend/foreman/tools.py` line 425)

**Cons:**
- Latency proportional to polling interval (minutes, not seconds)
- GitHub rate limits: 5,000 requests/hour per token for REST;
  polling dozens of PRs every minute burns quota fast
- More complex state tracking (store last-seen comment ID, last-checked SHA)
  to avoid re-triggering the foreman on already-processed events

---

### 2c. GitHub App vs OAuth Token

**Current auth:** The system uses GitHub OAuth tokens stored in `github_tokens`
(`backend/models.py` line 112). These are user-scoped — they act as the logged-in
human and inherit their repository permissions.

**GitHub App advantages for webhooks:**
- App webhooks can be org-level (one registration covers all repos in the org)
- Fine-grained permission model; can request only `pull_requests: read` and
  `checks: read` without full repo access
- No per-repo webhook setup required for org installs
- Higher rate limits (5,000 req/hr per installation)
- Required for receiving `check_run`/`check_suite` events reliably (status
  checks from Apps require App auth to read in some configurations)

**GitHub App disadvantages:**
- Non-trivial setup: create App in GitHub settings, distribute private key,
  handle installation tokens (JWT → installation token exchange, 1-hour expiry)
- Requires a new auth flow alongside the existing OAuth flow
- App installation requires admin rights on the repo/org

**Recommendation:** Start with OAuth tokens + per-repo webhooks (they already
exist). GitHub App migration is a later upgrade if org-wide coverage or CI check
events from Apps are needed.

---

## 3. Recommended Architecture

**Recommendation: GitHub Webhooks + in-process event queue**

Webhooks give real-time delivery at minimal API cost. The missing public-URL
requirement is manageable for production deployments; local dev can use a tunnel.

### End-to-end flow

```
GitHub ──POST─────────────────────────────────────────────────────────────────►
         /webhooks/github/{guild_id}
              │
              │  1. Verify X-Hub-Signature-256 (HMAC-SHA256, guild secret)
              │  2. Parse event type + PR number
              │  3. Look up task by pr_url / branch name
              │  4. Persist GithubEvent row (idempotent, dedup by delivery_id)
              │  5. Broadcast `github-event` to guild WebSocket
              │
              ▼
        backend/routes/webhooks.py
              │
              │  asyncio.create_task()
              ▼
        run_foreman_ai(
          guild_id,
          "[github-event] PR #42 (task t-xyz789): CI failed — rspec suite..."
        )
              │
              ▼
        Foreman AI ──tool call──► send_followup(task_id, "CI failed: fix ...")
                                             │
                                             ▼
                              Worker re-runs Claude in same worktree
```

**Guild WebSocket broadcast** (`github-event` message) lets the frontend display
live CI/review status badges without polling REST.

### Webhook registration flow

When a worker opens a PR, `github_pr.py:open_pr()` (or a new
`register_webhook()` helper) calls:

```
POST /repos/{repo}/hooks
{
  "name": "web",
  "config": { "url": "{backend_url}/webhooks/github/{guild_id}", "secret": "{secret}", "content_type": "json" },
  "events": ["pull_request", "pull_request_review", "pull_request_review_comment",
              "issue_comment", "check_run", "status"],
  "active": true
}
```

If a webhook already exists for that URL it is reused (check with
`GET /repos/{repo}/hooks`). The secret is generated once per guild and stored in
the DB.

---

## 4. Data Model

### New DB table: `github_events`

```sql
CREATE TABLE github_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      TEXT    NOT NULL,
    task_id       TEXT,              -- FK → tasks.id (nullable: event may arrive before task is linked)
    delivery_id   TEXT    NOT NULL UNIQUE,  -- X-GitHub-Delivery header, for dedup
    event_type    TEXT    NOT NULL,  -- pull_request | pull_request_review | check_run | status | ...
    action        TEXT,              -- opened | closed | submitted | completed | ...
    repo          TEXT    NOT NULL,  -- owner/repo
    pr_number     INTEGER,
    pr_url        TEXT,
    sender_login  TEXT,
    payload_json  TEXT    NOT NULL,  -- full raw payload (trimmed to 64 KB)
    created_at    TEXT    NOT NULL
);
CREATE INDEX github_events_task_id ON github_events(task_id);
CREATE INDEX github_events_guild_id ON github_events(guild_id, created_at);
```

**Linking events to tasks:** On arrival, query:

```sql
SELECT id FROM tasks
WHERE guild_id = ? AND (
    pr_url LIKE '%/pull/' || ? -- match by PR number + repo
    OR branch = ?              -- match by branch name from PR head ref
)
ORDER BY created_at DESC LIMIT 1;
```

### New column: `tasks.pr_number` and `tasks.pr_repo`

Extract PR number from `pr_url` at PR-creation time and store it explicitly
(avoids fragile string matching). Add via Alembic migration:

```python
op.add_column("tasks", sa.Column("pr_number", sa.Integer(), nullable=True))
op.add_column("tasks", sa.Column("pr_repo", sa.Text(), nullable=True))
```

### New column: `guilds.webhook_secret`

```python
op.add_column("guilds", sa.Column("webhook_secret", sa.Text(), nullable=True))
```

Generated with `secrets.token_hex(32)` on first webhook registration.

### Event types and fields that matter

| GitHub event | `action` values | Key fields | What triggers in foreman |
|---|---|---|---|
| `pull_request` | `closed` (merged=true/false), `reopened`, `synchronize` | `merged`, `head.sha`, `pr.number` | Notify foreman: PR merged → finalize; PR closed unmerged → investigate |
| `pull_request_review` | `submitted` | `state`: approved/changes_requested/commented, `body`, `user.login` | Approved → foreman can finalize or flag; changes_requested → send_followup |
| `pull_request_review_comment` | `created` | `body`, `path`, `diff_hunk`, `user.login` | Post to foreman as context for send_followup |
| `issue_comment` (on PRs) | `created` | `body`, `user.login` | Feed to foreman; human reviewer may be requesting changes |
| `check_run` | `completed` | `conclusion`: success/failure/cancelled, `name`, `output.summary` | CI fail → send_followup to fix; CI pass → foreman can finalize |
| `status` | (no action field) | `state`: pending/success/failure/error, `context`, `description` | Legacy CI status; treat same as check_run failure/success |

### Filtering noise

- Ignore events from bots matching `[bot]` suffix in `sender.login` unless the
  event is a CI check (bots drive most CI)
- Ignore `check_run` events with `status != "completed"` (pending runs are noisy)
- Deduplicate by `delivery_id` (unique constraint on `github_events.delivery_id`)

---

## 5. Implementation Plan (Priority Order)

### Phase 1 — Core webhook receiver (backend only, no foreman integration)

1. **Alembic migration** — add `github_events` table, `guilds.webhook_secret`,
   `tasks.pr_number`, `tasks.pr_repo` columns.

2. **`backend/routes/webhooks.py`** — new FastAPI router:
   ```python
   @router.post("/webhooks/github/{guild_id}")
   async def github_webhook(guild_id: str, request: Request): ...
   ```
   - Reads body as raw bytes (before JSON parse, for HMAC)
   - Verifies `X-Hub-Signature-256` against `guilds.webhook_secret`
   - Parses `X-GitHub-Event` header + JSON body
   - Inserts `GithubEvent` row (idempotent: `INSERT OR IGNORE` on `delivery_id`)
   - Returns `202 Accepted` immediately

3. **Register router** in `backend/main.py` (alongside existing routers).

4. **`backend/routes/guilds.py`** — add `POST /guilds/{guild_id}/webhook-secret`
   endpoint to generate/rotate the secret (owner-only). Returns the secret once
   so the user can configure it on GitHub.

5. **Tests** — add pytest fixtures that POST mock GitHub payloads with valid and
   invalid HMAC to verify acceptance/rejection.

### Phase 2 — Foreman notification

6. **Event → foreman bridge** — after persisting the event, look up the task by
   PR number+repo, then call `run_foreman_ai()` with a structured summary:
   ```
   [github-event] PR #42 (task t-xyz789, jmelloy/pioneer-square):
   check_run "rspec" completed: failure
   Summary: 3 tests failed in spec/auth_spec.rb
   ```
   Respect the existing `reset_foreman_poll()` pattern to avoid concurrent
   foreman invocations per guild.

7. **Foreman system prompt update** (`backend/foreman/prompt.py`) — add a
   section describing `github-event` triggers and the expected response
   (send_followup, finalize_task, or no-op with a log line).

8. **New foreman tool: `get_pr_status`** — wraps existing `_gh_api` helper to
   fetch `GET /repos/{repo}/pulls/{number}/reviews` and
   `GET /repos/{repo}/commits/{sha}/check-runs`, giving the foreman on-demand
   visibility during its decision loop.

### Phase 3 — Webhook registration automation

9. **`worker/pioneer_worker/github_pr.py`** — after `open_pr()` succeeds, call
   a new `ensure_webhook()` function that:
   - Fetches the guild's webhook secret from `/guilds/{guild_id}/webhook-secret`
   - Lists existing hooks on the repo (`GET /repos/{repo}/hooks`)
   - Creates or updates the webhook if the Pioneer Square URL isn't present

10. **`backend/routes/guilds.py`** — add `GET /guilds/{guild_id}/webhook-secret`
    (worker-accessible, authenticated) so workers can fetch the secret without
    it being in worker config.

### Phase 4 — Frontend

11. **`frontend/src/stores/tasks.ts`** — handle new `github-event` WS message
    type; update the task's `ciStatus` / `reviewState` in-store.

12. **Frontend UI** — display CI badges and review status on task cards in the
    sidebar.

### Phase 5 — GitHub App migration (optional, later)

13. Replace per-repo OAuth webhook setup with a GitHub App installation flow for
    org-level coverage and higher rate limits.

---

## 6. Open Questions / Risks

### Network reachability
Webhooks require a public HTTPS URL. For local development, operators will need
a tunnel (ngrok, Cloudflare Tunnel, etc.). A polling fallback (see §2b) could be
activated when no public URL is configured, at the cost of latency.

### Multi-guild, multi-repo webhook fan-out
A single backend instance may serve many guilds each with many repos. Each repo
needs its own webhook (GitHub does not support wildcard repo targeting via
OAuth). At scale this becomes many webhook registrations. Mitigation: only
register webhooks for repos that have active tasks; deregister on task
finalization.

### Token scope
The existing GitHub OAuth flow (`backend/routes/auth.py`, `/auth/github/login`)
requests the default scope. Creating webhooks requires `admin:repo_hook` or
`write:repo_hook` scope — this must be added to the OAuth scope in
`backend/oauth.py`. Existing users will need to re-authorize.

### Idempotency and retries
GitHub retries webhook deliveries for up to 72 hours on non-2xx responses. The
`delivery_id` unique constraint in `github_events` prevents duplicate processing,
but the backend must return `200/202` even when it ignores a duplicate delivery.

### Foreman concurrency
`run_foreman_ai()` is serialized per-guild via the `_active_foreman` lock in
`backend/foreman/runner.py`. If a webhook arrives while the foreman is already
running (e.g., mid task-complete review), it will be queued or dropped depending
on the current `spawn()` behavior. The implementation should enqueue webhook
events for replay after the current foreman turn completes — similar to how
`reset_foreman_poll()` works today.

### Secret rotation
If `guilds.webhook_secret` changes, all registered webhooks for that guild must
be updated (`PATCH /repos/{repo}/hooks/{hook_id}`). A helper endpoint or CLI
command should handle this atomically.

### CI event volume
`check_run` events fire for every CI step, not just the final suite result. A
repo with 50 CI steps will fire 50 events per push. Filter to only act on events
where `check_run.app.slug` matches known CI providers and
`check_run.conclusion in (failure, success)`, and aggregate by `check_suite_id`
to avoid triggering the foreman 50 times per commit.

### Private vs public repos
Webhook payloads contain code-adjacent metadata (diff hunks in review comments).
Ensure the `payload_json` stored in `github_events` is not exposed over
unauthenticated endpoints and is subject to the same guild membership checks as
task data.
