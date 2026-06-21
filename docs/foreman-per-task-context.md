# Design: Per-Task Context Splitting for the Foreman

**Status:** Design only — not yet implemented.
**Issue:** [#649](https://github.com/jmelloy/pioneer-square/issues/649)
**Date:** 2026-06-21

---

## 1. Motivation & Problem Statement

### What the Foreman does today

The Foreman is a Claude agent that manages all active tasks in a single conversation
thread per guild. Every trigger — `task-complete`, `[github-event]`, `needs-input`, human
chat, and the periodic poll — is serialized through one `_processing` flag and appended to
one shared conversation history (`foreman/pioneer_foreman/foreman.py`). Each turn injects
the full `<state>` block (all workers, all tasks) into the message via
`build_state_preamble` (`runner.py:184`).

History today is loaded by **`user_id`**, not by task: `_load_history` in
`foreman/pioneer_foreman/runner.py` calls `http.get_history(user_id)` and applies a
sliding window of `_HUMAN_TURN_WINDOW = 5` non-tool-response user turns, capped to
`MAX_HISTORY_MESSAGES = 20` messages sent to Anthropic. Turns are already persisted with a
`task_id` (`_save_turn(..., task_id=task_id)`), but nothing reads them back filtered by
task — the FK is written but unused for retrieval.

### What breaks as task count grows

**Context bloat.** The state preamble grows linearly with task count. With 10 active tasks,
each Claude call carries a wall of JSON that's largely irrelevant to the specific event
being handled. Because the history window is small, real decision context gets squeezed
out by task-list noise.

**Cross-task interference.** Tool results, follow-up reasoning, and GitHub event details for
task A interleave with task B's context in the same conversation thread. A `send_followup`
intended for task A might reference task B's branch name if the history is dense enough.

**Serial throughput.** The `_processing` flag ensures exactly one foreman run at a time. If
task A's `task-complete` review triggers a 10-round Claude conversation (CI failure
analysis, redirect, follow-up), task B's `task-complete` event waits in the 100-slot
`_message_queue`. With N concurrent tasks, p99 latency scales with N × average-run-duration.

**Single point of failure.** A crash or hang during one task's review blocks all other tasks
until the connection resets and the queue is drained.

**Periodic-check noise.** The poll loop (`_poll_loop` in `foreman.py`) sends all
non-terminal tasks to the foreman every cycle (`[periodic-check] ... {task_summary}`). As
task count grows, every poll turn carries more tasks and more history, making it harder for
Claude to spot a genuinely stalled task.

---

## 2. Architecture Overview

### Model: Parent orchestrator + N child task-contexts

Each active task gets its own isolated Claude conversation thread. A lightweight parent
foreman handles cross-cutting orchestration (task creation/assignment, worker lifecycle,
human chat, routing events to the right child). Child contexts own the full lifecycle of a
single task from assignment through finalization.

```
                        ┌─────────────────────────────────────┐
  Browser ──────────────│  Backend WebSocket / REST            │
  GitHub webhooks ───── │  (unchanged; event routing lives     │
  Worker WS messages ── │   here via foreman-trigger)          │
                        └──────────────┬──────────────────────┘
                                       │  foreman-trigger messages
                                       ▼
                        ┌─────────────────────────────────────┐
                        │   Parent Foreman                    │
                        │   (one per guild, always on)        │
                        │                                     │
                        │  - Receives all triggers            │
                        │  - Routes task-specific events      │
                        │    to the right child context       │
                        │  - Handles human chat               │
                        │  - Handles worker lifecycle events  │
                        │  - Handles task creation/assignment │
                        │  - Runs periodic health check       │
                        │    (summarizes child states only)   │
                        └──────┬────────────┬────────┬────────┘
                               │            │        │
                   ┌───────────┘   ┌────────┘        └────────────┐
                   ▼               ▼                              ▼
          ┌──────────────┐ ┌──────────────┐             ┌──────────────┐
          │ Child ctx    │ │ Child ctx    │    . . .    │ Child ctx    │
          │ task t-aaa   │ │ task t-bbb   │             │ task t-zzz   │
          │              │ │              │             │              │
          │ Own history  │ │ Own history  │             │ Own history  │
          │ Own flag     │ │ Own flag     │             │ Own flag     │
          │ Own queue    │ │ Own queue    │             │ Own queue    │
          └──────────────┘ └──────────────┘             └──────────────┘
```

### What child contexts ARE

