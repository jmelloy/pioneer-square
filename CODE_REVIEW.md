# Pioneer Square — Code Review

**Date:** 2026-05-03
**Branch:** `claude/review-codebase-JXFWJ`
**Scope:** `backend/`, `frontend/`, `worker/`, root scripts and config

## Overall rating: **B / 7.5 out of 10**

The architecture is sound and the codebase shows clear care: typed WebSocket protocol, Alembic migrations, real test suites (backend 119, worker 49), structured concurrency in the worker, and documented review history in `git log`. The main risks are concentrated in a single file (`backend/main.py`, **2195 lines**, not the ~1700 the CLAUDE.md still claims) and revolve around three themes: **un-retained `asyncio.create_task` calls**, **swallowed exceptions**, and **a CORS configuration that defeats credentialed-cookie protection**. None of the issues are emergencies; most are tractable in a day or two each.

| Area | Grade | Notes |
|---|---|---|
| Architecture / data model | A− | Clean three-process split, sensible WS protocol, ID prefixing convention, soft-delete added recently. |
| Backend code quality | C+ | One mega-file; bare excepts; OAuth state in-memory; raw SQL in foreman. |
| Worker | B+ | Three-task model is well-structured. Reconnect resync is best-effort but unverified. |
| Frontend | B | Recent split landed. WS lifecycle on guild switch still leaks under fast navigation. |
| Concurrency safety | C | Multiple fire-and-forget `create_task` sites lose errors; foreman triggers are not awaited. |
| Security | C+ | CORS `*` + credentials, OAuth error reflection, OAuth CSRF state in-memory. |
| Tests | B | Good coverage on happy paths; thin on WS reconnect, foreman handoff, OAuth callback. |
| Migrations | A− | All revisions present and chained; `init_db` stamps pre-Alembic DBs. |

---

## High-severity findings

### H1. CORS allows any origin with credentials
`backend/main.py:100-106` — `allow_origins=["*"]` together with `allow_credentials=True` is a documented browser anti-pattern. Modern browsers refuse to send cookies in this combination, but `Authorization: Bearer` headers are not cookies — frontends explicitly set them and they will flow to any origin that gets a user to load a script. Tighten to an env-driven origin list (`FRONTEND_URL`, plus localhost for dev).

### H2. OAuth CSRF state lives in process memory
`backend/main.py:115` — `oauth_states: set[str] = set()`. A backend restart (or any second worker/replica) drops every in-flight state token, which both breaks legitimate logins and weakens the CSRF guarantee. Persist state to a small `oauth_states` table with a 10-minute TTL, or to Redis if multi-process becomes a thing.

### H3. OAuth exception messages reflected to clients
`backend/main.py:516, 521, 527` — `detail=f"GitHub token exchange failed: {exc}"`. Exposes traceback content (urllib3 versions, file paths, sometimes inner messages with secrets in them). Log the exception, return a generic `502` body.

### H4. Bare `except Exception:` swallows WS auth and disconnect errors
`backend/main.py:1107-1108` and `1131-1133`. The first hides DB failures during token lookup (the user is silently treated as anonymous); the second hides bookkeeping errors during disconnect cleanup. At minimum log with `logger.exception(...)`; better, narrow to the specific exceptions you expect.

### H5. Fire-and-forget `asyncio.create_task` loses Foreman errors
`backend/foreman/runner.py:393` — `asyncio.create_task(run_foreman_ai(guild_id, msg))` with no reference, no `add_done_callback`. If `run_foreman_ai` raises, Python may log a "Task exception was never retrieved" warning at GC time and the UI sees nothing. Two more sites: `backend/foreman/runner.py:415` (`reset_foreman_poll`) and the agent-stream task in `backend/main.py` (~`start_agent_run`). Wrap each in a helper that retains the task, attaches a `logger.exception` done callback, and removes itself from a registry on completion.

### H6. `_stale_worker_sweeper` and Foreman polling have no global error boundary
`backend/main.py:200+` and `backend/foreman/runner.py:343+` — both are infinite `while True` loops; if any single iteration raises an unexpected exception, the loop dies and the system silently degrades (workers stay marked online forever, foreman never polls again). Wrap each iteration body in `try/except Exception: logger.exception(...)`.

