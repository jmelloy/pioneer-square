# Max-Turns Audit: Follow-up Queue Bug

**Date:** 2026-05-27  
**Task under investigation:** `t-tvxkcl` — `error_max_turns` at ~16:14 UTC, three subsequent `send_followup` calls silently dropped  
**Branch investigated:** `origin/claude/notify-foreman-on-max-turns-484-t-ty0s` (PR #485)

---

## 1. Root Cause: Why Follow-ups Don't Run After Max-Turns

There are two independent mechanisms that can cause follow-ups to be silently dropped after max-turns. Depending on whether Claude exits with code 0 or non-zero, one or both apply.

### 1a. Lock not released when task transitions to `error` state

**File:** `backend/ws_handlers.py:40,631`

```python
# line 40
_TERMINAL_STATES = ("done", "failed", "cancelled")  # "error" is missing

# line 629-633 — handle_task_update
if update_values:
    await ctx.db.execute(update(Task).where(Task.id == task_id).values(**update_values))
    if update_values.get("state") in _TERMINAL_STATES:          # "error" never matches
        await LockService(ctx.db).release(f"task:{task_id}")    # lock stays held
    await ctx.db.commit()
```

When `success=False` — exit code non-zero, which can happen on any error including some
versions of Claude CLI reporting max-turns — the worker sends:

```python
# worker/pioneer_worker/worker.py:1902-1908
await self._task_update(
    task_id,
    agent=agent,
    type="needs-input" if stop_reason == "needs_input" else "error",
    state="error",          # <-- sends state="error" via handle_task_update
    **msg,
)
```

`handle_task_update` sets the task to `error` state but **never releases the follow-up
lock** because `"error"` is absent from `_TERMINAL_STATES`. From this point:

1. `send_followup` calls `LockService.acquire(f"task:{task_id}")` → fails (lock held).
2. Each call is stored as a `pending-followup` `TaskEvent` row.
3. The `pending-followup` queue is only drained in `handle_task_followup_done`
   (`ws_handlers.py:724-735`), which is triggered by `task-followup-done` — a message
   the worker only sends on **success**.
4. The queue therefore never drains. All follow-ups are permanently blocked.

**Verdict:** This is a definite code bug. Any task that exits with a non-zero code (error,
interrupted, or some CLI versions of max-turns) leaves the lock held forever and silently
swallows all subsequent `send_followup` tool calls.

### 1b. Foreman receives no max-turns signal — follow-up loop never starts

**File:** `backend/ws_handlers.py:675-691`

When max-turns exits with code 0 (`success=True`, the normal case per `claude_runner.py`
docstring and tests), the task correctly lands in `awaiting-review` with the lock released.
However, the foreman is triggered with a generic "task complete" message:

```python
# ws_handlers.py:675-691 (current main)
await _trigger_foreman(
    ctx.guild_id,
    "task-complete",
    f"[task-complete] Worker {worker_id_msg} finished task {task_id}: "
    f'"{desc[:80]}" — branch: {branch}.{pr_line} '
    "The worker has returned to its idle pool; the task is parked in "
    "awaiting-review for human review. "
    "Default behaviour: leave PR-bearing tasks open so reviewers can "
    "comment — call send_followup if a comment or CI failure asks for "
    "an iteration on the same branch (any idle worker can pick it up). "
    "Only call finalize_task when the work is genuinely closed (PR "
    "merged, task abandoned, or it was an ephemeral/automation task).",
    ...
)
```

The foreman LLM reads "default behaviour: leave PR-bearing tasks open" and has no
directive to call `send_followup` for continuation. The follow-up loop never starts
automatically. Even if `send_followup` is called externally, it will work only if an
idle worker is available at that moment — if the worker pool is temporarily busy or
disconnected (e.g., still processing the previous `agent-state: working` → `idle`
transition), all calls return "No idle worker available" with no state change and no
log entries, which matches the t-tvxkcl symptom exactly.

### Timeline reconstruction for t-tvxkcl

| Time | Event |
|------|-------|
| ~16:14 UTC | Claude subprocess hits max-turns; exits with code 0; `stop_reason="max_turns"` |
| +0s | Worker sends `task-complete` (`stopReason="max_turns"`, `state="awaiting-review"`) |
| +0s | `handle_task_complete` sets task to `awaiting-review`, releases lock |
| +0s | Foreman triggered with generic "task complete, default: leave open" message |
| +Xs | Foreman calls `send_followup` (3 times, likely based on prior context) |
| +Xs | `_select_followup_worker` returns `None` — no idle agents at that moment |
| +Xs | All 3 calls return "No idle worker available"; no state change; no log entries |
| now | Task stuck in `awaiting-review`; no follow-up ever dispatched |

---

## 2. Recent Commit Analysis — Regression from Yesterday

```
git log --since="2 days ago" --oneline
a221e7a Merge pull request #482 from jmelloy/claude/ios-app-design-aCcNC
c33ae73 Make factory floor agents tappable
04db8e2 Fix touch targets on nested sidebar tabs and tab close button
506ba8f Fix iOS viewport and safe-area on mobile layout
24d552d Merge pull request #480 from jmelloy/claude/fix-chat-messages-reactivity-performance-479-follow-up-t-e0bv
...
```

**No commit on `main` in the past two days touched `ws_handlers.py`, follow-up
dispatching, or `task-complete` handling.** The lock-not-released bug and the missing
max-turns signal predate all recent activity.

### PR #485 (`claude/notify-foreman-on-max-turns-484-t-ty0s`, commit `0c49453`)

This PR is **not yet merged to main**. It was authored 2026-05-27 to address the missing
max-turns foreman signal (Bug 1b above). Key diff in `backend/ws_handlers.py`:

```diff
 async def handle_task_complete(ctx: WSContext, data: dict) -> None:
     ...
+    stop_reason = data.get("stopReason", "success")
     ...
+    if stop_reason == "max_turns":
+        foreman_message = (
+            f"[task-complete/max-turns] Worker {worker_id_msg} task {task_id}: ..."
+            "IMPORTANT: Claude hit its max-turns limit and stopped before finishing. "
+            "Call send_followup with a continuation prompt so the worker can resume on the "
+            "same branch/worktree. ..."
+        )
+    else:
+        foreman_message = (   # original generic message
```

```diff
 async def handle_task_followup_done(ctx: WSContext, data: dict) -> None:
     ...
+    stop_reason = data.get("stopReason", "success")
+    last_text_fud = data.get("lastText", "")
     ...
+    if stop_reason == "max_turns":
+        human_msg = (
+            f"[followup-done/max-turns] ... hit Claude's max-turns limit ..."
+            "Call send_followup with a continuation prompt to resume ..."
+        )
+    elif queued_payloads:
         ...
```

**Assessment of PR #485:**

- Correctly fixes Bug 1b for the normal case (`exit_code=0`, `stop_reason="max_turns"`).
- The `"max_turns"` string matches the value emitted by `claude_runner.py` (confirmed by
  docstring at line 282 and test at `worker/tests/test_claude_runner.py:270`).
- Moving the `max_turns` check **before** `queued_payloads` in `handle_task_followup_done`
  is correct — a max-turns exit should tell the foreman to resume, regardless of whether
  queued payloads exist.
- **Does NOT fix Bug 1a** — the lock-not-released issue on `error` state remains.
- **Latent risk:** if a Claude CLI version emits `"error_max_turns"` instead of
  `"max_turns"` as the result subtype, the check won't match and the generic message
  is still sent. The worker's `claude_runner.py` documents `"max_turns"` as the
  canonical value; `"error_max_turns"` appears only in the bug description (likely a
  display artifact from `parse_claude_event` rendering `"✗ max_turns: <error>"` in
  terminal output).

**Conclusion on Bug 2:** No commit on `main` regressed this behavior. The follow-up
drop bugs were present before PR #485. PR #485 partially fixes them but is not merged.

---

## 3. Recommended Fix

### Fix A — Release lock on `error` state (Bug 1a)

**File:** `backend/ws_handlers.py:40` and/or `:631`

**Option 1** (minimal, targeted):
```python
# ws_handlers.py:629-633
if update_values:
    await ctx.db.execute(update(Task).where(Task.id == task_id).values(**update_values))
    new_state = update_values.get("state")
    if new_state in _TERMINAL_STATES or new_state == "error":
        await LockService(ctx.db).release(f"task:{task_id}")
    await ctx.db.commit()
```

**Option 2** (cleaner, makes intent explicit):
```python
# ws_handlers.py:40
_TERMINAL_STATES = ("done", "failed", "cancelled")
_LOCK_RELEASE_STATES = (*_TERMINAL_STATES, "error")  # error is non-terminal but lock must free

# ws_handlers.py:631
if update_values.get("state") in _LOCK_RELEASE_STATES:
    await LockService(ctx.db).release(f"task:{task_id}")
```

This ensures that when a follow-up task fails (error exit), the lock is freed so the
foreman can dispatch a new `send_followup` call instead of queuing it silently.

### Fix B — Merge PR #485 (Bug 1b)

PR #485 correctly adds the max-turns signal to the foreman. Merging it will cause the
foreman to automatically call `send_followup` with a continuation prompt when max-turns
is hit, instead of silently leaving the task in `awaiting-review`.

### Fix C — Harden against `"error_max_turns"` subtype (latent risk)

If future Claude CLI versions emit `"error_max_turns"`:

**File:** `backend/ws_handlers.py` (after PR #485 is merged)

```python
# Update both handle_task_complete and handle_task_followup_done
_MAX_TURNS_REASONS = frozenset({"max_turns", "error_max_turns"})
...
if stop_reason in _MAX_TURNS_REASONS:
    ...
```

### Priority order

| Priority | Fix | Impact |
|----------|-----|--------|
| P0 | Fix A (lock release on error) | Unblocks all silently-stuck follow-ups |
| P1 | Merge PR #485 | Auto-resumes max-turns tasks without human intervention |
| P2 | Fix C (error_max_turns hardening) | Defensive; prevents silent regression on CLI version change |

---

## Appendix: Key File References

| File | Line(s) | Relevance |
|------|---------|-----------|
| `backend/ws_handlers.py` | 40 | `_TERMINAL_STATES` definition — `"error"` absent |
| `backend/ws_handlers.py` | 629–636 | `handle_task_update` — lock release guarded by `_TERMINAL_STATES` |
| `backend/ws_handlers.py` | 662 | `handle_task_complete` — lock released correctly on success |
| `backend/ws_handlers.py` | 675–691 | `handle_task_complete` — generic foreman message, no max-turns hint |
| `backend/ws_handlers.py` | 715 | `handle_task_followup_done` — lock released correctly on success |
| `backend/ws_handlers.py` | 724–735 | `pending-followup` drain — only fires on `task-followup-done` |
| `backend/foreman/tools.py` | 790–814 | `send_followup` — lock acquire; queues on failure |
| `backend/foreman/tools.py` | 202–249 | `_select_followup_worker` — returns `None` if no idle agent |
| `worker/pioneer_worker/worker.py` | 1890–1908 | Task result dispatch — `success=True` → `task-complete`, else `error` |
| `worker/pioneer_worker/claude_runner.py` | 282–284,357,377 | `stop_reason` from `result.subtype`; `success = exit_code == 0` |
