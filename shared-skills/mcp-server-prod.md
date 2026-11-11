---
name: mcp-server-prod
description: Query the production Pioneer Square MCP endpoint (POST /mcp) safely — auth tiers, rate limits, available tools, and a read-only incident investigation workflow.
---

# MCP Server Against Prod

> Sources of truth: `backend/routes/mcp.py`, `docs/mcp.md`, `backend/routes/debug_query.py`.

## What it provides

`POST /mcp` is a **stateless MCP server** (JSON-RPC 2.0 over HTTP, JSON-response mode, protocol
`2025-03-26`). It implements only `initialize`, `ping`, `tools/list` and `tools/call`. It does
not use the `mcp` Python SDK, has no sessions, no resources, no prompts and no SSE streaming.

| Tool | Tier | What it does |
|------|------|--------------|
| `list_tasks {guild_id, state?, limit?}` | Normal | Recent tasks in a guild (limit ≤ 100, default 20) |
| `get_task {guild_id, task_id}` | Normal | One task by id |
| `debug_query {sql}` | Debug | Read-only SELECT on allow-listed operational tables |
| `cloudflare_logs {script?, search?, minutes?, limit?}` | Debug | Cloudflare Workers logs (needs `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` on the backend; request shape **not verified against the live API**) |

> ⚠️ **There are no file-reading or code-search tools.** This server exposes *operational state*
> (tasks, logs, workers, GitHub event mirrors), not repository contents. To read code or search a
> repo, use `gh`, `git`, or the foreman's GitHub tools (`get_github_issue`, `search_github_issues`,
> `list_github_prs`). Don't expect `read_file` or `search_code` to exist — `tools/list` is the
> authority on what is available.

## Authentication and token management

Two tiers (a debug call requires *both* credentials):

| Tier | Headers | Notes |
|------|---------|-------|
| Normal | `Authorization: Bearer <member login token \| worker token \| foreman JWT>` | Authorized against the `guild_id` argument |
| Debug | Normal Bearer **+** `X-Debug-Token: $DEBUG_TOKEN` **+** `X-Guild-Id: <guild>` | `X-Guild-Id` attributes the call to a principal for the audit log |

Behaviour worth knowing:

- Debug tools are **hidden from `tools/list`** and return 403 without a valid debug token.
- The debug token alone is never enough: without a valid Bearer you get 401 (`-32001`).
- If `DEBUG_TOKEN` is unset on the backend, debug tools are disabled entirely.
- Normal-tier calls need `guild_id` (argument); debug calls need the `X-Guild-Id` header.

Token hygiene:

```bash
# Keep secrets in env vars, never inline in scripts or shell history
export PIONEER_URL=https://<prod-host>          # placeholder: use your deployment's URL
export TOKEN=...        # your own member/worker token
export DEBUG_TOKEN=...  # only if you are authorised to use it; see debug-token.md
export GUILD=<guild-id>
```

See [debug-token.md](debug-token.md) for where the debug token comes from and its (lack of)
scoping. Never paste tokens into chats, issues, PRs or logs.

## Rate limiting and quotas

| Limit | Value | Scope |
|-------|-------|-------|
| Debug tool calls | **30 per 60 s** | Per principal, **in-memory per process** — with N backend instances the effective limit is up to N×30 |
| Over limit | HTTP 429, JSON-RPC `-32003 "Debug rate limit exceeded"` | |
| `debug_query` rows | 500 max | Per call |
| `debug_query` runtime | 30 s statement timeout | Per call |
| `list_tasks` rows | 100 max | Per call |
| `cloudflare_logs` | `limit` ≤ 200, `minutes` ≤ 1440 | Per call |

Normal-tier tools are not rate limited by this endpoint, but they hit the production DB — don't loop.
Back off on 429: wait the full 60 s window instead of retrying in a tight loop. Every debug call
(and every denial/rate-limit hit) is written to the `mcp.audit` logger with principal, tool and
arguments, so **your queries are recorded** — write them as if someone will read them.

## Safety precautions

> ⚠️ **Every tool on this server is read-only.** `debug_query` runs inside
> `SET TRANSACTION READ ONLY`; `list_tasks`/`get_task` only read. There is no write tool, so
> there is nothing to "approve" — but the data is production data.

| Operation | Allowed? | Guidance |
|-----------|----------|----------|
| `tools/list`, `ping`, `initialize` | ✅ | Always safe; start here |
| `list_tasks`, `get_task` | ✅ | Guild-scoped; prefer these first |
| `debug_query` SELECT | ✅ with care | Not guild-scoped — may return other guilds' rows. Filter by `guild_id`, use `LIMIT` |
| `INSERT/UPDATE/DELETE/DDL` | ❌ | Rejected by the DB and the validator |
| Credential tables | ❌ | Not on the allow-list |
| Remediation (cancel, redirect, followup) | ➖ Not via MCP | Use foreman tools or guild REST endpoints, deliberately, with a human's go-ahead |

