# Sidebar IA restructure: Workers → right, Tasks grouped by conversation

Status: plan only (no code changes).

## 1. Current state

Layout (`frontend/src/views/AppView.vue:4-8`): `TopBar` + `.app-body` = `GuildSidebar` | `MainView` | `ChatPane`.
Mobile tab bar switches `tasks | work | chat` (`activeTab`, provided as `switchMobileTab`).

| Region | Component | Contents |
|---|---|---|
| Left (360px) | `GuildSidebar.vue` | Tab bar **Tasks / Workers / Issues**; `TaskTree`, `WorkerList`, `IssuesTab`; footer `CostSummary` + connection status |
| Center | `MainView.vue` | Factory Floor + tabs for agents, workers, tasks, conversations (`ConversationDetailPanel`), issues |
| Right | `ChatPane.vue` ("FOREMAN COMMS", collapsible) | Tab bar **Chat / Conversations**; `ChatTab`, `ConversationList` |

Task tree: `TaskTree.vue` fetches `GET /guilds/{id}/tasks/tree` (`backend/routes/tasks.py:374`), refetched
(debounced 1.5s) on any task count/state change. Backend groups by
`coalesce(issue, issue closed by PR, PR)`, propagates keys through `parent_task_id`, nests via `_nest_tasks`
(a `phase='issue'` root absorbs parentless siblings). Response `TaskTreeData {nodes: TreeGroupNode[], ungrouped}`
(`types.ts:95-107`). Rows: `TaskTreeRow.vue` (recursive; special `isIssueRoot` styling).

