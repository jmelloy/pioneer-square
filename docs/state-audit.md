# Pioneer Square — Worker / Agent / Task State Audit

> Generated: 2026-05-26 · Verified at commit: fc64c4478e91949438d4467238671baf69d401ac
>
> Point-in-time snapshot, not living documentation — re-grep the named file/function in
> current code rather than trusting exact line numbers, and read "X is vestigial/legacy"
> style observations as true as of the date above, not as permanent verdicts.

## Executive Summary

Pioneer Square manages state across three hierarchical levels:

1. **Workers** — execution hosts that register with the backend
2. **Agents** — per-process WebSocket identities owned by a worker (one agent per worker process lifetime)
3. **Tasks** — discrete work items assigned to workers

Relationship: `Guild → Workers → Agents` (one agent per live worker process; tasks are assigned to a worker, not directly to an agent).

---

## 1. Worker States

### Defined Values

| State | Default? | Set By | Terminal? |
|-------|----------|--------|-----------|
| `idle` | ✓ | Schema / ORM default | No |
| `online` | No | `handle_join` on WebSocket connect | No |
| `offline` | No | Disconnect handler, startup reset, stale sweeper | No |

*Defined in* `backend/models.py` (`Worker.state`, Python default `"idle"`) *and* `backend/alembic/versions/20260428_000000_initial_schema.py` (`workers.state` TEXT, `server_default='idle'`).

### State Transition Locations

| Transition | To State | File & Function | Trigger |
|-----------|----------|-----------------|---------|
| Worker WebSocket joins | `online` | `backend/ws_handlers.py` — `handle_join` | `join` WS message |
| Worker WebSocket disconnects | `offline` | `backend/ws_handlers.py` — `handle_worker_disconnect` | WS close event |
| Startup reset | `offline` | `backend/main.py` — `reset_connection_state` | App startup — resets all workers |
| Stale-worker sweeper | `offline` | `backend/main.py` — `_sweep_stale_workers_once` | Background sweeper task |

### Notes

- Worker state is coarse (online/offline/idle only); fine-grained "what is it doing" lives in agent state (below). The frontend has no separate worker vocabulary — `frontend/src/stores/agents.ts` derives a display state from the highest-priority agent state for that worker.
- `backend/worker_lifecycle.py` (added after this audit) layers drain/reconcile behavior on top of the same three-value column, without adding new state values.

---

## 2. Agent States

### Defined Values

TypeScript union (authoritative frontend type):
```typescript
// frontend/src/types.ts
type AgentState = 'idle' | 'thinking' | 'working' | 'busy' | 'error' | 'offline'
```

| State | Default? | Set By | Notes |
|-------|----------|--------|-------|
| `idle` | ✓ | Join, task completion, error recovery | Normal ready state |
| `working` | No | Task begins execution | Primary active state |
| `thinking` | No | Reserved / mock worker | Not currently emitted by production worker |
| `busy` | No | Reserved / mock worker | Not currently emitted by production worker |
| `error` | No | Claude subprocess crash or timeout | Triggers task lock release |
| `offline` | No | WS close, stale sweeper, startup reset | Triggers task lock release |

### Complementary Field: `activity`

A granular sub-state refining `working`, emitted alongside `agent-state` messages and cleared on offline/idle. Now persisted on `agents.activity` (migration `20260501_000000_add_activity_to_agents`) — no longer memory-only as an earlier version of this audit assumed.

```typescript
// frontend/src/types.ts
type AgentActivity = 'reading' | 'editing' | 'running' | 'searching' | 'fetching' | 'thinking' | 'planning'
```

Mapping from Claude tool names to activities (`worker/pioneer_worker/claude_runner.py`):

| Tool | Activity |
|------|----------|
| `Bash` | `running` |
| `Read`, `TodoRead` | `reading` |
| `Write`, `Edit`, `MultiEdit`, `NotebookEdit` | `editing` |
| `WebSearch` | `searching` |
| `WebFetch` | `fetching` |
| `Agent`, `TodoWrite` | `planning` |
| Thinking block | `thinking` |

*Defined in* `backend/models.py` (`Agent.state`), the same initial-schema migration (`agents.state`, `server_default='idle'`), and `frontend/src/types.ts` (`AgentState`).

### State Transition Locations

