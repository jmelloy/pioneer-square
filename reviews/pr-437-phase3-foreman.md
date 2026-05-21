# Review: PR #437 — Fix merge conflicts, phase-3 standalone foreman

Reviewed PR #437 (rebase of `claude/phase-3-standalone-foreman-package` onto main).
Full review posted as GitHub PR comment. Summary of findings below.

## Blockers

- **exec_tool / update_turn_tokens over-authorized** (`backend/routes/foreman.py`):
  Both new endpoints use `require_worker_or_member_path`, allowing any authenticated
  worker or member to invoke foreman tools (`create_task`, `cancel_task`,
  `shutdown_worker`, etc.). Restrict to `foreman:jwt` callers.

## Medium

- **Deferred imports inside hot-path auth function** (`backend/auth_deps.py`):
  `import os` and `from utils import verify_foreman_jwt` are inlined inside
  `authorize_worker_or_member`. Move to module level or a dedicated `jwt_utils.py`.

- **`_FakeToolUse` duck-type is fragile** (`backend/routes/foreman.py`):
  The exec_tool route instantiates a private class to satisfy `_exec_one_tool`'s
  interface. Refactor `_exec_one_tool` to accept plain `name/id/input` kwargs instead.

## Low

- Dead code `_tool_use_ts = _now  # noqa: F841` in `foreman/pioneer_foreman/runner.py`
- `anyio` listed in `[project.dependencies]` but never imported; belongs only in test extras
- CLAUDE.md docker compose example missing `PIONEER_FOREMAN_KEY` (README was updated, CLAUDE.md was not)
- Bedrock support removed without documentation

## Non-issues

- Stdlib-only HS256 JWT — intentional, no PyJWT dep needed
- `JWTTokenManager` proactive 5-min refresh buffer — correct
- WS `join` without token — consistent with existing foreman protocol
- `asyncio.gather` in `exec_tools` — mirrors embedded runner concurrency
- `strip_orphaned_tool_results` — necessary guard for cross-process history sharing