Data model:
- `Task` (DB, `models.py:424`) **already has `conversation_id` (FK, indexed)**, stamped when a task is created from a conversation (#1271). `thread_id` is deprecated.
- Frontend `Task` type (`types.ts:73`) has **no `conversation_id`**; neither the tree query nor `TaskCreatedMsg`/`TaskAssignedMsg` WS messages carry it.
- `Conversation` (`types.ts:115`): id, name, status (active/archived/closed), created_at, updated_at. Listed by `GET /api/guilds/{id}/conversations` (newest first, cap 200); held in `stores/conversations.ts` and already selectable (`uiStore.selectedConversationId` → opens a conversation tab).

## 2. Target

- **Left sidebar = user/work side**: Tasks (grouped conversation → tasks), Issues.
- **Right sidebar = foreman side**: Chat, Conversations, **Workers**.
- A conversation node shows multiple tasks beneath it.

## 3. Proposed component structure

### 3a. Move Workers
- `GuildSidebar.vue`: remove Workers tab button, `workerCount`, `<WorkerList>`, the `.workers-section` deep style; `activeTab` becomes `'tasks' | 'issues'`. Footer (cost/connection) stays.
- `ChatPane.vue`: add third pane tab **Workers** (with live-count badge, moved `workerCount` logic using `agentsStore.workers.filter(w => w.state !== 'offline')`); render `<WorkerList>` when active. Pane `activeTab` type → `'chat' | 'conversations' | 'workers'`.
- Move the `.workers-section` override style (border-top none, flex 1, max-height none) into ChatPane scoped styles.
- `WorkerList` includes `SpawnWorkerForm` (1417 lines); verify it fits the ChatPane width (check `.chat-pane` width vs 360px) and works while pane is minimized (hidden → fine).
- Caveat: ChatPane is collapsible, so workers are hidden when minimized; the header already shows foreman state — consider also showing worker count there.
- Mobile: right pane is the "Chat" mobile tab, so Workers becomes reachable under Chat. Acceptable; alternative is a 4th mobile tab (not recommended).
- Tests: update `components/__tests__` for GuildSidebar/ChatPane; any e2e (`frontend/tests/e2e/*.spec.ts`) that clicks the Workers tab.

### 3b. Group tasks by conversation
Rename/rework `TaskTree.vue` → keep filename, change grouping; new `ConversationGroup` header (or inline) replacing the issue header.

Tree shape: `Conversation → task roots → children` (existing `TaskTreeRow` recursion for plan/execute/review/followup).

Issue relationship is **demoted, not removed**: the phase='issue' root row (`isIssueRoot`) keeps its GitHub link and now sits inside the conversation. A conversation touching several issues shows several issue-root subtrees.

## 4. Data flow / backend changes

1. **Tree endpoint** (`get_guild_tasks_tree`): add `col(Task.conversation_id)` to the select. Preferred: new response shape (keep old behind `?group_by=issue` for one release, or drop — frontend is only consumer, but ios/ and cli/ should be grepped first):
   ```
   { conversations: [{ id, name, status, created_at, updated_at, tasks: TaskTreeNode[] }],
     unassigned: TaskTreeNode[] }
   ```
   - Group by `Task.conversation_id`; propagate through `parent_task_id` (reuse the existing both-direction propagation loop, keyed on conversation id instead of issue key) so a child with NULL conversation_id joins its parent's conversation.
   - Reuse `_nest_tasks` per conversation (issue-root absorption still applies).
   - Join `Conversation` (guild-scoped) for name/status/timestamps; avoids needing the conversations store to be loaded. Conversations with no live tasks are omitted.
   - Sort: conversations with any non-terminal task first, then `updated_at`/`created_at` desc (consistent with #1380).
   - Include `conversation_id`, `issue_repo`, `issue_number` on each task so the issue-root link still renders (currently selected? `issue_*` fields are selected already).
   - The 200-task limit stays; note it now truncates across conversations.
2. **Types** (`types.ts`): add `conversation_id?: number | null` to `Task`; add `ConversationGroupNode` and `TaskTreeData` v2; remove `TreeGroupNode` if unused.
3. **WS**: add `conversationId` to `TaskCreatedMsg`/`TaskAssignedMsg` (`ws_types.py`) + `TaskCreatedWS` so `tasksStore` has it; not strictly required since `TaskTree` refetches on change, but needed if we later render from the store directly. Also regenerate `frontend/src/generated` types if generated from ws_types.
4. **Flat list** `GET /guilds/{id}/tasks`: include `conversation_id` in the serializer (used by `tasksStore`).
5. **DB**: no migration. `tasks.conversation_id` and its index exist. Backfill concern: tasks predating #1271/#1299 have NULL (or only legacy `thread_id`). Options: (a) show them in "Unassigned"; (b) one-off backfill via `thread_id → threads.conversation_id` (Thread has `conversation_id`). Recommend (b) as a small Alembic data migration if prod has many such rows — check with `debug-query` first (`SELECT count(*) FROM tasks WHERE conversation_id IS NULL AND thread_id IS NOT NULL`).
6. Frontend watch key in `TaskTree` unchanged (task count/state); also refetch when a conversation is renamed/closed (watch `conversationsStore` list).

## 5. Edge cases

- **No conversation (NULL)** — webhook/CI-spawned or legacy tasks: bottom "No conversation" group (replaces "Ungrouped"), collapsed by default if all terminal.
- **Child with different/NULL conversation than parent**: parent wins (propagation); log nothing.
- **Archived/closed conversations**: hide group if all tasks terminal (mirrors current `activeNodes` rule: open or has active tasks); show a status dot like `conversation-dot-*` in MainView.
- **Deleted/soft-deleted tasks**: still excluded by `live_tasks_filter()`.
- **Conversation missing from the join** (deleted): treat as unassigned.
- **Task spanning multiple issues / PRs**: shown under conversation via issue-root rows; PRs without an issue-root show the existing PR link on the task row.
- **Expand/collapse state**: localStorage key `pioneer_tree_expanded` uses `issue:`/`pr:` keys; use `conv:<id>` keys, old keys are inert.
- **Click behavior**: header click toggles; a small "open" affordance sets `uiStore.selectedConversationId` (opens the conversation tab, existing watcher in GuildSidebar). Task click unchanged (`selectedTaskId`).

## 6. UI mockup

```
LEFT (Tasks | Issues)              CENTER                RIGHT (Chat | Conversations | Workers)
┌ TASKS ─ ISSUES ───────────┐                             ┌ FOREMAN COMMS ──────────────┐
│ ▾ 💬 Fix login redirect  3 │                             │ [Chat][Conversations][Workers 2]
│    ⊙ #412 Login loop  ↗    │                             │ ● worker-a   working        │
│      ├ plan     ✓ done     │                             │ ● worker-b   idle           │
│      ├ execute  ● working  │                             │ [+ Spawn worker]            │
│      └ review   ○ pending  │                             └─────────────────────────────┘
│ ▾ 💬 Add dark mode       1 │
│      └ execute  ● working  │
│ ▸ 💬 Docs cleanup (closed) │
│ ▾ No conversation        2 │
│      ├ ci-fix   ✓ done     │
│      └ ...                 │
│ CostSummary / Connected    │
└────────────────────────────┘
```

## 7. Implementation sequence (separate PRs, each independently shippable)

1. **Workers → right** (frontend only): GuildSidebar/ChatPane edits + tests. Low risk.
2. **Backend**: `conversation_id` on `/tasks` and `/tasks/tree` (new `conversations` shape, tests in `backend/tests` next to existing tree tests), optional backfill migration, WS fields.
3. **Frontend grouping**: types, `TaskTree.vue`/group header, expand-state keys, spec tests (`sidebar/__tests__`), `npm test && npm run type-check`, screenshots in PR.

## 8. Open questions

- Keep the issue-grouped view as a toggle, or fully replace? (Plan assumes replace; issues remain reachable via Issues tab and issue-root rows.)
- Should Workers stay visible when ChatPane is minimized?
- Is the tree endpoint consumed by ios/ or cli/? (grep before changing response shape.)