| Transition | To State | File & Function | Trigger |
|-----------|----------|-----------------|---------|
| Agent joins | `idle` | `backend/ws_handlers.py` — `handle_join` (upsert) | `join` WS message |
| Task begins | `working` | `worker/pioneer_worker/worker.py` — `_set_state("working", agent)` | Task dispatch |
| Claude crash | `error` | `worker/pioneer_worker/worker.py` — multiple `_set_state("error", ...)` call sites | Subprocess failure / timeout |
| Task completes successfully | `idle` | `worker/pioneer_worker/worker.py` — `_set_state("idle", ...)` call sites | End of task execution |
| WS closes (worker side) | `offline` | `worker/pioneer_worker/worker.py` — `_set_state("offline", ...)` | Connection loss |
| WS closes (backend side) | `offline` | `backend/ws_handlers.py` — `handle_worker_disconnect` | Connection handler |
| Stale sweeper | `offline` | `backend/main.py` — `_sweep_stale_workers_once` | Background sweeper task |
| Startup reset | `offline` | `backend/main.py` — `reset_connection_state` | App startup |
| Explicit WS message | any | `backend/ws_handlers.py` — `handle_agent_state` | `agent-state` message from worker |

### Lock-Release States

```python
# backend/ws_handlers.py
_LOCK_RELEASE_AGENT_STATES = frozenset({"idle", "offline", "error", "timeout"})
```

When an agent enters one of these states, the backend checks whether it owns a `working` task and, if so, moves it to `awaiting-review` (guarded so the task's `worker_id` must match the agent's, to avoid a stale `current_task_id` releasing the wrong lock).

> **`timeout` state discrepancy (still present):** `_LOCK_RELEASE_AGENT_STATES` includes `"timeout"` but the TypeScript `AgentState` union does **not** list it, and no code path in worker or backend emits `agent-state: timeout`. It appears to be a defensive placeholder. Still worth a decision: confirm an emitting path and add it to the union, or drop it from the constant.

**Frontend priority ranking** — `frontend/src/stores/agents.ts` derives one display state per worker from its most-active agent via `STATE_RANK = { working: 0, thinking: 1, busy: 2, error: 3, idle: 4, offline: 5 }` (lower = higher priority).

---

## 3. Task States

### Defined Values

TypeScript union (authoritative frontend type):
```typescript
// frontend/src/types.ts
type TaskState =
  | 'pending'         // newly created, awaiting dispatch
  | 'planning'        // foreman planning phase (rare)
  | 'working'         // actively executing on a worker
  | 'awaiting-review' // waiting for foreman review / follow-up
  | 'done'            // finalized (terminal)
  | 'failed'          // task failed (terminal)
  | 'followup'        // follow-up phase (legacy/rare)
  | 'cancelled'       // cancelled by user (terminal)
```

### Terminal States

```python
# backend/ws_handlers.py
_TERMINAL_STATES = ("done", "failed", "cancelled", "error")
```

