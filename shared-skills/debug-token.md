---
name: debug-token
description: Use the operator-level DEBUG_TOKEN to inspect live Pioneer Square backend state (tasks, task logs, workers) via /debug/query and the MCP debug tools.
---

# Debug Token Usage

> Sources of truth: `backend/auth_deps.py` (`check_debug_token`, `require_debug_token`),
> `backend/routes/debug_query.py`, `worker/skills/debug-query/`. If this doc and the code
> disagree, the code wins — fix this doc.

## What it is

The **debug token** is a single operator-wide shared secret, held in the backend's `DEBUG_TOKEN`
environment variable. It is **not** a per-user or per-guild credential and is not minted by any
API. Presenting it unlocks:

| Surface | Auth | Purpose |
|---------|------|---------|
| `POST /debug/query` | debug token only | Read-only SQL against operational tables |
| MCP `debug_query`, `cloudflare_logs` | Bearer token **plus** debug token + `X-Guild-Id` | Same, over MCP (see [mcp-server-prod.md](mcp-server-prod.md)) |

Use it when logs and the UI aren't enough: a task is stuck, a worker looks wedged, or you need to
correlate rows across tables (`tasks` ↔ `task_events` ↔ `task_logs` ↔ `workers`).

**Don't use it** when a normal guild-scoped endpoint answers the question (e.g.
`GET /guilds/{guild_id}/tasks/{task_id}/debug` with a member token returns a unified timeline).
Prefer the least-privileged tool that works.

## How to obtain it

You do not generate it by calling an API. It is provisioned by whoever runs the backend:

| Environment | Where it lives |
|-------------|----------------|
| Local dev | Set `DEBUG_TOKEN=...` in the backend's environment (e.g. `docker-compose.override.yml`), then restart. |
| Production (AWS) | SSM SecureString `.../debug_token`, injected into ECS as `DEBUG_TOKEN` (`terraform/ssm.tf`, `terraform/ecs.tf`). Terraform seeds a random 48-char value unless `var.debug_token` is set. |
| Worker containers | The backend forwards `DEBUG_TOKEN` into worker env **only if it is set on the backend** (`backend/utils.py`). Check with `test -n "$DEBUG_TOKEN" && echo available`. |

Generate a strong value for local use:

```bash
openssl rand -hex 24
```

> ⚠️ **If `DEBUG_TOKEN` is unset, the `/debug/*` routes are not mounted at all** (`main.py`
> registers them conditionally) and the MCP debug tools are disabled. A 404 on `/debug/query`
> usually means the token isn't configured on that backend — not that you typed it wrong.

> ⚠️ **Never ask a user to paste the token into chat, and never commit it.** If it isn't in your
> environment, the capability isn't available to you.

## Making authenticated calls

The token goes in `X-Debug-Token` (or as `Authorization: Bearer <token>`).

```bash
export PIONEER_BACKEND_URL=http://localhost:8000   # worker image default: http://backend:8000
# DEBUG_TOKEN must already be in your environment

curl -sS -X POST "$PIONEER_BACKEND_URL/debug/query" \
  -H "X-Debug-Token: $DEBUG_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT id, state, worker_id FROM tasks ORDER BY created_at DESC LIMIT 5"}'
```

Use `jq` to build the body so quoting never breaks the JSON:

```bash
q() {
  curl -sS -X POST "${PIONEER_BACKEND_URL:-http://backend:8000}/debug/query" \
    -H "X-Debug-Token: $DEBUG_TOKEN" -H "Content-Type: application/json" \
    -d "$(jq -n --arg sql "$1" '{sql: $sql}')"
}
q "SELECT count(*) AS n FROM tasks WHERE state = 'working'" | jq .
```

Inside a worker, the bundled wrapper does the same thing:

```bash
"$PIONEER_SKILL_DIR/scripts/query.sh" "SELECT id, state FROM tasks WHERE state = 'working' LIMIT 10"
```

### Response codes

| Code | Meaning |
|------|---------|
| 200 | JSON array of row objects |
| 400 | Rejected by validator (not a single SELECT, comment present, table not allowed, forbidden function) |
| 401 | No token supplied |
| 403 | Token supplied but wrong |
| 404 | Route not mounted — `DEBUG_TOKEN` unset on that backend |
| 504 | Query exceeded the 30s timeout |

## Server-side limits

Enforced by the backend, not by clients:

- One `SELECT` statement; **no SQL comments** (`--`, `/* */`), no `;`-chained statements.
- Runs in `SET TRANSACTION READ ONLY` — the database rejects all writes.
- Allow-listed tables only: `tasks`, `task_logs`, `task_events`, `workers`, `agents`,
  `github_events`, `llm_usage`, `foreman_turns`, `conversations`, `messages`, `github_issues`,
  `github_pull_requests`, `discord_thread_bindings`, `discord_channel_guilds`.
  Credential tables (`github_tokens`, `user_sessions`, …) are unreachable.
