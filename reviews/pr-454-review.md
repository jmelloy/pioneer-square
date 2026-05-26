# Review: PR #454 — Fix codex_runner readline buffer overflow

**PR:** https://github.com/jmelloy/pioneer-square/pull/454  
**State:** Merged  
**Author:** jmelloy  

---

## Overview

One-line fix: adds `limit=8 * 1024 * 1024` to `asyncio.create_subprocess_exec` in `codex_runner.py:run_codex_auto`. Raises the asyncio `StreamReader` buffer from the 64 KB default to 8 MB, preventing `ValueError: Separator is not found, and chunk exceed the limit` when Codex emits a JSON line longer than 64 KB.

---

## What's Good

- Minimal, targeted change that addresses the crash directly.
- Comment explains the *why*.
- Correctly mirrors the same concern addressed in `claude_runner.py`.

---

## Issues Found

### 1. Limit value inconsistency with `claude_runner.py`

`claude_runner.py` defines `STDOUT_LINE_LIMIT = 16 * 1024 * 1024` (16 MiB) as a named constant (line 174) and uses it in `create_subprocess_exec`. This PR inlines `8 * 1024 * 1024` (8 MiB) without a constant. If Codex emits stream-json of the same shape as Claude, the lower ceiling is arbitrary and the magic number is harder to audit.

**Recommendation:** Define a `CODEX_LINE_LIMIT` constant (or reuse a shared constant) matching the 16 MiB ceiling from `claude_runner.py`.

### 2. No `LimitOverrunError` fallback — crash still possible

The async `for raw in proc.stdout` at `codex_runner.py:107` has no `LimitOverrunError` handler. If a single Codex stdout line exceeds 8 MB, asyncio will still raise `LimitOverrunError` — uncaught — killing the runner. `claude_runner.py` handles this gracefully (lines 307, 321). `codex_runner.py` should do the same.

### 3. `stderr` stream unprotected

The `_drain_stderr()` inner function (`codex_runner.py:99–103`) iterates `proc.stderr` with no error handling. The `limit` parameter to `create_subprocess_exec` applies to both streams, so stderr is now also 8 MB — but if a stderr line exceeds that, the crash recurs. `claude_runner.py` handles `LimitOverrunError` in its `_drain_stderr` (line 188).

### 4. `pi_runner.py` has the same latent bug

`pi_runner.py:71` calls `create_subprocess_exec` with no `limit` override and no `LimitOverrunError` handling. Same class of bug.

---

## Recommended Follow-ups

```python
# codex_runner.py — add constant + LimitOverrunError guards

CODEX_LINE_LIMIT = 16 * 1024 * 1024  # match claude_runner

# In create_subprocess_exec:
limit=CODEX_LINE_LIMIT,

# _drain_stderr with guard:
async def _drain_stderr() -> None:
    try:
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line:
                await emit(f"[stderr] {line}")
    except asyncio.LimitOverrunError:
        await emit("[stderr] <line exceeded buffer limit, truncated>")

# stdout loop with guard:
async for raw in proc.stdout:
    try:
        line_str = raw.decode(errors="replace").strip()
        ...
    except asyncio.LimitOverrunError:
        await emit("[codex] <stdout line exceeded buffer limit, skipping>")
        continue
```

---

## Verdict

The fix resolves the immediate crash for the common case (stdout lines < 8 MB) and is safe. Three follow-up issues remain: matching the 16 MiB ceiling from `claude_runner`, adding `LimitOverrunError` guards on stdout and stderr, and applying the same fix to `pi_runner.py`.