`"error"` was added to this tuple since the original audit (mirrored in `frontend/src/stores/tasks.ts`'s `TERMINAL_STATES`) but is still missing from the `TaskState` union above and from the label/color maps below — a drift worth reconciling. Once a task enters a terminal state it cannot transition further (guarded at every endpoint).

### State Transition Graph

```
[created]
    │
    ▼
 pending ──(cancel)────────────────────────────────────────────► cancelled
    │                                                                 ▲
    │ (foreman assigns)                                               │ (cancel: any non-terminal)
    ▼                                                                 │
 working ──(task-update: state=planning)──► planning ────────────────┤
    │  │                                    [unguarded; no watchdog]  │
    │  └──(task-update: state=failed/error)──────────────► failed/error
    │                                                                 │
    │ (task-complete / agent: idle, offline, error, timeout)          │
    ▼                                                                 │
 awaiting-review ──(finalize)──────────────────────────────► done    │
    │  │                                                              │
    │  └──(redirect_task)───────────────────────────────► working ───┘
    │
    └──► followup ──(task-followup-done)──► awaiting-review
         [legacy: no active backend path currently sets this state]
```

### State Transition Locations

| Transition | From → To | File & Function | Trigger |
|-----------|-----------|-----------------|---------|
| Task created | `→ pending` | `backend/foreman/tools.py` — `create_task` tool | Foreman AI decision (no frontend "task-created" WS message exists) |
| Foreman assigns | `pending → working` | `backend/foreman/tools.py` — `assign_task` tool | Foreman AI decision |
| Worker sends task-update | `→ (any)` | `backend/ws_handlers.py` — `handle_task_update` | `task-update` WS message from worker |
| Worker sends task-complete | `working → awaiting-review` | `backend/ws_handlers.py` — `handle_task_complete` | `task-complete` WS (guarded: `state == "working"`) |
| Agent enters lock-release state | `working → awaiting-review` | `backend/ws_handlers.py` — `handle_agent_state` | `agent-state` handler (agent idle/offline/error) |
| Stale task watchdog | `working → awaiting-review` | `backend/main.py` — orphaned-task sweep in the lifespan sweeper | Sweeper: orphaned working task, no active agent |
| Follow-up done | `(non-terminal) → awaiting-review` | `backend/ws_handlers.py` — `handle_task_followup_done` | `task-followup-done` WS |
| Finalize | `(non-terminal) → done` | `backend/routes/tasks.py` — `finalize_task_endpoint` | REST endpoint |
| Cancel | `(non-terminal) → cancelled` | `backend/routes/tasks.py` — `cancel_task_endpoint` | REST endpoint |
| Redirect | `(non-terminal) → working` | `backend/routes/tasks.py` — `redirect_task_endpoint` | REST endpoint |

### Frontend State Rendering

Labels and colors defined in `frontend/src/stores/tasks.ts` (`STATE_LABELS`/`STATE_COLORS`):

| Backend State | Display Label | Color |
|--------------|---------------|-------|
| `pending` | `pending` | `dim` |
| `planning` | `planning` | `blue` |
| `working` | `working` | `green` |
| `awaiting-review` | **`review`** (shortened) | `amber` |
| `done` | `done` | `teal` |
| `failed` | `failed` | `red` |
| `followup` | **`follow-up`** (hyphenated) | `orange` |
| `cancelled` | `cancelled` | `red` |

**Vocabulary differences** (frontend display vs. backend value): `awaiting-review` → `review`, `followup` → `follow-up`.

---

## 4. Cross-Level Analysis

### Interaction Map

```
Guild
  └─ Worker (host state: idle/online/offline)
       └─ Agent (fine-grained: idle/working/thinking/busy/error/offline)
            └─ current_task_id (link to Task)

Task (state: pending/working/awaiting-review/done/failed/followup/cancelled/error)
```

- **Worker state ← agent state (frontend only):** the DB never derives worker state from agent state — independent. The frontend computes a per-worker display state from the highest-priority agent state (`frontend/src/stores/agents.ts`), so a worker can show "working" while the DB says "online" — intentional; backend worker state is a coarse connectivity flag, not a workload indicator.
- **Agent state → task state (lock release):** when an agent transitions to `idle`, `offline`, `error`, or `timeout`, the backend checks whether its `current_task_id` points to a `working` task owned by the same worker, and if so releases the lock and moves the task to `awaiting-review` (`backend/ws_handlers.py` — `handle_agent_state`). Primary safety mechanism against tasks stuck in `working` after a worker crash.
- **Task state ← agent state:** none beyond the lock-release above, by design. A task staying `awaiting-review` while its agent is `idle` is normal — it's waiting on the foreman.

### Design Observations (at time of writing)

1. **`thinking`/`busy` agent states remain undefined in production** — present in the frontend union, `STATE_RANK`, and the stale-task watchdog's active-agent query (`backend/main.py`), but no worker path emits them (confirmed still true). The related frontend color/rank branches are dead code.

2. **Worker state is still coarse**, cycling only `idle → online → offline` while the frontend prefers agent-derived state. `worker.state` can diverge from actual agent liveness (e.g. `online` worker, all agents `offline`). New lifecycle columns (`container_id`, `spawned_version`, `started_at`, `drain_requested_at`) add richer tracking alongside `state`, but don't change its three values.

3. **`followup` task state is still legacy/rare** — present in the frontend union, color map, a UI dropdown, and tests, but no backend path sets `task.state = 'followup'` (the follow-up flow stays in `awaiting-review`). Confirmed still true; likely dead code.

4. **Security concern — `task-update` is still unvalidated.** `handle_task_update` (`backend/ws_handlers.py`) applies a worker's requested state change without checking it owns the task (confirmed unpatched). A rogue worker can set `state=planning` on any task; since `planning` is outside both `_TERMINAL_STATES` and the watchdog's `state == "working"` guard, the victim task stalls indefinitely. **Fix:** validate `task.worker_id == requesting agent's worker_id`, mirroring the guard already present in `handle_agent_state`'s lock-release path.

5. ~~**Agent `activity` is not persisted.**~~ **Resolved** — `activity` is now a persisted `agents` column (migration `20260501_000000_add_activity_to_agents`), surviving restarts rather than living only in memory.

---

## 5. File Reference Map

Superseded by the "Where Defined" / "State Transition Locations" tables in sections 1–3, which already name the responsible file and function for every state and transition covered by this audit. Grep those names in current code rather than relying on a separate index here.