Child contexts are **separate asyncio tasks running isolated conversation threads** within
the same foreman process. They are not separate OS processes and not separate WebSocket
connections. Each child is an instance of a new `TaskContext` class that wraps:

- Its own Claude conversation history (loaded by `task_id` from the DB — the `task_id`
  column already exists on history rows and is already populated; only the *read* path
  needs a task filter).
- Its own `_processing` flag and trigger queue.
- Its own `run_foreman_ai`-style loop, but with a narrowed system prompt and state preamble
  scoped to one task.

This keeps the implementation close to the current code, avoids IPC complexity, and lets the
existing `foreman-broadcast` / `foreman-trigger` WS protocol (see `docs/foreman-split-plan.md`)
remain unchanged. The child reuses the same `ws_send` closure and the same
`ForemanHTTPClient` instance as the parent.

---

## 3. Child Context Lifecycle (creation, handoff, teardown)

### What triggers spawning a child context

A child context is spawned when the parent foreman successfully calls `assign_task` (i.e.,
when a task transitions from `pending` to `working`). This is the natural boundary: before
assignment the parent owns the task; after assignment a worker owns execution and the child
context owns the foreman's review loop.

The parent does **not** spawn a child for `create_task` alone — a task with no worker
assigned is still entirely the parent's concern.

### Initial state / system prompt the child receives

The child receives a slimmed-down system prompt built by a new
`build_child_system_blocks(task_id, task_name, worker_id, phase)`:

```
You are the Foreman AI in Pioneer Square managing a single task.

Task: {task_name} ({task_id})
Phase: {phase}
Worker: {worker_id}
Repo: {issue_repo or primary_repo}

## Your responsibilities for this task
- Monitor this task to completion
- Decide send_followup vs finalize_task after task-complete or task-followup-done
- Handle CI failures, [github-event], and needs-input for this task
- Escalate to the human (or to the parent foreman) when genuinely stuck

## Available tools
[CHILD_FOREMAN_TOOLS — FOREMAN_TOOLS minus create_task / assign_task]
```

The `<state>` preamble injected per turn (a new `build_child_state_preamble(worker_row,
task_row)`) contains **only** the assigned worker and **only** this task's row — not the
global task list. This is the primary context-size win.

### What state lives where

| State | Lives in |
|---|---|
| Task conversation history (Claude messages) | Child (DB rows filtered by `task_id`) |
| Task follow-up instructions, CI context | Child |
| Worker assignment, task name/phase | Parent (authoritative copy in DB) |
| Human chat history | Parent |
| Guild-level state (all tasks, all workers) | Parent |
| Routing map: `task_id → child handle` | Parent (in-memory dict) |

### How a child is torn down

1. **`finalize_task` is called** by the child — it completes its current Claude run, then
   removes itself from the parent's routing map and cancels its poll loop.
2. **The task reaches a terminal state** (`done`, `failed`, `cancelled`) in the DB — the
   parent's periodic health check detects this and cancels any orphaned child context.
3. **The parent reconnects** after a crash — it re-enumerates non-terminal tasks from
   `GET /guilds/{id}/foreman/state` and re-spawns child contexts for those that are not
   terminal (see §6).
4. **Abandonment** — if a child has been idle (no events, no activity) longer than a
   configurable TTL (default 2 hours), it tears itself down and logs a warning.

---

## 4. Parent ↔ Child Communication & Aggregation

### Handoff

After `assign_task` succeeds, the parent:

1. Instantiates `TaskContext(task_id, worker_id, task_name, phase, http, ws_send, config)`.
2. Starts it as an `asyncio.Task` (`asyncio.create_task(child.run())`).
3. Registers it in `self._child_contexts: dict[str, TaskContext]`.
4. Flushes any pre-spawn buffered triggers for that `task_id` (see §5) into the child queue.

### How children report back

Children do **not** call back into the parent object directly:

- **Normal chat/broadcasts** go through the shared `ws_send` / `foreman-broadcast`
  mechanism, identical to today.
- **Finalization** — the child calls `finalize_task` via `/exec_tool` (no parent
  involvement), then signals its own teardown.
- **Escalation** uses a shared `asyncio.Queue[EscalationRequest]` that the parent drains
  after each of its own turns.

### How the parent surfaces aggregate status to the human

All authoritative state is in the DB, so the parent does not query child memory. On a human
status query it:

1. Reads the full task list via `GET /guilds/{id}/foreman/state` (unchanged).
2. Annotates each task with whether a live child context exists in `_child_contexts`.
3. Summarizes and responds — same behavior as today, just with no cross-task tool-call
   history polluting its own context.

### Escalation

