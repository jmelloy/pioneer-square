# Design: Per-Task Context Splitting for the Foreman

*Branch: claude/design-foreman-per-task-context-splitting-t-fgun*  
*Date: 2026-06-14*

---

## 1. Motivation & Problem Statement

### What the Foreman does today

The Foreman is a Claude agent that manages all active tasks in a single conversation thread per guild. Every trigger — `task-complete`, `github-event`, `needs-input`, human chat, and the periodic poll — is serialized through one `_processing` lock and appended to one shared conversation history. Each turn injects the full `<state>` block (all workers, all tasks) into the user message.

### What breaks as task count grows

**Context bloat.** The state preamble grows linearly with task count. With 10 active tasks, each Claude call carries a wall of JSON that's largely irrelevant to the specific event being handled. Because `MAX_HISTORY_MESSAGES = 20` and `_HUMAN_TURN_WINDOW = 5`, real decision context gets squeezed out by task-list noise.

**Cross-task interference.** Tool results, follow-up reasoning, and GitHub event details for task A interleave with task B's context in the same conversation thread. The foreman's reasoning about "which worker to assign" or "does this PR need a follow-up" can be polluted by unrelated prior turns. A `send_followup` intended for task A might reference task B's branch name if the history is dense enough.

**Serial throughput.** The `_processing` lock ensures exactly one foreman run at a time. If task A's `task-complete` review triggers a 10-round Claude conversation (CI failure analysis, redirect, follow-up), task B's `task-complete` event waits in a 100-slot queue. With N concurrent tasks, p99 latency scales with N × average-run-duration.

**Single point of failure.** A crash or hang during one task's review blocks all other tasks until the connection resets and the queue is drained.

**Periodic-check noise.** The poll loop sends all non-terminal tasks to the foreman every cycle. As task count grows, every poll turn carries more tasks and more history, making it harder for Claude to spot a genuinely stalled task among the crowd.

---

## 2. Architecture Overview

### Model: Parent orchestrator + N child task-contexts

Each active task gets its own isolated Claude conversation thread. A lightweight parent foreman handles cross-cutting orchestration (initial task creation/assignment, worker lifecycle, human chat, routing events to the right child). Child contexts handle the full lifecycle of a single task from assignment through finalization.

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
          │ Own lock     │ │ Own lock     │             │ Own lock     │
          │ Own queue    │ │ Own queue    │             │ Own queue    │
          └──────────────┘ └──────────────┘             └──────────────┘
```

### What child contexts ARE

Child contexts are **separate asyncio tasks running isolated conversation threads** within the same foreman process. They are not separate OS processes and not separate WebSocket connections. Each child is an instance of a new `TaskContext` class that wraps:

- Its own Claude conversation history (keyed by `task_id` in the DB — the `task_id` FK already exists on `messages` rows)
- Its own `_processing` flag and trigger queue
- Its own `run_foreman_ai`-style loop, but with a narrowed system prompt focused on one task

This keeps the implementation close to the current code, avoids IPC complexity, and lets the existing `foreman-broadcast` / `foreman-trigger` WS protocol remain unchanged.

---

## 3. Child Context Lifecycle

### What triggers spawning a child context

A child context is spawned when the parent foreman calls `assign_task` (i.e., when a task transitions from `pending` to `working`). This is the natural boundary: before assignment the parent owns the task; after assignment a worker owns execution and the child context owns the foreman's review loop.

The parent does **not** spawn a child for `create_task` alone — a task with no worker assigned is still entirely the parent's concern.

### Initial state / system prompt the child receives

The child receives a slimmed-down system prompt:

```
You are the Foreman AI in Pioneer Square managing a single task.

Task: {task_name} ({task_id})
Phase: {phase}
Worker: {worker_id}
Repo: {issue_repo or primary_repo}

## Your responsibilities for this task
- Monitor this task to completion
- Decide send_followup vs finalize_task after task-complete or task-followup-done
- Handle CI failures, github-event, and needs-input for this task
- Escalate to the human (or to the parent foreman) when genuinely stuck

