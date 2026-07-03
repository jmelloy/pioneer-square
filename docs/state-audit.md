# Pioneer Square — Worker / Agent / Task State Audit

> Generated: 2026-05-26
> Verified at commit: fc64c4478e91949438d4467238671baf69d401ac

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

### Where Defined

- **Database schema**: `backend/alembic/versions/20260428_000000_initial_schema.py:63` — `workers.state` TEXT, `server_default='idle'`
- **ORM model**: `backend/models.py:98` — `Worker.state`, Python default `"idle"`

### State Transition Locations

| Transition | To State | File & Location | Trigger |
|-----------|----------|-----------------|---------|
| Worker WebSocket joins | `online` | `backend/ws_handlers.py:247` (`handle_join`) | `join` WS message |
| Worker WebSocket disconnects | `offline` | `backend/ws_handlers.py:578` (`handle_worker_disconnect`) | WS close event |
| Startup reset | `offline` | `backend/main.py:75` | App startup — resets all workers |
| Stale-worker sweeper | `offline` | `backend/main.py:146` | Background sweeper task |

### Notes

- Worker state is coarse: only online/offline/idle. The fine-grained "what is the worker doing right now" is captured by agent state (below).
- Frontend does **not** have a separate state vocabulary for workers. The computed worker list in `frontend/src/stores/agents.ts:52–72` derives a display state from the highest-priority agent state for that worker.

---

## 2. Agent States

### Defined Values

