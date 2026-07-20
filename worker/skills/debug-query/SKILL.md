---
name: debug-query
description: Run read-only SQL against the Pioneer Square backend's operational tables (tasks, task_logs, task_events, workers, agents, github_events, llm_usage, foreman_turns) via its DEBUG_TOKEN-gated /debug/query endpoint. Use this when debugging a task/worker/agent issue and you need to inspect live backend state instead of guessing from logs alone. Only available when DEBUG_TOKEN is set in the environment.
---

# Debug query

Lets you run a read-only `SELECT` against the backend's operational tables over
HTTP, without shelling into the database. Backed by `backend/routes/debug_query.py`.

## Before using

Check `DEBUG_TOKEN` is set:

```bash
test -n "$DEBUG_TOKEN" && echo available
```

If it's empty, this skill is not usable in this environment — don't attempt the
query, and don't ask the user for the token.

## Usage

```bash
"$PIONEER_SKILL_DIR/scripts/query.sh" "SELECT id, state, worker_id FROM tasks WHERE state = 'working' LIMIT 10"
```

`PIONEER_SKILL_DIR` is set in the worker image to this skill's own directory, so
it resolves correctly no matter which repo's worktree is the current working
directory. If it's unset (e.g. running this skill outside the worker image),
use the path relative to this file instead: `scripts/query.sh "..."`.

Prints the JSON array of matching rows on success, or a JSON error body (with a
non-zero exit code) on failure.

## Constraints (enforced server-side, not by this script)

- Single `SELECT` statement only — no `INSERT`/`UPDATE`/`DELETE`/DDL, no
  comments, no semicolon-chained statements.
- Only these tables: `tasks`, `task_logs`, `task_events`, `workers`, `agents`,
  `github_events`, `llm_usage`, `foreman_turns`. No credential-bearing tables
  (`github_tokens`, `user_sessions`, etc.) are exposed.
- Results are capped at 500 rows and the query is capped at 2 seconds.
- Not guild-scoped — a query can return rows across every guild.

## Example

```bash
"$PIONEER_SKILL_DIR/scripts/query.sh" \
  "SELECT id, subtype, created_at FROM task_events WHERE task_id = 't-abc123' ORDER BY created_at DESC LIMIT 20"
```
