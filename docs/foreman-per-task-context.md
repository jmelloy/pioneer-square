# Per-Task Context Splitting for the Foreman

**Status:** Implemented in the embedded backend Foreman.
**Issue:** [#649](https://github.com/jmelloy/pioneer-square/issues/649)
**Updated:** 2026-07-07

---

## Current Model

The backend Foreman can run task-scoped child turns for review-loop events while
keeping cross-cutting work in the parent guild context.

- **Task-scoped events:** `task-complete`, `followup-done`, `needs-input`, `task-error`.
- **Parent-context events:** human chat, worker lifecycle, periodic checks,
  Claude/auth events, task creation and assignment.

`ws_handlers._trigger_foreman()` decides which path a trigger takes:

```python
child = bool(task_id) and event in _CHILD_FOREMAN_EVENTS
spawn(run_foreman_ai(guild_id, message, task_id=task_id, child=child))
```

This decision is made entirely in `backend/foreman/runner.py` and
`backend/ws_handlers.py`, independent of whether a standalone proxy is
connected — the proxy, when present, is only used later by
`backend.foreman.runner` to execute the concrete LLM API call. See
[foreman-split-plan.md](foreman-split-plan.md) for how the proxy fits in.

---

## How Isolation Works

There are no long-lived child supervisor objects. A child context is an ephemeral
single Foreman turn with three pieces of isolation:

1. `task_id`-filtered history in `_load_history(..., task_id=...)`.
2. Child-mode prompt, state preamble, and tool set via
   `run_foreman_ai(child=True, task_id=...)`.
3. A per-task `asyncio.Lock` keyed as `(guild_id, "task:<id>")` in `_guild_locks`.

If a task-scoped run is already active for the same task, a new automated
invocation (task-complete, followup-done, needs-input) is dropped rather than
queued; the poll/re-trigger mechanism recovers stale or missed work on a later
tick. A human-originated invocation (e.g. a follow-up posted from the web UI)
is instead appended to a small FIFO queue (`_enqueue_human_turn`/
`_drain_human_queue` in `runner.py`) and drained once the in-flight run for
that task finishes. Different tasks run concurrently.

Parent runs are keyed by `(guild_id, user_id)` and never read task-tagged child
history, even if the incoming parent trigger includes a `task_id` for Discord
thread routing.

---

## Prompt And History

Child runs use `build_child_system_blocks(...)`, `build_child_state_preamble(...)`,
and `CHILD_FOREMAN_TOOLS`.

`CHILD_FOREMAN_TOOLS` excludes `create_task` and `assign_task`, so child turns can
review, follow up, finalize, cancel, redirect, or inspect their task but do not
create or assign new work. The child state preamble includes only the relevant
task and assigned worker, keeping review context small and free of unrelated
task history.

`send_followup` and `redirect_task` are scoped to the child's own task
(issue [#997](https://github.com/jmelloy/pioneer-square/issues/997)):
`_task_mutation_blocked()` in `backend/foreman/tools.py` rejects a call whose
`task_id` differs from the child's own, so a per-task child context can course
correct or continue its own task but cannot reach into another task's
lifecycle — that stays the parent Foreman's job.

---

## Operational Notes

- No child queue, respawn, or teardown path exists today.
- There is no standalone `TaskContext` class.
- Continuity comes from database-backed `ForemanTurn.task_id` history.
- Frontend/Discord messages produced inside a child turn are tagged with that
  `task_id`; parent messages can still route to a task's Discord thread without
  being stored in that task's child history.
