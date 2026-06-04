# Foreman Architecture: Task Execution & Threading Analysis

## Overview

The foreman exists in two flavors that share the same core logic:

- **Embedded foreman** (`backend/foreman/runner.py`): runs as an asyncio task inside the FastAPI/uvicorn process.
- **Standalone foreman** (`foreman/pioneer_foreman/`): a separate process that connects to the backend via WebSocket; talks to the backend exclusively through REST and WS messages.

Both invoke `run_foreman_ai()` — the embedded version accesses the DB directly; the standalone version proxies every call through `ForemanHTTPClient`.

---

## Current Execution Model

### Asyncio, not threads

The entire foreman stack is single-event-loop asyncio. The only thread usage is:

- `asyncio.to_thread(urllib.request.urlopen, ...)` for synchronous GitHub/A2A HTTP calls
- `asyncio.to_thread(docker_client.containers.run, ...)` for `spawn_worker`

There are no `threading.Thread` instances in any foreman path. All concurrency comes from `asyncio.create_task` / `spawn()`.

### Trigger points

`run_foreman_ai()` is spawned as a fire-and-forget background task on four events (via `_trigger_foreman` in `ws_handlers.py`):

| Event | Handler | Trigger |
|-------|---------|---------|
| User chat message addressed to foreman | `handle_chat` | `_trigger_foreman(..., "chat", ...)` |
| Worker `task-complete` | `handle_task_complete` | `_trigger_foreman(..., "task-complete", ...)` |
| Worker `task-followup-done` | `handle_task_followup_done` | `_trigger_foreman(..., "followup-done", ...)` |
| Worker `needs-input` escalation | `handle_needs_input` | `_trigger_foreman(..., "needs-input", ...)` |
| Worker online/offline | `handle_worker_register`, `handle_worker_disconnect` | `_trigger_foreman(...)` |
| Background poll | `_poll_loop` (per guild) | `spawn(run_foreman_ai(...))` |

`_trigger_foreman` either sends a `foreman-trigger` WS message to the external foreman (if connected) or calls `spawn(run_foreman_ai(...))` for the embedded one. `spawn()` is a thin wrapper around `asyncio.create_task` that keeps a strong reference so the task isn't GC'd.

### How multiple tasks are handled

**There is no per-guild serialization.** If three workers complete tasks within the same event-loop tick, three independent `run_foreman_ai` coroutines are created and all run concurrently. Each:

1. Fetches a fresh snapshot of guild state (workers + tasks) from the DB
2. Saves a system turn and the human-message turn to the DB
3. Loads the last `_HUMAN_TURN_WINDOW` (5) human turns from the DB
4. Runs up to `MAX_FOREMAN_ROUNDS` (10) rounds of Claude → tool-execution
5. Writes assistant and tool-result turns back to the DB

Because step 3 reads what step 2 just wrote, two concurrent runs will each see the other's newly written turns in the next call after they finish — but within a single run, they read a consistent (if slightly stale) snapshot. The main hazard is that two concurrent runs could both decide to `assign_task` the same idle worker or `send_followup` the same task; `send_followup` has a per-task distributed lock in `LockService` that prevents this, but `assign_task` and `create_task` have no such guard.

The background poll loop (`_poll_loop`) runs per guild, starting at 60 s and doubling to 3600 s. It also spawns a `run_foreman_ai` call when active tasks are found, which can overlap with an event-driven call already in progress.

---

## Per-Invocation API Call Sequence

Every `run_foreman_ai(guild_id, human_message)` call performs the following, all sequentially:

```
1. DB: read guild/workers/tasks (one query)
2. DB: _save_turn("system", ...)         ← audit-only; not sent to Anthropic
3. DB: _save_turn("user", ...)
4. DB: _load_history(...)                ← reads back recent turns

For each round (up to MAX_FOREMAN_ROUNDS=10):
  5. ANTHROPIC: client.messages.create(...)   ← biggest I/O; blocks until full response
  6. DB: _save_turn("assistant", ...)
  7. DB: _update_turn_tokens(...)
  8. WS: broadcast_msg (text blocks)
  9. WS: broadcast_msg (tool-use events)
 10. TOOL EXEC: exec_tools(tool_uses)         ← concurrent per batch (asyncio.gather)
       ├─ DB: per-tool reads/writes
       ├─ GITHUB: asyncio.to_thread(urllib...) per API call
       └─ A2A/ANTHROPIC: external agent or nested Claude call
 11. WS: broadcast_msg (tool-result events)
 12. DB: _save_turn("user", [...tool_results])

End:
 13. DB: Message.add(...) / http.save_message(...)
```

The `asyncio.gather` in step 10 is the only explicit parallelism. If Claude returns N tool calls in one response, all N execute concurrently. Within a single tool, calls are sequential except in `review_pr_internal` which uses `asyncio.gather` to fetch PR metadata and diff in parallel.

---

## Blocking / Sequential Behavior

### 1. Claude API rounds are strictly serial

Each round's `await client.messages.create(...)` must complete before the next round starts. With up to 10 rounds and typical Anthropic API latency of 2–8 seconds per call, a single foreman invocation can take 20–80+ seconds of wall-clock time before completing.

