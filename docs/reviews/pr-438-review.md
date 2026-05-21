# Code Review: PR #438 — Fix docstring gap + clarify batch-disconnect prompt

**Date:** 2026-05-21
**PR:** [#438](https://github.com/jmelloy/pioneer-square/pull/438)
**Scope:** `backend/ws_handlers.py`, `backend/foreman/prompt.py`
**Verdict:** Approve — two targeted documentation fixes, both correct and low-risk

---

## What this PR does

PR #435 (merged) added worker lifecycle notifications so the foreman AI learns when workers join or leave the guild. PR #438 fixes two documentation gaps found during the review of #435:

1. **`ws_handlers.py` docstring** — `_trigger_foreman`'s event vocabulary list was missing `worker-online` and `worker-offline`.
2. **`foreman/prompt.py`** — The `reason=disconnect` bullet gave no guidance on the mass-disconnect case, where a single trigger can carry multiple `[worker-offline]` lines joined by `\n`.

Diff size: 4 additions, 1 deletion. No functional changes.

---

## Analysis

### `backend/ws_handlers.py` — docstring fix

```diff
-    ``claude-auth``, ``periodic-check``.
+    ``claude-auth``, ``periodic-check``, ``worker-online``,
+    ``worker-offline``.
```

Correct and complete. The docstring now enumerates all actual callers of `_trigger_foreman`. No functional impact.

### `backend/foreman/prompt.py` — batch-disconnect clarification

```diff
+  A single message may contain multiple `[worker-offline]` lines when several
+  workers disconnect simultaneously — handle each line independently.
```

Accurate. The batch code in `routes/websocket.py` joins lines with `\n` and sends them as a single trigger:

```python
offline_lines = "\n".join(
    f"[worker-offline] worker_id={wid} reason=disconnect"
    for wid in sorted(stale_worker_ids)
)
await ws_handlers._trigger_foreman(
    guild_id,
    "worker-offline",
    offline_lines,
    task_name="foreman.worker-offline:disconnect-batch",
)
```

The prompt now correctly prepares the foreman for this multi-line format.

---

## Minor observations (non-blocking)

**Prompt guidance could be more explicit.** "Handle each line independently" is correct but terse. Stating "apply the same assessment you would for a single-worker disconnect to each line" would be more actionable for the model. The surrounding bullet text provides enough context to infer the right behaviour, so this is not a blocker.

**`worker-online`/`worker-offline` absent from CLAUDE.md protocol table.** The WS message-type table documents wire messages, not internal trigger names, so omitting them is defensible. A note as `[foreman-trigger]` sub-types would help future contributors. Low priority.

**Re-registration noise (carried forward from #435).** Every `worker-register` fires `worker-online`, including routine re-registrations. Called out in the PR description as a known acceptable trade-off — tracking first-registration state per agent would add complexity without clear payoff.

---

## Test coverage

PR #435 added three tests covering online, graceful offline, and abrupt-disconnect paths. PR #438's changes are pure documentation/prompt text — no new tests needed. Existing tests continue to validate the functional behaviour.

---

## Summary

Both fixes are correct, minimal, and targeted. The prompt addition properly documents a real edge case (batch disconnect) that the foreman would otherwise have no guidance for. The docstring fix keeps the vocabulary list accurate for future maintainers. Ready to merge.