A child that cannot decide (e.g., a `needs-input` it can't answer) enqueues:

```python
@dataclass
class EscalationRequest:
    task_id: str
    task_name: str
    reason: str            # human-readable summary
    trigger_payload: dict  # original trigger so the parent can re-handle it
```

The parent drains this queue after each of its own turns, handles the escalation with full
guild state, and — if resolved — re-enqueues the resolved trigger back to the child via
`child.enqueue(resolved_payload)`.

---

## 5. Event Routing

### Events that go to the parent

| Event | Reason |
|---|---|
| Human chat messages | Parent owns the human conversation |
| `[worker-online]` / `[worker-offline]` | Cross-cutting; may affect multiple tasks |
| `create_task` + `assign_task` flows | Parent spawns the child after assignment |
| `[periodic-check]` poll | Parent runs the health check, delegating per-task checks |
| Any trigger with no matching `task_id` | Default: parent handles unknown-task events |

### Events that go to the child (routing key = `task_id`)

`task-complete`, `task-followup-done`, `needs-input`, `[github-event]` for a PR belonging to
a task, and `check_run` CI success/failure. The `taskId` is already present on
`foreman-trigger` payloads (`_handle_trigger` reads `data.get("taskId")` today), so routing
needs no new backend protocol.

### Routing algorithm

```python
def route_trigger(self, trigger: dict) -> None:
    task_id = trigger.get("taskId")
    if task_id and task_id in self._child_contexts:
        child = self._child_contexts[task_id]
        if child.is_alive():
            child.enqueue(trigger)
            return
    # Task assigned but child not yet spawned: buffer for flush-on-spawn.
    if task_id and task_id in self._pending_for_task:
        self._pending_for_task[task_id].append(trigger)
        return
    # Fall through to the parent.
    self._parent_dispatch(trigger)
```

### Pre-spawn race

There is a small window between `assign_task` completing (backend sets task to `working`)
and the child being registered. A `_pending_for_task: dict[str, list[dict]]` buffer in the
parent holds triggers for that `task_id`; the parent flushes them into the child's queue at
the end of handoff (§4). Triggers are buffered, never dropped.

---

## 6. Failure Modes & Recovery

### Child context crashes or stalls

**Crash.** The asyncio task raises an unhandled exception. The parent's task-done callback
logs the error, removes the child from `_child_contexts`, and `ws_send`s an alert to the
human. The DB task is left in its last-known state; the parent's next periodic check notices
it's non-terminal with no live child and either re-spawns it or surfaces it for human review.

**Stall.** The child's `_processing` flag stays `True` longer than `child_stall_timeout`
(configurable, default 5 minutes). A per-child watchdog cancels the in-flight Claude call,
clears the flag, logs the stall, and resumes draining the queue.

### Parent crash while children are active

The process exits; all child asyncio tasks are cancelled. On reconnect (`foreman-registered`):

1. The parent reads all non-terminal tasks from `GET /guilds/{id}/foreman/state`.
2. For each non-terminal task with a `worker_id`, it spawns a fresh child context.
3. Each new child sends itself a synthetic `[reconnect]` trigger: "I just reconnected; the
   task was in state `{state}`. Check if it still needs action."
4. The child loads its DB-backed history (filtered by `task_id`) on its first turn —
   continuity is preserved because history is persisted, not in-memory.

### Worker goes offline while a child holds a task

The parent receives `[worker-offline]` and notifies the affected child via
`child.enqueue(worker_offline_trigger)`. The child decides whether to `send_followup` to a
different worker or escalate. If no other worker is available, it escalates through the
`EscalationQueue`.

---

## 7. Implementation Path

### Phase 0 — Plumbing (no behavior change)

1. **Verify `task_id` population.** Confirm the `task_id` column on history rows is
   populated for all trigger types (it is written by `_save_turn` today; audit that callers
   always pass it).
2. **Task-scoped history read.** Add an optional `task_id` filter to `get_history`
   (`http_client.py`) and the backing `GET /guilds/{id}/foreman/history` endpoint, then a
   `_load_history(..., task_id=...)` path in `runner.py` that loads only that task's turns.
3. **`TaskContext` skeleton** in `foreman/pioneer_foreman/task_context.py`: wraps a
   `task_id`, with `enqueue()`, `run()`, `is_alive()`, and a `_processing` flag mirroring the
   parent's run/drain loop.
4. **Routing map + escalation queue** on `Foreman` (`foreman.py`).

### Phase 1 — Spawn children on assign_task

5. **Intercept successful `assign_task` results** in the parent's run loop; spawn a
   `TaskContext` for the returned `task_id`.
6. **`build_child_system_blocks`** and **`build_child_state_preamble`** in
   `foreman/pioneer_foreman/prompt.py` (and the shared `backend/foreman_core/prompt.py`).
7. **`CHILD_FOREMAN_TOOLS`** in `backend/foreman_core/tools_schema.py` = `FOREMAN_TOOLS`
   minus `create_task` / `assign_task`.

### Phase 2 — Route events to children

8. **Apply the routing map** in `_handle_trigger` using the existing `taskId`.
9. **`_pending_for_task` buffer** + flush-on-spawn.
10. **Child teardown** on `finalize_task` (deregister, cancel poll loop).

### Phase 3 — Escalation & recovery

11. **`EscalationQueue`** drained by the parent after each of its turns.
12. **Reconnect re-spawn** on `foreman-registered`.
13. **Per-child stall watchdog.**

### Phase 4 — Observability & tuning

14. **Per-child token logging** to compare child vs. parent context sizes.
15. **`child_context_count`** added to `foreman-poll-status` broadcasts so the frontend can
    show "N task contexts active".
16. **Independent history caps** — parent may want a smaller `MAX_HISTORY_MESSAGES`;
    long-running children may want larger.

### New files

| File | Purpose |
|---|---|
| `foreman/pioneer_foreman/task_context.py` | `TaskContext` — the child context runner |
| `foreman/pioneer_foreman/escalation.py` | `EscalationRequest` dataclass + queue helpers |

### Files to modify

| File | Change |
|---|---|
| `foreman/pioneer_foreman/foreman.py` | Routing map, child spawn/teardown, escalation drain, pre-spawn buffer |
| `foreman/pioneer_foreman/runner.py` | `task_id`-filtered history loader; `child=True` flag to narrow state |
| `foreman/pioneer_foreman/prompt.py` | `build_child_system_blocks`, `build_child_state_preamble` |
| `foreman/pioneer_foreman/http_client.py` | `task_id` param on `get_history` |
| `backend/foreman_core/tools_schema.py` | Export `CHILD_FOREMAN_TOOLS` |
| `backend/routes/foreman.py` | `task_id` filter on the history endpoint |

The embedded foreman (`backend/foreman/`) follows the same design with `TaskContext` objects
running inside the backend process; the class can be shared via `backend/foreman_core/`. Note
that `docs/foreman-split-plan.md` Phase 5 anticipates removing the embedded foreman entirely,
so this design should be prototyped in the standalone foreman first (see open question 7).

---

## 8. Open Questions

1. **Re-spawning children for pre-existing tasks.** On reconnect the parent sees tasks
   already `working` / `awaiting-review`. Re-spawning children is good for reliability but
   requires the child to reconstruct context from DB history alone — is that enough, or does
   the child need a richer "resumption briefing"?

2. **Child tool set.** The proposal excludes `create_task` / `assign_task` from children. But
   a child handling a complex task might legitimately want to spawn a sub-task. Should
   children be allowed `create_task` → `assign_task` with `parent_task_id`? Does that spawn a
   grandchild context?

3. **Periodic health check.** With per-task contexts the parent's poll only needs to find
   orphaned tasks (non-terminal, no live child). Should the parent also tick each child with
   an "are you still alive?", or is the stall watchdog sufficient?

4. **Follow-up history.** `send_followup` reuses the same task and branch. Should the child's
   history accumulate across the original task and all follow-ups (same `task_id`), or should
   each follow-up start fresh? Accumulating gives useful prior context; fresh avoids bloat on
   long-lived tasks.

5. **Concurrency cap.** There is no limit in this design. With 20 tasks, 20 child contexts
   could hit the Anthropic API simultaneously. Should the parent rate-limit child spawning or
   use a semaphore on Claude calls?

6. **Parent history partitioning.** The parent's own history grows as it handles assignment,
   human chat, and escalations. Is the existing sliding window enough, or should the parent's
   history be partitioned by topic?

7. **Embedded vs. standalone divergence.** Both share `backend/foreman_core/` but have
   different runner entry points. Prototype in the standalone foreman first, then port — or
   update both in parallel? (See `docs/foreman-split-plan.md` Phase 5.)

8. **WS message attribution.** Today all foreman chat carries `"from": "foreman"`. With child
   contexts, should messages carry a `taskId` so the frontend can show "Foreman [task t-aaa]"
   vs. "Foreman [parent]"? This is a frontend + WS-protocol change.
