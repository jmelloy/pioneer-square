# Claude console compaction investigation

## Sources and exports

Read production data using `POST /debug/query` with the operator's `DEBUG_TOKEN`.
Queries were SELECT-only. Inspected all 785 stored log rows for `t-mu75ra`, event
counts across the ten latest worker tasks (1,855 rows), and local Claude session
transcripts. The task's conversation is 596; it also contains `t-vpqlaf` (312 rows).
The investigation itself did not change application behavior or production state.
The implementation following it is recorded at the end of this document.

Private exports in `/tmp/pioneer-claude-t-mu75ra/` (directory 0700, files 0600):

| File | Contents |
| --- | --- |
| `task-logs.json` | All 785 production task-log rows, ordered by ID. |
| `claude-details.json` | All 437 stored structured details, including tool input/output. |
| `stream-events.json` | The 155 stored `claude_json` events with log IDs/timestamps. |
| `recent-task-event-counts.json` | Event counts across ten latest production worker tasks. |
| `transcript.jsonl` | Complete local Claude transcript, including API-request bookkeeping. |
| `messages.json` | Its 398 assistant/user records. |
| `tool-pairs.json` | Its 121 calls paired with original results by exact tool ID. |

These exports contain prompts, commands, and outputs; intentionally not committed.
The local transcript covers session `8c90efca-78b1-49a5-b625-9429cff1fce3` only.
Production logs also contain events from `3cbf7efe-f324-45b1-8aac-7ed0f085ad15`.
The stored production events are not a lossless stdout capture: the runner already
simplified normal tool blocks and truncated some notification summaries.

## Findings from production

| Stored entries | `t-mu75ra` | Ten latest worker tasks |
| --- | ---: | ---: |
| Total | 785 | 1,855 |
| Plain text/status/thinking | 348 | 901 |
| Tool calls | 141 | 329 |
| Tool results | 141 | 329 |
| Task started | 33 | 66 |
| Task notification | 33 | 66 |
| Hook started / response | 20 / 20 | 28 / 28 |
| Rate-limit updates | 12 | 43 |
| Tool progress | 6 | 22 |
| Code change published | 15 | 21 |
| VCS state changed | 13 | 15 |
| Background tasks changed | 2 | 4 |
| Task updated | 1 | 3 |

### 1. `parent_tool_use_id` is useful here, but these are heartbeats

All six example progress events, and all 22 in the ten-task sample, have
`heartbeat: true`. Example, with shortened IDs:

```json
{
  "type": "tool_progress",
  "tool_use_id": "toolu_...-heartbeat-1",
  "parent_tool_use_id": "toolu_...",
  "tool_name": "Bash",
  "elapsed_time_seconds": 60,
  "heartbeat": true,
  "session_id": "8c90efca-78b1-49a5-b625-9429cff1fce3"
}
```

The six events refer to five parent calls. One call has updates at 30 and 60
seconds. **Grouping by the progress event's own ID would fail:** its synthetic
ID changes per heartbeat. For `heartbeat: true`, use the parent ID and replace
its elapsed-time preview. Do not infer that every non-null parent means a nested
Agent: the local example transcript has 121 Bash calls and no Agent calls.

For actual nested-agent streams, a parent ID groups children; individual child
calls still need their own IDs so concurrent siblings do not overwrite each
other. That case was not present in this sample.

### 2. Task start/completion is the larger immediate win

All 33 starts pair exactly with 33 notifications by `(session_id, task_id)`.
Notifications report 32 completed and one failed task. Lifecycle events also
retain `tool_use_id`, so future logs can attach them to the originating call.

These are mostly `task_type: local_bash`, including starts with
`is_backgrounded: false`, not exclusively explicitly backgrounded jobs.
Start descriptions can be the entire shell command, and notifications often
repeat it. The example's descriptions range from 21 to 852 characters; the ten
latest tasks include a **3,846-character** description (`t-o01jyp`).

