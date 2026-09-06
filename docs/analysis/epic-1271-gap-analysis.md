# Epic #1271 — Deep Analysis: Implementation Gaps

Status: complete. Posted as a comment on issue #1271.

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

## Finding: Concrete dual-write gaps — several creation sites never stamp `conversation_id`

`Conversation` has no `deleted_at`/soft-delete (models.py:176 — inherits only `SQLModel`, unlike
`Thread`/`Task`/`Guild` which mix in `SoftDeleteMixin`), so conversations can't be soft-deleted the
way threads can — likely fine but worth a deliberate decision, not an oversight.

`Thread`'s class docstring (models.py:248-256) calls it deprecated, but its `name`/`status`/
`discord_thread_id` fields are still live and populated (models.py:275-277) — mirrored, not removed,
which is correct for the current migration stage but means Thread is not actually a
"compatibility-only" table yet as Phase 7 intends.

**Task creation sites that never stamp `thread_id` or `conversation_id` (4 found)** — tasks created
here can never be attributed to a Conversation, and by extension neither can any GitHub event/issue/PR
correlated through them:
- `backend/routes/agents.py:58-71` — interactive Pi task creation (`start_agent_run`)
- `backend/routes/discord.py:429-441` — `/pickup` slash command
- `backend/routes/discord.py:498-511` — `/review` slash command
- `backend/routes/workers.py:359-401` — worker-dispatch `assign_task` REST route

**Message creation gap:** `backend/routes/webhooks.py:1270-1336` (`ci_notify` endpoint) resolves a
`task_row` and could stamp `task_row.conversation_id` (the sibling `github_webhook` handler does
exactly this at webhooks.py:762-786), but its `Message(...)` insert at lines 1328-1336 sets only
`task_id` — no `conversation_id`, no `thread_id`. CI-notify chat messages never join a conversation.

**GitHub cache backfill gap:** `scripts/backfill_github_cache.py:74,77` call
`github_cache.upsert_issue`/`upsert_pr` with no `conversation_id` argument, defaulting to `None` —
even when a matching `Task` with a resolvable `conversation_id` already exists for that
issue/PR's `(repo, number)`. This is one of only two documented upsert entry points for
`GithubIssue`/`GithubPullRequest` (models.py:762-767, 806-810) and it silently skips conversation
attribution.

**Migrations are correct.** All 4 alembic migrations for this epic (`20260904_000000` through
`20260904_020000`) backfill in correct dependency order and the previously-known backfill-ordering
bug (PR #1274 / commit `3edde4a`) is already fixed in the working tree, with a regression test added.

**Reads are mostly migrated correctly** — `routes/threads.py:214` (message history),
`db/github_events.py:25-27`, and `foreman/history.py:88-91` all already read by `conversation_id`.
The two remaining `Task.thread_id` reads (`thread_service.py:175`, `thread_maintenance.py:135`) are
either paired with an equivalent conversation_id lookup or legitimately Thread-instance-scoped
cleanup, not gaps.

## Finding: THE HEADLINE ACCEPTANCE CRITERION IS UNMET — `Conversation` is still a 1:1 singleton per `(guild_id, user_id)`

The epic's entire premise (from the issue body): *"Each new top-level human Foreman message starts
a new `Conversation` unless the message is explicitly posted inside an existing conversation."* This
is what makes Conversation a real "unit of work" instead of a per-user bucket.

**This was never implemented.** `foreman/conversation_service.get_or_create_conversation`
(conversation_service.py:34-54) is an explicit singleton getter — its own docstring and
`models.py:181-184` and `foreman/history.py:76-78` all restate "`Conversation` is 1:1 with
`(guild_id, user_id)`." A returning user's new top-level message reuses the same `Conversation` row
forever; only the very first message from a `(guild, user)` pair ever creates one. This is *exactly*
the pre-epic behavior described in the epic's own "Problem" section (`Conversation`: per
`(guild_id, user_id)` anchor/memory bucket) — unchanged.