### H7. `backend/main.py` is 2195 lines and growing
Routes, OAuth flow, WS handler dispatch wrapper, agent subprocess streaming, foreman triggers, and DB session helpers are all in the same module. CLAUDE.md still says "~1700 lines"; reality is 2195 with growth slope intact. Split into `backend/routes/{auth,guilds,workers,tasks,agents}.py`, plus `backend/oauth.py`, `backend/agent_runner.py`. The existing `backend/foreman/` and `backend/ws_handlers.py` show this is already the working pattern — just finish the job.

---

## Medium-severity findings

### M1. `agent_owners` reconciliation race
`backend/main.py:1139-1146` — clean ownership check (`agent_owners.get(aid) is websocket`) but the read–check–update isn't atomic with respect to a concurrent reconnect. Under burst reconnects the new owner can be installed between the check and the DB update; we then mark its agent offline. Move the check inside a per-guild `asyncio.Lock`, or do the ownership flip and DB update in one pass.

### M2. `_gh_create_session` uses an ad-hoc DB session
`backend/main.py:537` opens a new `db = await get_db()` with manual `try/finally: await db.close()`. The codebase has both this pattern and `Depends(get_db)`. Pick one. The dependency form composes with FastAPI's exception handling and avoids the leak window when an exception fires before the `finally`.

### M3. Foreman fetches workers via raw SQL with `text()`
`backend/foreman/runner.py:426-444`. Today the only bound parameter is `:guild_id`, so it is safe — but any future maintainer adding a status filter is one f-string away from injection. Express the same query through ORM `select(...)` joined to `Agent`; SQLite supports `GROUP_CONCAT` via `func.group_concat`.

