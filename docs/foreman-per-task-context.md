# Foreman Conversation Context

**Status:** Implemented in the embedded backend Foreman.
**Issue:** [#1200](https://github.com/jmelloy/pioneer-square/issues/1200) (supersedes the
per-task child context model from [#649](https://github.com/jmelloy/pioneer-square/issues/649))
**Updated:** 2026-08-21

---

## Current Model

The Foreman has a single context per (guild, user): their conversation. There is no
separate per-task history.

```text
thread_id = conversational context / Foreman memory scope
task_id   = optional referenced task/work item (metadata, not a context boundary)
```

- A human message to the Foreman runs in the user's conversation (`thread_id` resolved to
  their active thread; `task_id` is null).
- When the Foreman creates a task, `Task.thread_id` is stamped to the current thread, so
  the task stays linked to the conversation it came from.
- When a worker completes/errors on a task, the triggering event resolves `user_id` to the
  task's owner (`Task.user_id`) and runs in *that user's* conversation — the same
  `ForemanTurn` history as their other Foreman turns — with `task_id` attached purely as
  metadata (message badges, tool-context, Discord routing).
- `ws_handlers._trigger_foreman()` no longer branches on event type or task_id to pick a
  context; every trigger goes through `foreman.runner.run_foreman_ai()` the same way:

```python
await run_foreman_ai(guild_id, message, user_id=user_id, task_id=task_id, ...)
```

---

## Run Locking

`_guild_locks` in `backend/foreman/runner.py` serialises concurrent runs, keyed on
`(guild_id, user_id)` — never on `task_id`. This means:

- A task-triggered event (task-complete, followup-done, needs-input, task-error) and that
  task owner's own chat naturally serialise against each other instead of racing on
  independent locks (this closes the cross-context race from issue #927 by construction —
  there's only one context left to race with).
- Two different tasks owned by different users still run concurrently.
- Automated (non-human) invocations are dropped, not queued, when the lock is busy — the
  poll loop re-triggers on the next tick. Human-originated invocations are queued
  (`_human_queues`) and drained in order once the in-flight run finishes.

---

## History And Thread Resolution

- `_load_history(guild_id, user_id)` always loads the whole (guild, user) conversation —
  there is no `task_id`-filtered slice. `ForemanTurn.task_id` is still stamped on each row
  (mirrors `ApiRequestLog.task_id`) purely as metadata, e.g. for the debug view; it is never
  used to filter reads.
- `resolve_thread_id(db, guild_pk, task_id=task_id, user_id=user_id)`
  (`foreman/thread_service.py`) is called early in `_run_foreman_ai`, before history is
  loaded. It prefers the referenced task's thread (`Task.thread_id`, stamped once at
  task-creation time) and falls back to the user's current active thread. It never creates
  a thread — an automated task event for a task with no thread degrades gracefully (the
  turn just isn't attached to a thread) rather than spinning up an unrelated conversation.
- Prompt context (system prompt, tool set, state preamble) is always the full whole-guild
  view (`FOREMAN_TOOLS`, `build_system_blocks`, `build_state_preamble`) — task-specific
  detail (description, state, branch/PR, worker) is already included via the
  `tasks_block`/`workers_block` state preamble, same as for any other trigger.

---

## Message Metadata

`task_id` is still carried through to `Message.task_id`, `ForemanTurn.task_id`, and the WS
`ChatMsg.taskId` field, and the frontend still badges lines that concern a specific task —
none of that changed. What changed is that `task_id` no longer selects a different
*history*: it's metadata on a turn that otherwise belongs to the same conversation as
everything else for that user.

Discord routing (`_emit_foreman_chat`'s `discord_task_id`) mirrors the run's narration into
the referenced task's Discord thread when one exists, same as before.