### 2. DB operations within a run are serial

Steps 2 → 3 → 4 → (6 → 7 per round) → 12 are all sequential. Each is a DB roundtrip. These are individually fast (< 5 ms on local Postgres) but add up across 10 rounds (up to ~30 extra awaits beyond the Claude calls).

### 3. GitHub multi-step tools are partially serial

`get_pr_status` makes 3 sequential `asyncio.to_thread` calls (PR data → reviews → check runs). `get_github_issue` makes 2 (issue data → comments). `_supersede_prior_bot_reviews` makes 1 REST call + 1 GraphQL call + N resolve mutations sequentially. These run in thread-pool threads so they don't block the event loop, but they do block the calling foreman run while awaiting results.

### 4. No guild-level concurrency cap

Multiple `run_foreman_ai` coroutines for the same guild can (and do) run simultaneously. There is nothing preventing a guild from having 5 concurrent foreman AI invocations, each making independent Claude API calls and DB writes.

### 5. Slow tools block subsequent rounds

`review_pr` calls an external A2A agent with a 60-second timeout. `review_pr_internal` makes a nested Anthropic API call with a 2048-token output. Either one can hold up a foreman run for 10–60 seconds at step 10, blocking the next Claude round (step 5) until tools complete.

---

## Tasks "In Flight" at Once

| Scope | Count | Notes |
|-------|-------|-------|
| Concurrent `run_foreman_ai` tasks (embedded) | Unbounded | One spawned per trigger; no cap |
| Concurrent `run_foreman_ai` tasks (standalone) | Unbounded | Same; uses `asyncio.create_task` |
| Claude API rounds per invocation | Up to 10 | `MAX_FOREMAN_ROUNDS=10`; serial |
| Tool calls per Claude round | N (all parallel) | `asyncio.gather(*coros)` |
| Worker tasks in flight | 1 per worker process | `_task_runner` is serial per worker |
| Poll loops | 1 per guild | Background task, doubles interval each cycle |

---

## Highest-Value Opportunities

### 1. Per-guild foreman serialization (highest impact)

**Problem**: Concurrent foreman invocations for the same guild read shared history, make redundant Claude calls, and can produce conflicting tool actions.

**Opportunity**: Add a per-guild `asyncio.Lock` (or `asyncio.Queue`) that ensures only one `run_foreman_ai` runs at a time per guild. New triggers arriving while a run is active are either:
  - **Queued**: buffered and replayed as a single merged message after the active run finishes.
  - **Coalesced**: the active run is given a chance to incorporate the new event in its next Claude round (by injecting a note into the message queue), and duplicate triggers during that window are dropped.

This eliminates the history-coherence race, the double-assignment hazard, and reduces redundant Claude API spend.

### 2. Streaming Claude responses

**Problem**: `client.messages.create(...)` awaits the full response before any text reaches the frontend, adding perceived latency.

**Opportunity**: Use the Anthropic streaming API (`client.messages.stream(...)`) and broadcast text tokens to the frontend as they arrive. The tool-use detection logic (currently post-hoc on `resp.content`) would need to buffer tool blocks until `stream_end`, but text blocks could stream immediately.

### 3. Parallelize multi-call GitHub tools

**Problem**: `get_pr_status` fetches PR metadata, reviews, and check runs with 3 sequential `asyncio.to_thread` calls.

**Opportunity**: Use `asyncio.gather` within the tool for independent calls:
```python
pr_data, reviews_raw, check_runs = await asyncio.gather(
    asyncio.to_thread(_gh_api, f"/repos/{repo}/pulls/{num}", token),
    asyncio.to_thread(_gh_api, f"/repos/{repo}/pulls/{num}/reviews", token),
    asyncio.to_thread(_gh_api, f"/repos/{repo}/commits/{sha}/check-runs", token),
)
```
Same optimization applies to `get_github_issue` (issue + comments), `_supersede_prior_bot_reviews` (reviews list + GraphQL), etc. This is already done in `review_pr_internal` for the PR+diff pair.

### 4. One asyncio task per in-flight task (structural change)

**Problem**: Every foreman trigger creates a new `run_foreman_ai` coroutine that reloads all history from the DB and starts from scratch. There is no memory of the conversation between triggers for the same task.

**Opportunity**: Maintain a per-task (or per-guild) long-lived asyncio task with an `asyncio.Queue` as its inbox. Each event is put onto the queue; the task's inner loop pulls from the queue and feeds messages into its conversation context. Benefits:
  - History stays in-memory between invocations (no DB roundtrip to reload turns).
  - The foreman can natively "wait" for a worker to finish then continue without a re-trigger.
  - Single reader/writer per task eliminates the concurrent-run race entirely.
  - The poll loop becomes unnecessary — the task can schedule its own wakeup.

### 5. Cap and back-pressure on concurrent foreman runs

**Problem**: A burst of task completions can spawn many simultaneous Claude API calls, driving up latency and cost.

**Opportunity**: Add a `asyncio.Semaphore` (e.g., limit 2-3 concurrent runs per guild). Excess triggers are queued and run as slots open. Simple to implement with no structural change to `run_foreman_ai`.