TypeScript union (authoritative frontend type):
```typescript
// frontend/src/types.ts:1
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

A granular sub-state that refines the `working` state. It is **not** a separate state field — it is emitted alongside `agent-state` messages and cleared when the agent is not working.

```typescript
// frontend/src/types.ts:3–10
type AgentActivity = 'reading' | 'editing' | 'running' | 'searching' | 'fetching' | 'thinking' | 'planning'
```

Mapping from Claude tool names to activities (`worker/pioneer_worker/claude_runner.py:40–52`):

| Tool | Activity |
|------|----------|
| `Bash` | `running` |
| `Read`, `TodoRead` | `reading` |
| `Write`, `Edit`, `MultiEdit`, `NotebookEdit` | `editing` |
| `WebSearch` | `searching` |
| `WebFetch` | `fetching` |
| `Agent`, `TodoWrite` | `planning` |
| Thinking block | `thinking` |

### Where Defined

- **Database schema**: `backend/alembic/versions/20260428_000000_initial_schema.py:74` — `agents.state` TEXT, `server_default='idle'`
- **ORM model**: `backend/models.py:58` — `Agent.state`, Python default `"idle"`
- **Frontend type**: `frontend/src/types.ts:1`

### State Transition Locations

| Transition | To State | File & Location | Trigger |
|-----------|----------|-----------------|---------|
| Agent joins | `idle` | `backend/ws_handlers.py:224` | `join` WS message (upsert) |
| Task begins | `working` | `worker/pioneer_worker/worker.py:1600` | `_set_state("working")` |
| Claude crash | `error` | `worker/pioneer_worker/worker.py:1683, 1909` | Subprocess failure / timeout |
| Task completes successfully | `idle` | `worker/pioneer_worker/worker.py:1010, 1826, 1840, 1915` | End of task execution |
| WS closes (worker side) | `offline` | `worker/pioneer_worker/worker.py:1564` | `_set_state("offline")` |
| WS closes (backend side) | `offline` | `backend/ws_handlers.py:572` | Connection handler |
| Stale sweeper | `offline` | `backend/main.py:132` | Background sweeper task |
| Startup reset | `offline` | `backend/main.py:79` | App startup |
| Explicit WS message | any | `backend/ws_handlers.py:357–422` | `agent-state` message from worker |

### Lock-Release States

```python
# backend/ws_handlers.py:354
_LOCK_RELEASE_AGENT_STATES = frozenset({"idle", "offline", "error", "timeout"})
```

When an agent enters one of these states, the backend checks whether it owns a `working` task and, if so, moves it to `awaiting-review`.

> **`timeout` state discrepancy:** `_LOCK_RELEASE_AGENT_STATES` includes `"timeout"` but the TypeScript `AgentState` union (`frontend/src/types.ts:1`) does **not** list `timeout` as a valid value. No code path in the audited codebase emits `agent-state: timeout` from either the worker or the backend — the entry in `_LOCK_RELEASE_AGENT_STATES` appears to be a defensive placeholder (if a `timeout` state were ever emitted, the lock would release). This discrepancy requires a decision: either confirm a code path that emits `timeout` and add it to the TypeScript union, or remove it from `_LOCK_RELEASE_AGENT_STATES` if no such path is planned.

### Frontend Priority Ranking

Used by `frontend/src/stores/agents.ts:9–16` to derive a single display state when a worker has multiple agents:

```typescript
const STATE_RANK = { working: 0, thinking: 1, busy: 2, error: 3, idle: 4, offline: 5 }
```

Lower rank = higher priority (most active state wins).

---

## 3. Task States

### Defined Values

TypeScript union (authoritative frontend type):
```typescript
// frontend/src/types.ts:52–60
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
# backend/ws_handlers.py:40
_TERMINAL_STATES = ("done", "failed", "cancelled")
```

Once a task enters a terminal state it cannot transition further (guarded at every endpoint).

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
    │  └──(task-update: state=failed)────────────────────► failed     │
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

| Transition | From → To | File & Location | Trigger |
|-----------|-----------|-----------------|---------|
| Task created | `→ pending` | `backend/ws_handlers.py:154–162` (frontend msg) / foreman | `task-created` WS or Foreman tool |
| Foreman assigns | `pending → working` | `backend/routes/foreman.py` / Foreman `assign_task` tool | Foreman AI decision |
| Worker sends task-update | `→ (any)` | `backend/ws_handlers.py:619` | `task-update` WS message from worker |
| Worker sends task-complete | `working → awaiting-review` | `backend/ws_handlers.py:648–649` | `task-complete` WS (guarded: `state == "working"`) |
| Agent enters lock-release state | `working → awaiting-review` | `backend/ws_handlers.py:405–408` | `agent-state` handler (agent idle/offline/error) |
| Stale task watchdog | `working → awaiting-review` | `backend/main.py:202–203` | Sweeper: orphaned working task, no active agent |
| Follow-up done | `(non-terminal) → awaiting-review` | `backend/ws_handlers.py:693–696` | `task-followup-done` WS |
| Finalize | `(non-terminal) → done` | `backend/routes/tasks.py:276` | `finalize_task` REST endpoint |
| Cancel | `(non-terminal) → cancelled` | `backend/routes/tasks.py:327` | `cancel_task` REST endpoint |
| Redirect | `(non-terminal) → working` | `backend/routes/tasks.py:378` | `redirect_task` REST endpoint |

### Frontend State Rendering

Labels and colors defined in `frontend/src/stores/tasks.ts:16–36`:

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

**Vocabulary differences** (frontend display vs. backend value):
- `awaiting-review` → `review`
- `followup` → `follow-up`

---

## 4. Cross-Level Analysis

### Interaction Map

```
Guild
  └─ Worker (host state: idle/online/offline)
       └─ Agent (fine-grained: idle/working/thinking/busy/error/offline)
            └─ current_task_id (link to Task)

