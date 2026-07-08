# Per-Task Context Splitting for the Foreman

**Status:** Implemented in the embedded backend Foreman.
**Issue:** [#649](https://github.com/jmelloy/pioneer-square/issues/649)
**Updated:** 2026-07-07

---

## Current Model

The backend Foreman can run task-scoped child turns for review-loop events while
keeping cross-cutting work in the parent guild context.

Task-scoped events:

- `task-complete`
- `followup-done`
- `needs-input`
- `task-error`

Parent-context events:

- human chat
- worker lifecycle
- periodic checks
- Claude/auth events
- task creation and assignment

The standalone `pioneer foreman` process no longer participates in this routing.
It is only an LLM API proxy. All child-context decisions happen in
`backend/foreman/runner.py` and `backend/ws_handlers.py`.

---

## How Isolation Works

There are no long-lived child supervisor objects. A child context is an ephemeral
single Foreman turn with three pieces of isolation:

1. `task_id`-filtered history in `_load_history(..., task_id=...)`.
2. Child-mode prompt, state preamble, and tool set via
   `run_foreman_ai(child=True, task_id=...)`.
3. A per-task `asyncio.Lock` keyed as `(guild_id, "task:<id>")` in `_guild_locks`.

If a task-scoped run is already active for the same task, the new invocation is
dropped instead of queued — unless it's human-originated (e.g. a user-requested
follow-up posted from the web UI), in which case it's appended to a small FIFO
queue and drained once the in-flight run for that task finishes. See
"Human message queueing" below. Automated child-context triggers (task-complete,
followup-done, needs-input) keep the drop-if-busy behavior; the backend
poll/re-trigger mechanism recovers stale or missed work on a later tick.
Different tasks may run concurrently.

Parent runs are keyed by `(guild_id, user_id)` and never read task-tagged child
history, even if the incoming parent trigger includes a `task_id` for Discord
thread routing.

---

## Prompt And History

Child runs use:

- `build_child_system_blocks(...)`
- `build_child_state_preamble(...)`
- `CHILD_FOREMAN_TOOLS`

`CHILD_FOREMAN_TOOLS` excludes `create_task` and `assign_task`, so child turns can
review, follow up, finalize, cancel, redirect, or inspect their task but do not
create or assign new work.

The child state preamble includes only the relevant task and assigned worker.
This keeps task review context small and prevents unrelated task history from
bleeding into the review loop.

---

## WebSocket Routing

`ws_handlers._trigger_foreman()` determines whether a trigger should be child
scoped:

```python
child = bool(task_id) and event in _CHILD_FOREMAN_EVENTS
spawn(run_foreman_ai(guild_id, message, task_id=task_id, child=child))
```

The external proxy is not involved in routing. If connected, it is used later by
`backend.foreman.runner` only when a concrete LLM API request needs to be
executed.

---

## Operational Notes

- There is no child queue, respawn, or teardown path.
- There is no standalone `TaskContext` class.
- Continuity comes from database-backed `ForemanTurn.task_id` history.
- Frontend/Discord messages produced inside a child turn are tagged with that
  `task_id`; parent messages can still route to a task's Discord thread without
  being stored in that task's child history.