- 500-row cap, 30s statement timeout.
- Filesystem/network functions (`pg_read_file`, `dblink`, `lo_*`, …) are blocked.
- A bare `:name` token can be mistaken for a bind parameter; `::type` casts and ISO timestamps are fine.

## Common troubleshooting queries

Task columns you'll use most: `id, worker_id, guild_id, state, phase, name, branch, pr_url,
pr_number, issue_number, parent_task_id, created_at`. Task states flow
`pending → working → awaiting-review → done | failed`.

```bash
# What is running right now?
q "SELECT id, name, state, worker_id, created_at FROM tasks WHERE state IN ('pending','working') ORDER BY created_at"

# One task's full row
q "SELECT * FROM tasks WHERE id = 't-abc123'"

# Recent lifecycle events for a task
q "SELECT id, subtype, created_at FROM task_events WHERE task_id = 't-abc123' ORDER BY created_at DESC LIMIT 20"

# Last log lines (the 'data' column holds untruncated tool output)
q "SELECT timestamp, level, line FROM task_logs WHERE task_id = 't-abc123' ORDER BY timestamp DESC LIMIT 30"

# Is the assigned worker known and what does it cover?
q "SELECT id, repos, tools FROM workers WHERE id = 'w-xyz'"

# Pending tasks nobody picked up (foreman-created tasks start with worker_id NULL)
q "SELECT id, name, created_at FROM tasks WHERE state = 'pending' AND worker_id IS NULL"

# What did the foreman last do?
q "SELECT * FROM foreman_turns ORDER BY 1 DESC LIMIT 5"
```

If a column name errors, run `SELECT * FROM <table> LIMIT 1` to see the real shape rather than guessing.

## Example: debugging a stuck task

**Symptom:** task `t-abc123` has shown `working` for hours with no PR.

1. **Confirm state and age.**
   ```bash
   q "SELECT id, state, phase, worker_id, branch, pr_url, created_at FROM tasks WHERE id='t-abc123'" | jq .
   ```
2. **Check whether logs are still flowing.**
   ```bash
   q "SELECT timestamp, line FROM task_logs WHERE task_id='t-abc123' ORDER BY timestamp DESC LIMIT 10" | jq -r '.[] | "\(.timestamp) \(.line)"'
   ```
   Compare the newest timestamp to now. Minutes old → still working, be patient. Hours old → stalled.
3. **Check the worker.**
   ```bash
   q "SELECT id, repos FROM workers WHERE id = (SELECT worker_id FROM tasks WHERE id='t-abc123')"
   ```
   > A row in `workers` does not prove the worker is online. Confirm liveness from the
   > foreman's `[worker-offline]` events or `task_events`.
4. **Look at lifecycle events** for a missing `task-complete` or a `needs-input` escalation:
   ```bash
   q "SELECT subtype, created_at FROM task_events WHERE task_id='t-abc123' ORDER BY created_at DESC LIMIT 20"
   ```
5. **Decide:**

   | Finding | Action |
   |---------|--------|
   | Logs fresh | Wait; ask the foreman for `get_task_status` |
   | Logs stale, worker offline | Foreman: `send_followup` (picks another idle worker) |
   | Logs stale, worker online | Foreman: `redirect_task`, then `cancel_task` if no response |
   | `needs-input` event, unanswered | Answer the worker / escalate to a human |
   | Task never left `pending`, `worker_id` NULL | No assignment happened — `assign_task` or spawn a worker |

The debug token is **read-only** — remediation goes through the normal foreman tools or guild
endpoints (`/guilds/{guild_id}/tasks/{task_id}/redirect|cancel|followup`), not through SQL.

## Security considerations

> ⚠️ **The token is all-or-nothing and long-lived.** It has no scopes and no expiry. It is valid
> until the operator rotates `DEBUG_TOKEN` (SSM update + ECS redeploy).

- **Not guild-scoped.** One query can return rows from every guild, including task descriptions,
  chat `messages`, and LLM usage. Filter by `guild_id` yourself and only report what's relevant.
- **Read-only, but sensitive.** Task logs and messages can contain secrets pasted by users or
  emitted by tools. Don't echo bulk rows into chat, issues or PRs; summarise.
- **Comparison is constant-time** (`hmac.compare_digest`), but there is no per-token rate limit on
  `/debug/query` itself. Keep queries narrow and use `LIMIT`.
- **Never** put the token in URLs, commit messages, logs, PR descriptions or screenshots.
- **Rotate immediately** if it appears anywhere it shouldn't: update the SSM parameter, redeploy.
- Terraform deliberately seeds a random value rather than a shared placeholder, so an
  unconfigured prod is unreachable rather than open.