Task (state: pending/working/awaiting-review/done/failed/followup/cancelled)
```

### Worker State ← Agent State (Frontend Only)

- The **database** does not derive worker state from agent state. They are independent.
- The **frontend** computes a display state for each worker by selecting the highest-priority agent state among all agents belonging to that worker (`frontend/src/stores/agents.ts:52–72`).
- This means the frontend can show a worker as "working" while the DB has it as "online". This is intentional — the backend worker state is a coarse connectivity flag, not a workload indicator.

### Agent State → Task State (Lock Release)

- When an agent transitions to `idle`, `offline`, `error`, or `timeout`, the backend checks whether that agent's `current_task_id` points to a `working` task owned by the same worker.
- If so, it releases the task lock and moves the task to `awaiting-review`.
- This is the primary safety mechanism preventing tasks from getting stuck in `working` when a worker crashes.
- Location: `backend/ws_handlers.py:354–421`

### Task State ← Agent State (None — by design)

- Task state does **not** automatically change based on agent state transitions (other than the lock-release above).
- A task staying `awaiting-review` while its agent is `idle` is normal — the task is waiting for the foreman to finalize or send a follow-up.

### Potential Design Issues

1. **`thinking` and `busy` agent states are undefined in production code.** They appear in `backend/main.py:188` (active-agent query) and the frontend type union but are never emitted by the real worker. This means the query might never match any real agent in those states, and the frontend color/rank mappings for them are dead code paths.

2. **Worker state is nearly vestigial.** The only transitions are `idle → online → offline`. The frontend ignores it in favor of agent-derived state. The backend uses it mainly as a connectivity flag. There is a risk that `worker.state` and the set of online agents diverge (e.g., worker shows `online` but all its agents are `offline` after a crash).

3. **`followup` task state is legacy/rare.** It appears in the frontend type union and color mapping but no backend code path currently sets `state = 'followup'` directly. The follow-up flow uses `awaiting-review` as the state while a follow-up is in progress. This state may be dead code.

4. **Security concern — `task-update` state changes are unvalidated.** The `task-update` WebSocket message handler (`backend/ws_handlers.py:619`) applies the requested state change without verifying that the requesting worker owns the task. A rogue or compromised worker can therefore set `state=planning` on *any* other worker's task. Because `planning` is absent from both `_TERMINAL_STATES` and `_LOCK_RELEASE_AGENT_STATES`, the stale-task watchdog does not reclaim it — the victim task is stalled indefinitely with no automatic recovery path.

   **Recommended fix:** Add server-side ownership validation to the `task-update` handler. Before applying a state change, verify that `task.assigned_worker_id == requesting_agent.worker_id`. Reject the update (e.g. a `task-update-error` WS message) if the IDs do not match.

5. **Agent `activity` is not persisted.** It is only in memory / WS broadcast. A browser reload loses the current activity display. This is probably fine for a display hint but worth noting.

---

## 5. File Reference Map

### State Definitions

| File | What It Defines |
|------|----------------|
| `backend/models.py:58, 98, 128` | ORM field defaults for agent, worker, task state |
| `backend/alembic/versions/20260428_000000_initial_schema.py:63, 74, 88` | DB column defaults |
| `backend/ws_handlers.py:40, 354` | `_TERMINAL_STATES`, `_LOCK_RELEASE_AGENT_STATES` constants |
| `frontend/src/types.ts:1, 52–60` | TypeScript `AgentState`, `AgentActivity`, `TaskState` unions |

### State Transitions — Backend

| File | Responsibility |
|------|----------------|
| `backend/ws_handlers.py:210–344` | `handle_join`: agent register, worker → online |
| `backend/ws_handlers.py:357–422` | `handle_agent_state`: agent state changes + lock release |
| `backend/ws_handlers.py:566–594` | `handle_worker_disconnect`: mark offline |
| `backend/ws_handlers.py:628–680` | `handle_task_complete`: working → awaiting-review |
| `backend/ws_handlers.py:683–702` | `handle_task_followup_done`: → awaiting-review |
| `backend/routes/tasks.py:273–399` | `finalize_task`, `cancel_task`, `redirect_task` endpoints |
| `backend/main.py:72–206` | Startup reset + stale sweeper |

### State Transitions — Worker

| File | Responsibility |
|------|----------------|
| `worker/pioneer_worker/worker.py:751–760` | `_set_state()`: updates local state + broadcasts |
| `worker/pioneer_worker/worker.py:733–749` | `_emit_agent_state()`: sends `agent-state` WS message |
| `worker/pioneer_worker/claude_runner.py:40–52, 80` | Maps Claude tool events to `activity` values |

### Frontend Rendering

| File | Responsibility |
|------|----------------|
| `frontend/src/stores/agents.ts:9–16, 52–72, 98–113` | State rank, computed worker state, agent updates |
| `frontend/src/stores/tasks.ts:16–36, 241–245` | Task labels and color mapping |