`_truncate_task_summary` handles `task_started` but only caps `summary`;
`claudeEventSummary` renders `description`. Therefore start descriptions bypass
the cap. Notification summaries are capped at 500 *inside the stored event*,
which also destroys their full text for the JSON viewer.

Use one lifecycle row per task: start → latest status → completion summary,
with earlier events expandable. Bound and flatten the displayed preview, not
the raw JSON. Prefer the originating Bash input's human-readable `description`
when available; the lifecycle event's `description` may just be a long command.

### 3. More cheap noise reductions are available

- All 20 hook pairs match by `(session_id, hook_id)` and all responses report
  `outcome: success`, `exit_code: 0`, `hook_event: SessionStart`. Hide successful
  setup details behind a compact group; expose hook failures and errors.
- All 12 rate-limit events in this task have identical `rate_limit_info` and
  `status: allowed`. Deduplicate unchanged state within the session; retain
  meaningful warning/recovery transitions.
- 15 code-publication and 13 VCS notifications add more noise. They may describe
  related pushes, but do not share an explicit pairing ID; don't blindly merge
  them by adjacency. Consider a repository-activity group later, preserving
  distinct branches/URLs/actions.

## Why current grouping misses this

`LogList.vue` groups consecutive `tool_use` / `tool_result` / long `thinking`
entries. Every raw progress, task, hook, or rate-limit event breaks the group.
Short thinking also breaks it because the runner omits `toolType: thinking`.

`parse_claude_event` drops normal calls' `id`, results' `tool_use_id` and
`is_error`, and enclosing events' parent/session metadata. Assistant text also
loses its enclosing-event metadata. Raw fallback events retain those fields.
Thus historical lifecycle and heartbeat events can be grouped now, but exact
historical call/result association generally cannot be recovered from Pioneer
logs. Do not guess by adjacency with interleaved calls.

The local transcript retains IDs: its 121 calls all have matching results,
including three `is_error: true` results. Replaying an error result through the
current parser confirms its detail only contains `toolType` and `output`.
Preserving errors requires fixing this before broad tool-pair compaction.

Sources:

- `worker/pioneer_worker/claude_runner.py::parse_claude_event`,
  `_truncate_task_summary`, `_claude_json_detail`.
- `frontend/src/components/log-pane/LogList.vue::isToolLine/groupedLogs`.
- `frontend/src/utils/claudeEventSummary.ts`.

## Measured compaction opportunity

The current grouping rules project the 785 persisted rows to **645 top-level
rows**, including 30 tool groups containing 89 calls. This is a source-level
projection, not a screenshot or browser measurement.

A conservative completed-task projection:

1. Replace each matched start/notification pair with its notification.
2. Fold the one completed task update into its notification.
3. Hide successful hook pairs in collapsed setup details.
4. Remove standalone heartbeat rows, retaining them as call activity details.
5. Deduplicate identical rate-limit state within each session.

This removes **90 standalone stored-row presentations**: 785 → 695 before tool
grouping. Applying existing grouping afterward yields **645 → 544 top-level
rows (~16% fewer)**, because fewer metadata events split tool runs. No completion
or failure notification is removed. Raw rows should remain available on expand;
the projection must not delete stored logs.

This estimate does not include pairing the 141 tool calls/results, compacting
ordinary thinking/status lines, or grouping repository activity. Merely replacing
the two repeated heartbeat rows with one latest update saves only one row in this
example: task lifecycle and setup noise are the larger opportunities.

## Recommended implementation order

### First: frontend-only projection for existing logs

- Single-line, bounded lifecycle previews with full JSON on expand.
- Group lifecycle by session/task ID and hooks by session/hook ID.
- Replace heartbeat previews by parent call ID; avoid standalone heartbeat spam.
- Deduplicate unchanged rate-limit state, not warning/recovery transitions.
- Preserve failure states even if later progress arrives; keep unknown events.
- Keep raw history accessible and use stable group keys while streaming.

This belongs in the shared log-list projection, so it reaches both the console
and `/task/:id/log`. Add tests around interleaving, lifecycle completion/failure,
streaming updates, raw expansion, and old logs without IDs.

