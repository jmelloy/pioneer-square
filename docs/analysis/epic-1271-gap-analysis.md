# Epic #1271 — Deep Analysis: Implementation Gaps

Status: in progress (research phase)

## Epic PRs reviewed
- #1280 (merged 2026-09-04) — Make Conversation the core Foreman thread model (retry of closed #1272)
- #1281 (merged 2026-09-04) — Scope Message conversation history to conversation_id
- #1282 (merged 2026-09-04) — Link Task to Conversation
- #1283 (merged 2026-09-04) — Update ForemanTurn to use Conversation context
- #1284 (merged 2026-09-04) — Link GitHub events/issues/PRs to Conversation
- #1285 (merged 2026-09-05) — Discord thread mirroring for Conversation
- #1286 (merged 2026-09-06) — Move Thread UI/lifecycle fields into Conversation model, deprecate Thread

Sub-issues #1274–#1279 all closed, one per PR above (#1272 was an earlier closed/unmerged
attempt superseded by #1280).

## Research in progress
Four parallel research passes launched against current `main`-equivalent working tree:
1. Schema/dual-write audit (Conversation/Thread fields, conversation_id vs thread_id writes/reads, migration backfill correctness)
2. Foreman history/runner audit (full-history-by-conversation, followup decision logic, conversation_service.py completeness)
3. Discord mirroring + frontend rename audit
4. Test coverage audit

Findings to be appended below as they complete.