### M4. WebSocket handler holds one DB session for the connection's lifetime
`backend/main.py:1112-1165` — `db = await get_db()` outside the loop. Long-lived sessions accumulate identity-map state and pin a connection. Acquire per-message: open in `_touch_agent`, in each `dispatch` branch, and let the pool reuse it. (Aiosqlite is single-writer anyway, but a stuck handler shouldn't pin the only writer.)

### M5. Frontend WS lifecycle leaks on rapid guild switching
`frontend/src/stores/guild.ts:71-139` — module-scoped `ws`. `connectWebSocket` does close the prior socket, but if a route guard interrupts before `connectWebSocket` is called, the previous socket lives until GC. The Pinia store has no `$dispose` hook to force-close. Add an explicit `disconnectWebSocket()` and call it from `App.vue`'s `onBeforeUnmount` and from the route exit guard.

### M6. Terminal output buffers are unbounded
`frontend/src/stores/agents.ts` — terminal lines accumulate per-agent for the session's lifetime. After a multi-hour Foreman run the browser will start swapping. Cap at e.g. 5 000 lines per agent with a ring buffer; surface a "truncated" marker so users know.

### M7. Worker reconnect re-announces but doesn't verify
`worker/pioneer_worker/worker.py` — on reconnect we resend `worker-register` and `agent-state`. Backend processes the frames idempotently, but if a frame is dropped mid-handshake we never know. Add a `worker-register-ack` round-trip the worker awaits before flipping back to "registered" state; today the worker assumes success.

### M8. Foreman 300 s follow-up timeout is hard-coded and silent
`worker/pioneer_worker/worker.py` follow-up wait — the timeout auto-finalizes a task with no metric or audit log of how often this fires. Promote to config (`pioneer-worker.toml`), and emit a `task-followup-timeout` event so the foreman is informed rather than getting a silent finalize.

### M9. `task_logs` is the obvious source of unbounded growth
Persisted terminal output for every task lives in `task_logs`. There is no retention policy and no UI to compact. A worker running for weeks will grow the SQLite DB without bound. Add a periodic prune that drops `task_logs` older than N days for tasks in `done`/`failed`.

### M10. `test_stdin_inject.py` at the repo root
Stray dev script. Either move to `scripts/`, convert to a proper pytest in `backend/tests/`, or delete. It currently won't be discovered by pytest collection but is included in linting and looks like a real test.

---

## Low-severity / hygiene

- **L1.** `backend/main.py:115` — `oauth_states` is unbounded; even with M2 fix, add an upper bound or per-IP rate limit on `/auth/github/login`.
- **L2.** Many `now = datetime.now(UTC).isoformat()` repetitions — extract a `utc_now_iso()` helper. Trivial but reduces drift.
- **L3.** `backend/main.py:1190-1229` `_build_command` is safe (uses `create_subprocess_exec` with a list, no shell), but the `--max-turns 20` magic number is repeated; move to config.
- **L4.** `CLAUDE.md` line count claim is stale; either commit to splitting `main.py` and update the doc, or update the doc with the real number now.
- **L5.** `ruff` is configured but there's no CI workflow under `.github/` enforcing it. Add a minimal `lint.yml` so "Always run `ruff check . --fix && ruff format .`" actually has teeth.
- **L6.** Frontend `MAX_RETRIES` constant for WS reconnect — surface it in a tiny status pill so users know we've given up rather than just seeing a stale UI.
- **L7.** `backend/foreman/state.py` keeps in-memory conversation history trimmed to 40 messages — fine for a single-process backend, but the trimming policy (drop-oldest) silently loses the system context if a long tool-result run fills the window. Consider a `keep_first_n=4, drop_middle` strategy as token budgets get tight.

---

## Recommended improvement projects (ranked)

### Project 1 — "Concurrency safety pass" (3 days, high payoff)
- Introduce `backend/util/tasks.py` with `spawn(coro, *, name)` that retains the task in a registry, logs exceptions via `add_done_callback`, and cleans up on completion.
- Replace every `asyncio.create_task(...)` in `backend/` (≈6 sites) with `spawn`.
- Wrap `_stale_worker_sweeper` and `_poll_loop` iteration bodies in `try/except Exception: logger.exception(...)` so a single bad iteration doesn't kill the loop.
- Add a per-guild `asyncio.Lock` to `agent_owners` reconciliation.

### Project 2 — "Auth & secret hygiene" (2 days)
- Persist OAuth state to DB with TTL (drops in-memory `oauth_states`).
- Strip exception detail from `502` responses; log instead.
- CORS: env-driven allowlist, drop the `*` + credentials combo.
- Rate-limit `/auth/github/login` and `/auth/github/token`.

### Project 3 — "Split `backend/main.py`" (3 days)
- Extract `routes/auth.py`, `routes/guilds.py`, `routes/workers.py`, `routes/tasks.py`, `routes/agents.py`, plus `oauth.py` and `agent_runner.py`.
- Standardise on `Depends(get_db)` instead of manual `try/finally: await db.close()`.
- Update CLAUDE.md.

### Project 4 — "Frontend lifecycle correctness" (1 day)
- Disconnect WS on route exit / store dispose.
- Cap terminal-output buffers with a ring buffer + truncation marker.
- Surface "WS gave up reconnecting" state in UI.

### Project 5 — "Operational guardrails" (1-2 days)
- `task_logs` retention prune.
- Per-iteration error boundaries on every infinite background loop.
- CI workflow that runs `ruff check`, `pytest` (backend + worker), and `npm run type-check` + `npm test` on PRs.
- Worker `worker-register-ack` round-trip on reconnect.

---

## What's already strong

- Soft-delete on tasks landed cleanly (recent commits).
- Prompt caching on the foreman system prefix is correctly scoped.
- WebSocket protocol is documented in CLAUDE.md and matches code.
- Migrations are linear and properly chained; `init_db` handles pre-Alembic stamping.
- Recent component-split commits (PR #183) show the team is already comfortable with the kind of refactor needed for `main.py`.
- Three-runner abstraction in the worker (`claude_runner.py`, `codex_runner.py`, `pi_runner.py`) with a uniform return shape.

---

## Summary

The system is in good shape for a young multi-process workspace. The to-do list is concentrated and obvious — most of it is "finish the patterns you've already started" (extract from `main.py`, retain your tasks, persist your state). I'd prioritise the concurrency-safety pass first; everything else is easier once errors stop being silently lost.