### Then: retain minimal pairing metadata for future logs

Keep call ID, result tool ID, result `is_error`, parent ID, and session/invocation
scope in details. Include context for assistant text if nested text is collapsed.
Don't duplicate entire assistant messages in every tool block. Carry invocation
identity even when an individual event omits `session_id`.

Attach exact results/progress/lifecycle to expandable call rows. For heartbeats,
the call ID is the parent ID; for ordinary tool results it is `tool_use_id`.
Never group all null-parent events. Scope by Pioneer task plus Claude invocation
and keep distinct concurrent child calls. Legacy/unmatched entries stay visible.

Existing `TaskLog.data` and WS detail transport can carry these additive fields
without dedicated database columns. Full event preservation should replace
worker-side destructive summary truncation.

### Discord: same intent, simpler delivery

`_format_stream_entries` only groups normal tool calls/results and drops result
output. It does not use the frontend's event summaries: raw task/progress events
pass through as `[claude-json] ...` labels and split groups. Thinking splits its
groups too. Batches flush after four seconds or 1,500 characters.

First suppress heartbeat/unchanged rate-limit/setup-success noise and emit short,
friendly task completion/failure summaries. Preserve failure visibility and link
to the task log. Cross-batch latest-only replacement would need message-ID state
and Discord edits; skip that until the simpler filtering is measured. Avoid
suppressing an initial status indefinitely for genuinely long-running tasks.

Source: `backend/discord_notifier.py::_format_stream_entries`,
`notify_task_stream`, `_STREAM_FLUSH_INTERVAL`, `_STREAM_FLUSH_CHARS`.

## Change surface for implementation

- `worker/pioneer_worker/claude_runner.py` and `worker/tests/test_claude_runner.py`.
- `frontend/src/components/log-pane/LogList.vue`,
  `frontend/src/utils/claudeEventSummary.ts`, their tests, and explicit metadata
  types in `frontend/src/types.ts` as appropriate.
- `LogPane.vue` and `views/TaskLogView.vue` already share `LogList`.
- `backend/discord_notifier.py` and formatter tests.
- Verify task/conversation WS and persisted detail round-trips. Transport sources:
  `backend/ws_handlers.py::handle_terminal_output`, `backend/models.py::TaskLog`.

## Implemented follow-up

- Shared web log projection groups lifecycle, setup hooks, parent-keyed heartbeats,
  and unchanged rate-limit updates, keeping all original entries expandable.
  Failure/completion previews cannot be replaced by late progress. Task previews
  are flattened and bounded to 160 characters per description/summary.
- Discord omits heartbeat/setup-success/snapshot noise and redundant task starts,
  deduplicates rate state across batches, and formats short completion/failure
  summaries. Tool-use rows still show what started. Unknown events remain visible;
  task-update terminal states and hook/tool failures are retained. Fully suppressed
  batches do not produce header-only messages.
- Worker-side destructive lifecycle-summary truncation was removed; new tool
  results retain `is_error` so both renderers can keep failures outside tool groups.
- Exact call/result metadata pairing and actual nested-Agent compaction remain
  deferred. No cross-message Discord editing or database migration was added.

Replaying the actual implementation against the private production export gives
**645 → 549 top-level web rows (~15% fewer)**. All **785 original rows** are
retained exactly once inside the projection. This differs from the earlier
544-row estimate because heartbeat groups remain visible rather than disappearing.
An offline batched Discord replay retained the failed task notification and emitted
short completion summaries without raw task-start labels; no Discord messages
were sent during verification.

Validation: frontend suite 336 passed / 4 skipped, TypeScript checks and production
build passed; worker suite 484 passed; Discord, terminal routing, and conversation
service tests 136 passed. Ruff and targeted frontend ESLint/Prettier checks passed.
Existing Vite/deprecation/worker-test runtime warnings remain.

No deploy, browser validation, or real nested-Agent-stream validation was performed.
