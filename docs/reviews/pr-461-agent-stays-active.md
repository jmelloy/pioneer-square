# PR #461 Review: Fix agent stays active after task completes

**PR:** https://github.com/jmelloy/pioneer-square/pull/461  
**Date:** 2026-05-26  
**Verdict:** Approve with minor suggestions

## What the PR Does

Fixes a race condition where short-lived tasks complete before the worker's `agent-state: idle` WebSocket frame arrives, leaving the sidebar showing the agent as permanently working. The fix adds handlers for `task-complete` and `task-followup-done` in `agents.ts` to proactively reset agent state, with a fallback from `agentId` to `taskId` lookup for resilience.

## Key Findings

### Multi-round followup flicker
When a task has multiple followup rounds, the agent will briefly flash idle between rounds:
`working → idle (task-followup-done) → working (task-followup arrives) → idle...`

This trades the "stuck working" bug for a transient flicker. Acceptable tradeoff, worth documenting.

### `updateAgentState` return value
Returning `boolean` from `updateAgentState` slightly muddles its responsibility (now doubles as a lookup predicate). Not a blocker.

### Test coverage
7 tests (PR says 5) — double-update idempotency and deregistration-race tests are strong regression guards. Missing: multi-round followup flicker test.

## Suggestions (non-blocking)
1. Add a comment or follow-up issue about the `working → idle → working` flicker on multi-round followups.
2. Consider a dedicated `findAgentById` helper to keep `updateAgentState` single-responsibility.
3. Add a test for the multi-round followup scenario to prevent silent regressions.