Rules of thumb:

1. Start with the narrowest tool (`get_task`) before `debug_query`.
2. Always include `LIMIT` and a `WHERE` on id/state/time.
3. Don't paste raw rows (logs/messages may hold secrets or user data) into public places; summarise.
4. Never use MCP output to justify a destructive action without confirming with a human.
5. Don't fuzz tool names or probe for hidden tools.

## Common queries

All examples use this helper:

```bash
mcp() {  # usage: mcp <tool> '<json-args>'
  curl -sS "$PIONEER_URL/mcp" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Debug-Token: ${DEBUG_TOKEN:-}" \
    -H "X-Guild-Id: $GUILD" \
    -H 'content-type: application/json' \
    -d "$(jq -n --arg n "$1" --argjson a "$2" \
        '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:$n,arguments:$a}}')" \
  | jq -r '.error // .result.content[0].text'
}
```

### Discover what is available

```bash
curl -sS "$PIONEER_URL/mcp" -H "Authorization: Bearer $TOKEN" -H "X-Debug-Token: ${DEBUG_TOKEN:-}" \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name'
```

### List resources (tasks)

```bash
mcp list_tasks "$(jq -n --arg g "$GUILD" '{guild_id:$g, state:"working", limit:20}')" | jq .
mcp get_task   "$(jq -n --arg g "$GUILD" '{guild_id:$g, task_id:"t-abc123"}')" | jq .
```

### Read records (task logs, events, workers)

```bash
mcp debug_query '{"sql":"SELECT timestamp, level, line FROM task_logs WHERE task_id = '"'"'t-abc123'"'"' ORDER BY timestamp DESC LIMIT 30"}'
mcp debug_query '{"sql":"SELECT id, repos, tools FROM workers LIMIT 20"}'
```

### Search (logs and events — not code)

```bash
# Search task logs for an error string (ILIKE; bound it by time and LIMIT)
mcp debug_query '{"sql":"SELECT task_id, timestamp, line FROM task_logs WHERE line ILIKE '"'"'%rate limit%'"'"' AND timestamp > now() - interval '"'"'1 hour'"'"' ORDER BY timestamp DESC LIMIT 50"}'

# Edge/Worker logs
mcp cloudflare_logs '{"search":"error","minutes":30,"limit":50}'
```

> `--arg`/`jq` quoting gets ugly in nested SQL. For anything complex, write the JSON body to a
> temp file with a heredoc and use `curl -d @file`.

## Example workflow: investigating a production issue safely

**Scenario:** users report tasks failing since ~14:00 UTC.

1. **Scope with the least privilege.** Normal tier only:
   ```bash
   mcp list_tasks "$(jq -n --arg g "$GUILD" '{guild_id:$g, state:"failed", limit:20}')" | jq '.[] | {id,name,created_at}'
   ```
2. **Look at one failure.** `get_task` on the newest id; note `worker_id`, `branch`, `pr_url`.
3. **Escalate to debug tier only if needed**, narrowly:
   ```bash
   mcp debug_query '{"sql":"SELECT subtype, created_at FROM task_events WHERE task_id = '"'"'t-abc123'"'"' ORDER BY created_at DESC LIMIT 20"}'
   ```
4. **Find the pattern** — do failures share a worker or error text?
   ```bash
   mcp debug_query '{"sql":"SELECT worker_id, count(*) AS n FROM tasks WHERE state = '"'"'failed'"'"' AND created_at > now() - interval '"'"'6 hours'"'"' GROUP BY worker_id ORDER BY n DESC LIMIT 10"}'
   ```
5. **Check the edge** if it looks like an API/transport issue: `cloudflare_logs` with a short window.
6. **Stop and report.** Summarise: affected task ids, common factor, timeline, hypotheses. Keep
   raw rows out of the report.
7. **Remediate deliberately and outside MCP**, after human confirmation — e.g. the foreman's
   `send_followup` for a task whose worker died, or a redeploy for a bad release.

### Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `-32001` / 401 | Missing or invalid Bearer for that guild |
| `-32002` / 403 "Valid X-Debug-Token required" | Debug token missing/wrong, or `DEBUG_TOKEN` unset on backend |
| `-32602` "X-Guild-Id header is required" | Debug tool called without `X-Guild-Id` |
| `-32602` "Unknown tool" | Tool name typo — check `tools/list` (debug tools only appear with a valid debug token) |
| `-32003` / 429 | Debug rate limit hit — wait 60 s |
| `isError: true` with a validator message | SQL violated a guard (comment, non-allow-listed table, multiple statements) |