## Available tools
[same FOREMAN_TOOLS set, but create_task / assign_task are excluded]
```

The `<state>` preamble injected per turn contains **only** the assigned worker and **only** this task's row — not the global task list. This is the primary context-size win.

### What task-specific state lives in the child vs. the parent

| State | Lives in |
|---|---|
| Task conversation history (Claude messages) | Child (DB rows tagged `task_id`) |
| Task follow-up instructions, CI context | Child |
| Worker assignment, task name/phase | Parent (also in DB) |
| Human chat history | Parent |
| Guild-level state (all tasks, all workers) | Parent |
| Routing map: task_id → child handle | Parent (in-memory dict) |

### How a child is torn down

A child context is torn down when:

1. **`finalize_task` is called** by the child — the child completes its current Claude run, then removes itself from the parent's routing map and cancels its poll loop.
2. **The task transitions to a terminal state** (`done`, `failed`, `cancelled`) in the DB — the parent's periodic health check detects this and cancels orphaned child contexts.
3. **The parent foreman reconnects** after a crash — it re-enumerates non-terminal tasks from `GET /guilds/{id}/state` and re-spawns child contexts for tasks that are not yet terminal.
4. **Abandonment** — if a child context has been idle (no events, no activity) for longer than a configurable TTL (e.g., 2 hours), it tears itself down and logs a warning.

---

## 4. Parent ↔ Child Communication

### How the parent hands off a task to a child

After calling `assign_task`, the parent:

1. Instantiates a `TaskContext(task_id, worker_id, task_name, phase, ...)` object.
2. Starts it as an `asyncio.Task` (`asyncio.create_task(child.run())`).
3. Registers it in a dict: `self._child_contexts: dict[str, TaskContext]`.

The child receives its initial trigger immediately via `child.enqueue(trigger_payload)` — typically the same `task-assigned` confirmation or a synthetic "task is now working" message.

### How child contexts report status back to the parent

Children do **not** call back into the parent object. Instead:

- **Normal chat/broadcasts** go through the existing `ws_send` / `foreman-broadcast` mechanism, identical to today. The child gets the same `ws_send` closure the parent uses.
- **Escalation** (see §4.4) uses a shared `asyncio.Queue[EscalationRequest]` that the parent drains on each of its own turns.
- **Finalization** — the child calls `finalize_task` via the backend REST API (no parent involvement), then signals its own teardown.

### How the parent surfaces aggregate status to the human

The parent keeps the routing map. When a human sends a status query ("what's happening?"), the parent:

1. Reads the DB for the full task list (unchanged from today).
2. Annotates each task with whether a live child context exists.
3. Summarizes and responds (same behavior as today, just no cross-task tool call history to confuse it).

The parent does **not** need to query child contexts in memory — all authoritative state is in the DB.

### How escalation works

A child context that cannot decide (e.g., "the worker sent needs-input and I don't know the right answer") enqueues an `EscalationRequest`:

```python
@dataclass
class EscalationRequest:
    task_id: str
    task_name: str
    reason: str          # human-readable summary
    trigger_payload: dict  # original trigger so parent can re-handle it
```

The parent's event loop drains this queue after each of its own turns. It then handles the escalation as a normal trigger in its own context, with access to the full guild state. If the parent resolves it (e.g., answers the `needs-input`), it re-enqueues the resolved trigger back to the child via `child.enqueue(resolved_payload)`.

---

## 5. Event Routing

### Events that go to the parent

| Event | Reason |
|---|---|
| Human chat messages | Parent owns the human conversation |
| `worker-online` / `worker-offline` | Cross-cutting; may affect multiple tasks |
| `create_task` + `assign_task` requests | Parent spawns the child after assignment |
| `periodic-check` poll | Parent runs the health check, delegating per-task checks to children |
| Any trigger with no matching `task_id` | Default: parent handles unknown task events |

### Events that go to the child

| Event | Routing key |
|---|---|
| `task-complete` | `task_id` in trigger payload |
| `task-followup-done` | `task_id` |
| `needs-input` | `task_id` |
| `[github-event]` for a PR belonging to a task | `task_id` extracted from event body |
| `check_run` CI failure/success | `task_id` (already in event payload from backend) |

### Event routing algorithm

```python
def route_trigger(trigger: dict) -> None:
    task_id = trigger.get("task_id")
    if task_id and task_id in self._child_contexts:
        child = self._child_contexts[task_id]
        if child.is_alive():
            child.enqueue(trigger)
            return
    # Fall through to parent
    self._parent_queue.put_nowait(trigger)