Because of this, several other "done" phases are hollow by construction:
- Phase 3's "full conversation history" is identical to the old windowed per-user history, since
  there's only ever one conversation per user to load history from.
- The acceptance criterion "A top-level human message creates a new Conversation row and **no core
  Thread row is required**" is also false today: a `Thread` row is still created alongside every
  `Conversation` (confirmed via `tests/test_conversation_service.py::TestConversationIdDualWrite`,
  and `models.py:248-256`'s own docstring: "Removing `Thread` entirely ... hasn't happened yet").

No test asserts "a new top-level message creates a new Conversation" — and none could pass against
current code, since the code deliberately prevents it.

## Test coverage audit summary

- Suite is structurally sound: `pytest --collect-only` reports **1419 tests collected, 0 collection
  errors**. Could not execute against a real run (no Postgres available in this sandbox — pure
  environment blocker, not a code defect) so no pass/fail counts are available from this analysis.
- 22 test files touch conversation/thread code, covering: history windowing, the UI-lifecycle
  migration, `conversation_service.py`, conversation routes, Discord thread mirroring, thread
  maintenance/sweeping, `reactivate_conversation_thread` (both unit and integration level — solid),
  and dual-stamping of `Task.conversation_id`/`thread_id`.
- **Gap (a)**: No test exercises an unwindowed/full history load by `conversation_id` — consistent
  with the fact that no such code path exists (see Phase 3 finding above).
- **Gap (b)**: Followup routing (`working`→redirect, `awaiting-review`/`done`/`parked`→followup) is
  well tested, but *only* for Discord-thread-bound replies (`discord/router.py`'s
  `_route_task_bound_reply` + `tests/test_discord_router.py`). No equivalent routing or test exists
  for a plain in-app/web `Message` reply inside a `Conversation` — consistent with the Phase 5 finding.
- **Gap (c)**: The most consequential migration, `20260904_000000_add_conversation_id_columns.py`
  (backfills `messages`/`tasks`/`foreman_turns`/`github_events`), has **no dedicated migration-replay
  test**. Only the later, narrower UI-lifecycle migration (`...020000`, PR #1286) has one
  (`test_conversation_ui_lifecycle_migration.py`). The `...010000` github_cache migration is also
  untested at the migration level (only its runtime helpers are tested).
- **Gap (d)**: Confirmed above — untestable because unimplemented.
- No skipped/xfail tests found in any conversation/thread-related test file.

## Epic PRs reviewed

## Finding: Phase 3 (full conversation history) and Phase 5 (conversation followups) are NOT implemented

**This is the epic's headline goal and it has not landed**, despite all 6 sub-issues being closed.

- `backend/foreman/history.py` `ConversationHistory._windowed_turns` (history.py:59-111) queries
  `conversation_id == X OR (guild_id == G AND user_id == U)` — `conversation_id` is an **OR-fallback
  addition**, never the sole/primary key. The docstring (history.py:71-81) admits the `(guild_id,
  user_id)` match is "kept as an OR fallback rather than dropped."
- `_HUMAN_TURN_WINDOW` (constants.py:9, value 3) is still applied **unconditionally** in
  `_windowed_turns` (history.py:102-111) — there is no branch that removes windowing for
  conversation-scoped runs. Phase 3 explicitly asked for this trimming to be removed for
  conversation-scoped history; it wasn't touched.
- Because `Conversation` is still enforced 1:1 with `(guild_id, user_id)` (models.py:176-196,
  `conversation_service.get_or_create_conversation` at conversation_service.py:34-54), "full
  conversation history" and "windowed per-user history" are currently identical by construction —
  there's no way for a conversation to span more history than the old per-user window already did.
- `conversation_service.py`'s own header docstring (lines 1-23) states it "deliberately does not
  touch history loading (still windowed by (guild_id, user_id)...)" — the incompleteness is
  self-documented in prose, not flagged via TODO, so a naive TODO/FIXME grep misses it entirely.
- No `POST /api/guilds/{guild_id}/conversations/{conversation_id}/messages` endpoint exists
  (`routes/conversations.py` only has PATCH rename, PATCH close, GET detail — conversations.py:70-112).
  Chat is still dispatched only through the generic WS `handle_chat` → `trigger_foreman("chat", ...)`
  path keyed by `(guild_id, user_id)` (ws_handlers.py:516-590), not a conversation-scoped route.
- The followup decision logic (send worker input / `send_followup` / open PR / `get_task_status` /
  just answer) exists, but only as generic LLM system-prompt policy (prompt.py) + tool schema
  (tools_schema.py) operating on the guild-wide task list — there is no code that inspects "does
  *this conversation* have an active/awaiting-review task" before deciding, because conversations
  aren't yet the unit of task correlation for this decision.
- Minor: `touch_conversation()` (conversation_service.py:92-96) is dead code in production — only
  referenced from `backend/tests/test_conversation_service.py:143`.
- Minor: `reactivate_conversation_thread` (promised as a `conversation_service.py` function per the
  epic plan) actually lives in `backend/foreman/thread_service.py:221`, not `conversation_service.py`
  — a naming/location inconsistency, not a functional gap (PR #1285 uses it correctly).
- Edge case: `_notify_queued_turn_failure` (runner.py:1330-1379) can leave `conversation_id` null on
  a `ForemanTurn`/`Message` when `guild_pk_val` can't be resolved (runner.py:1346-1350).

## Finding: Phase 7 (Discord mirror-only) is partial, Phase 8 (API/frontend rename) is not started

- Primary Discord session/channel resolution is still `Thread`-keyed: `router.py`'s
  `_resolve_foreman_thread_session` (router.py:208-236) joins `Guild → Conversation → Thread` on
  `Thread.id`, not `discord_thread_id`. Only the final ad-hoc-chat message-persistence step
  (`_persist_inbound_message`, router.py:829-870) has a `discord_thread_id → conversation_id`
  shortcut via `get_conversation_by_discord_thread_id`, and only when `user_id` is set and no
  `task_id` — the "inbound Discord replies resolve via conversation_id" acceptance criterion is not
  true as the primary/general path.
- Mirror entry points are still named `on_thread_created` (thread_mirror.py:46) /
  `on_thread_updated` (thread_mirror.py:138) — not renamed to `on_conversation_created/updated` as
  Phase 7 asked. `_stamp_discord_thread_id`, `_get_discord_thread_id`, and
  `relay_discord_thread_event` all still query/key off the `Thread` table.
- `gateway.py`'s `_sync_thread_status` (line 176) is dead code explicitly marked
  "DEPRECATED (issue #1168): no longer called" but still present and still `Thread`-referencing —
  a cleanup candidate.
- `discord_notifier.py`'s "legacy conversation binding path" (`_ensure_conversation_thread`,
  ~line 782) is **not dead code** — it's a live, reachable fallback in `notify_foreman_chat`
  (lines 884-898) used whenever the new Conversation-model lookup misses. The epic's original audit
  flagged this as legacy; it hasn't been removed or folded in.
- **No `GET /api/guilds/{guild_id}/conversations` list route exists** — `routes/conversations.py`
  only has PATCH rename, PATCH close, and GET single-detail (conversations.py:70-112).
  `routes/threads.py` remains the only list/CRUD surface the frontend actually calls.
- **No `conversation-created`/`conversation-updated` WS events exist anywhere.**
  `backend/ws_types.py:373-394` defines only `ThreadCreatedMsg`/`ThreadUpdatedMsg`; a repo-wide grep
  for the conversation-event string literals returns zero hits. Only `thread-created`/`thread-updated`
  are ever broadcast (`thread_service.py:308-316`, `:376-386`).
- **Frontend has had zero commits related to this epic.** `git log --since=2026-09-01 -- frontend/`
  shows only a dependency bump, prettier formatting, a shareable task-log-viewer feature, and a
  worker/pi-provider fix — nothing touching `stores/threads.ts`, `types.ts`, or the sidebar/chat
  components. `ThreadList.vue`, `ThreadDetailPanel.vue`, `NewThreadModal.vue`, `ChatPane.vue`, and
  `useThreadsStore` remain entirely thread-centric, backed by `/threads` REST + `thread-created`/
  `thread-updated` WS events only. Phase 8 is not started.

## Recommendations / suggested remaining work

Roughly in priority order:

1. **Decide, explicitly, whether the epic's core premise still stands.** All 6 sub-issues are
   closed and the epic reads as done, but the foundational behavior change — new top-level message
   ⇒ new Conversation, replacing the 1:1 `(guild_id, user_id)` singleton — was never built. Everything
   else (dual-write columns, field migration, Discord binding) is scaffolding for that change, not
   the change itself. This needs a new sub-issue and probably a design pass: switching `Conversation`
   from singleton to multi-per-user has real implications for history loading, Discord thread
   creation cadence, and existing UI assumptions (`ThreadList.vue` currently lists one thing per
   user-ish). Recommend re-opening the epic or filing a clearly-scoped follow-up issue rather than
   treating it as done.
2. **Phase 3 — full conversation history.** Once/if (1) lands, `ConversationHistory._windowed_turns`
   (history.py:59-111) needs an actual conversation-scoped code path that drops `_HUMAN_TURN_WINDOW`
   trimming instead of only widening the OR-fallback match.
3. **Phase 5 — conversation-scoped followups.** Add the `POST .../conversations/{id}/messages`
   endpoint and generalize the followup decision logic in `discord/router._route_task_bound_reply`
   (currently Discord-thread-reply-only) to plain in-app conversation replies.
4. **Fix the 4 task-creation dual-write gaps** so all tasks are attributable to a conversation:
   `routes/agents.py:58-71`, `routes/discord.py:429-441`, `routes/discord.py:498-511`,
   `routes/workers.py:359-401`.
5. **Fix `webhooks.py`'s `ci_notify` message gap** (lines 1270-1336) to stamp `conversation_id` the
   same way `github_webhook` does.
6. **Fix `scripts/backfill_github_cache.py`** to resolve and pass `conversation_id` into
   `upsert_issue`/`upsert_pr`, matching the webhook path.
7. **Add a migration-replay test** for `20260904_000000_add_conversation_id_columns.py` (the
   messages/tasks/foreman_turns/github_events backfill) and for `20260904_010000` (github_cache) —
   these are the two most consequential migrations in the epic and are currently the only two
   without dedicated tests, while a narrower later migration does have one.
8. **Phase 7 cleanup**: rename `thread_mirror.on_thread_created/updated` toward conversation
   semantics, or explicitly document why they stay thread-named; make primary Discord session
   resolution (`router._resolve_foreman_thread_session`) conversation-first instead of
   `Thread.id`-first; decide whether `discord_notifier.py`'s legacy `_ensure_conversation_thread`
   fallback should be removed now that the new lookup exists; remove the dead
   `gateway._sync_thread_status` (already marked deprecated since #1168, never called).
9. **Phase 8 — not started.** Add `GET /api/guilds/{guild_id}/conversations` list route, add
   `conversation-created`/`conversation-updated` WS event types, and begin the frontend rename
   (`stores/threads.ts`, `types.ts`'s `ConversationThread`, `ThreadList.vue`,
   `ThreadDetailPanel.vue`, `NewThreadModal.vue`, `ChatPane.vue`) — zero frontend work has happened
   during this epic.
10. **Minor cleanup**: `conversation_service.touch_conversation()` is dead code outside tests;
    either wire it in (e.g. on every inbound message) or remove it. Consider moving/renaming
    `thread_service.reactivate_conversation_thread` into `conversation_service.py` for naming
    consistency with the epic's promised function list.