```

### What happens to events that arrive before a child context is ready

There is a small window between `assign_task` completing (backend sets task to `working`) and the child context being initialized. During this window:

- The parent holds the trigger in its own queue (not dropped).
- Once `TaskContext.__init__` completes and the child is registered, the parent flushes pending triggers for that `task_id` into the child's queue.
- A `_pending_for_task: dict[str, list[dict]]` buffer in the parent covers this race.

---

## 6. Failure Modes & Recovery

### Child context crashes or stalls

**Crash**: The asyncio task raises an unhandled exception. The parent's task-done callback detects this, logs the error, removes the child from `_child_contexts`, and sends a `ws_send` alert to the human. The underlying task in the DB is left in its last known state; the parent's next periodic check will notice it's non-terminal without a live child and either re-spawn the child or surface it for human review.

**Stall**: The child's `_processing` flag stays True for longer than `child_stall_timeout` (configurable, default 5 minutes). A watchdog coroutine in the child cancels the in-flight Claude API call, sets `_processing = False`, and logs the stall. The child then continues draining its queue normally.

### Parent crash while children are active

The parent process exits; all asyncio tasks (child contexts) are cancelled. On reconnect:

1. The parent calls `GET /guilds/{id}/state` and reads all non-terminal tasks.
2. For each non-terminal task that has a `worker_id`, it spawns a new child context.
3. Each new child sends a synthetic `[reconnect]` trigger to itself: "I just reconnected; the task was in state `{state}`. Check if it still needs action."
4. The DB conversation history for each `task_id` is loaded by the child on its first turn — continuity is preserved because history is DB-backed, not in-memory.

### Worker goes offline while a child holds tasks

This is handled the same way today: the parent receives `[worker-offline]` and escalates. Under the new design, the parent notifies the affected child context via `child.enqueue(worker_offline_trigger)`. The child then decides whether to call `send_followup` to a different worker or escalate to the parent. If no other worker is available, the child escalates via the `EscalationQueue`.

---

## 7. Implementation Path

### Phase 0 — Plumbing (no behavior change)

1. **Tag all existing DB history rows** by `task_id` already happens (the FK is there); verify the `task_id` column is populated consistently for all trigger types in the backend.
2. **Add `TaskContext` skeleton** in `foreman/pioneer_foreman/task_context.py`: wraps a `task_id`, has `enqueue()`, `run()`, `is_alive()`. On `run()`, it calls `run_foreman_ai` with a task-scoped history loader.
3. **Narrow the history loader**: add a `task_id` filter to `_load_history` in `runner.py` so a child only loads messages tagged with its own `task_id`.
4. **Add routing map + escalation queue** to `Foreman` in `foreman.py`. Routing logic: if `task_id in self._child_contexts`, enqueue to child; else enqueue to parent.

### Phase 1 — Spawn children on assign_task

5. **Intercept `assign_task` tool results** in the parent's tool executor. When a tool result indicates successful assignment, spawn a `TaskContext` for the returned `task_id`.
6. **Narrow child system prompt** in `foreman/pioneer_foreman/prompt.py`: add `build_child_system_blocks(task_id, task_name, worker_id, phase)` that omits guild-wide state.
7. **Narrow child state preamble**: `build_child_state_preamble(worker_row, task_row)` — only the one worker and one task.
8. **Exclude `create_task` / `assign_task` from child tool list**: add a `child_tools` filter in `tools_schema.py`.

### Phase 2 — Route events to children

9. **Parse `task_id` from all trigger payloads** in `_handle_trigger` and apply the routing map.
10. **Buffer pre-spawn triggers**: add `_pending_for_task` dict to `Foreman`; flush into child on spawn.
11. **Implement child teardown**: `TaskContext.on_finalize()` deregisters from parent map and cancels its own poll loop.

### Phase 3 — Escalation & recovery

12. **Implement `EscalationQueue`**: shared `asyncio.Queue`; parent drains it after each of its own turns.
13. **Implement reconnect re-spawn**: on `foreman-registered`, enumerate non-terminal tasks and spawn children.
14. **Implement stall watchdog**: per-child coroutine that cancels a hung Claude call after `child_stall_timeout`.

### Phase 4 — Observability & tuning

15. **Log per-child token usage** separately so you can compare child vs. parent context sizes over time.
16. **Add `child_context_count` to `foreman-poll-status`** broadcasts so the frontend can show "N task contexts active".
17. **Tune `MAX_HISTORY_MESSAGES`** independently for parent (can be smaller) and children (may need larger for long-running tasks).

### New modules / files

| File | Purpose |
|---|---|
| `foreman/pioneer_foreman/task_context.py` | `TaskContext` class — the child context runner |
| `foreman/pioneer_foreman/escalation.py` | `EscalationRequest` dataclass + queue helpers |

### Files to modify

| File | Change |
|---|---|
| `foreman/pioneer_foreman/foreman.py` | Add routing map, routing logic, child spawn/teardown, escalation drain |
| `foreman/pioneer_foreman/runner.py` | Add `task_id`-filtered history loader; accept `child=True` flag to narrow state |
| `foreman/pioneer_foreman/prompt.py` | Add `build_child_system_blocks`, `build_child_state_preamble` |
| `backend/foreman_core/tools_schema.py` | Export `CHILD_FOREMAN_TOOLS` (FOREMAN_TOOLS minus create/assign) |

The embedded foreman in `backend/foreman/` follows the same design but the `TaskContext` objects run inside the backend process. The same `TaskContext` class can be shared via `backend/foreman_core/`.

---

## 8. Open Questions

1. **Should the parent spawn a child for tasks it didn't create this session?** On startup / reconnect, the parent sees tasks already in `working` or `awaiting-review` state. Re-spawning children for these is good for reliability but requires the child to reconstruct context from DB history alone — is that reliable enough, or does the child need a richer "resumption briefing"?

2. **What is the right child tool set?** The current proposal excludes `create_task` and `assign_task` from children. But a child handling a complex task might legitimately want to spawn a sub-task (e.g., "create a review task for the PR I just saw"). Should children be allowed to call `create_task` → `assign_task` with `parent_task_id` set? If so, does that spawn a grandchild context?

3. **How should the periodic health check change?** Today the poll loop sends all active tasks to one foreman turn. With per-task contexts, the parent's poll only needs to check for orphaned tasks (non-terminal tasks with no live child). Should the parent also send each child a periodic "are you still alive?" tick, or is the stall watchdog sufficient?

4. **History isolation for follow-ups.** `send_followup` re-uses the same task and the same branch. Should the child's conversation history accumulate across the original task and all follow-ups (same `task_id`), or should each follow-up start a fresh child context? Accumulating gives the child useful prior context; starting fresh avoids context bloat on long-lived tasks.

5. **Concurrency cap.** How many concurrent child contexts is acceptable? There's no limit in this design. With 20 tasks, 20 child contexts could all hit the Anthropic API simultaneously. Should the parent rate-limit child spawning or use a semaphore on Claude API calls?

6. **Parent conversation history.** The parent's own history will grow over time as it handles assignment, human chat, and escalations. Should the parent's history be partitioned (e.g., one thread per human conversation topic), or is the existing 5-human-turn sliding window sufficient?

7. **Embedded vs. standalone foreman divergence.** The embedded foreman (`backend/foreman/`) and the standalone foreman (`foreman/pioneer_foreman/`) share `foreman_core` but have different runner entry points. Should both be updated in parallel, or should this design be prototyped in the standalone foreman first and then ported?

8. **WebSocket message attribution.** Today all foreman chat messages have `"from": "foreman"`. With child contexts, should messages carry a `taskId` field so the frontend can show "Foreman [task t-aaa]" vs. "Foreman [parent]"? This is a frontend change but affects the WS protocol design.
