# Conversation, Discord, and PR webhook audit

## Scope and method

- Target: `https://pioneer-square.melloy.life`, authenticated with the existing `DEBUG_TOKEN` through `POST /debug/query`. Token never printed or saved.
- Production observations: 2026-10-26, approximately 17:35–17:42 UTC; rolling seven-day queries. Counts are snapshots, not a transactionally consistent export.
- Source reviewed: local HEAD `4766dd6`. The debug endpoint does not establish the deployed source revision; production evidence and local reproductions are distinguished below.
- Production operations were read-only SQL. No Discord messages, webhook replays, deployments, or data repairs were performed.
- Local reproductions exercised real Python functions with database/network boundaries mocked. They demonstrate failure paths, not their production frequency.

## Summary

**Conversation IDs being non-null is not sufficient: task creation can attach work to the wrong conversation.** Production contains concrete examples. Discord also has several paths where an inbound message gets neither a Foreman turn nor an actionable failure response.

| Seven-day check | Observation |
| --- | --- |
| New tasks | 123; none missing a conversation ID |
| Foreman turns | 5,569; none missing a conversation ID |
| Task-linked GitHub events | 16,562; none missing or disagreeing with their task's conversation ID |
| Task-linked Foreman turns | 3,420; none disagreeing with their task's conversation ID |
| Discord inputs already present in Foreman turns | 21; all had a later assistant turn in the same conversation within ten minutes |
| `create_task` results disagreeing with created task's conversation | Three distinct tasks confirmed |
| GitHub events without a conversation ID | 128,339 of 144,901; many are legitimately unrelated CI/repository activity |
| Previously unlinked events now matching a task's exact guild/repo/PR | 4,968 distinct events, across 87 tasks; multiple tasks can match one event |

The 21/21 Discord result is **not** an inbound-delivery success rate: messages dropped before the Foreman starts never appear in `foreman_turns`. An assistant turn also does not prove a Discord message was delivered.

## Findings

### 1. High: task tools discard the run's conversation identity

**Production-confirmed attribution defect; locally reproduced.**

`RunConfig` and the journal/history have the correct `conversation_id`, but `ToolExecutor.exec`, `RealToolExecutor`, `exec_tools`, and `ToolContext` carry only guild/user identity. Both task-creation paths independently select the user's most recently updated conversation:

- `backend/foreman/run.py`: `ToolExecutor`, `RealToolExecutor`.
- `backend/foreman/tool_context.py`: `ToolContext` has no conversation field.
- `backend/foreman/tools.py`: `_handle_create_task`, new-task branch of `_handle_assign_task`.

An older conversation's webhook run can therefore create a task in an unrelated, newer conversation. The resulting task's subsequent events and replies consistently follow the **wrong** conversation, so task/turn equality checks alone miss this defect.

Concrete production `create_task` evidence:

| Task | Creation result turn | Creating conversation | Stored task conversation |
| --- | --- | --- | --- |
| `t-pj95pi` — PR #3030 feedback | 210129 | 325 | 327 |
| `t-kt7696` — PR #2925 lint | 208101 | 297 | 305 |
| `t-6iz06v` — PR #2869 conflicts | 208011 | 303 | 305 |

**Smallest correction:** pass the resolved conversation ID through the tool-execution boundary and use it for new tasks, including direct `assign_task` creation. Preserve an existing task's ownership; define parent-task inheritance explicitly. Keep latest-user resolution only for genuinely unscoped entry points. Cover all executor fakes and direct tool callers.

Do not bulk-reassign existing tasks using “latest conversation.” Repair only from unambiguous creation evidence, together with dependent event/cache associations.

### 2. High: failed direct task-thread routing is reported as handled

**Locally reproduced; production frequency unknown.**

`backend/discord/router.py::_forward_to_foreman` offers task-bound replies to `_auto_route_task_reply` before triggering the Foreman. Working tasks route directly to `redirect_task`; awaiting-review/done/parked tasks route to `send_followup`.

`_auto_route_task_reply` logs `is_error` but returns `True` even when the tool failed, or returned no results. `_forward_to_foreman` then returns without a Foreman invocation. The inbound message may be persisted, but no AI explanation or recovery is initiated.

**Reproduction:** mock a working task and make real `exec_tools` boundary return `[{"is_error": true, "content": "Worker disconnected"}]`; `_auto_route_task_reply` still returns `True`.

**Smallest correction:** count the direct route as handled only on a valid successful result. On a definite tool failure, forward the message and error to the Foreman or emit an explicit failure response. Do not blindly retry ambiguous failures that may already have applied a side effect. Add success/error/empty-result tests at `_forward_to_foreman`.

### 3. High: an initial context-loading failure strands queued human messages

**Locally reproduced; production frequency unknown.**

`backend/foreman/runner.py::run_foreman_ai` drains the human queue only after `_run_foreman_ai` returns successfully. Its `finally` releases the lock but does not drain or fail queued messages.

Configuration/database/context loading in `_run_foreman_ai` occurs outside its later exception handler. If one of those operations raises while a Discord reply is queued, the original run exits, the lock disappears, and the queued reply remains with no active consumer. Another trigger may eventually drain it, but nothing guarantees that; newer messages can also overtake it.

**Reproduction:** block the initial `_run_foreman_ai`, enqueue a second human invocation, then raise a simulated context-loading error. The second message remains in `_human_queues` after the active task finishes.

The queue is also in memory, capped at 50, and explicitly drops the oldest entry on overflow. `_consume_forever` removes gateway messages before routing and does not requeue failures. Process restarts lose pending messages.

**Smallest correction:** guarantee drain or an explicit failure disposition after initial-run failure, preserving FIFO. For restart-safe delivery, use persisted inbound message IDs and explicit processing states; a bounded in-memory queue alone cannot provide that guarantee.

### 4. High: responses can be generated but never delivered to Discord

**Local behavior reproduced; missing-mirror suppression is an intentional safety policy, not permission to leak elsewhere.**

`backend/discord_notifier.py::notify_foreman_chat` checks `conversation_id` before `channel_id`. If the exact conversation has no mirrored Discord thread, it returns without posting, even when the original inbound Discord channel is known. With a mirror, the reply goes there rather than necessarily to the channel where the user asked.

`TurnJournal.text` supplies both the resolved conversation and original reply channel, and persists the answer separately. Thus a successful Foreman run can look like a dropped Discord reply.

`_bot_request` additionally logs API exceptions and returns `None`; delivery has no persisted acknowledgement/retry state here. Rate limits, missing permissions, and deleted/locked threads can all yield no message after successful reasoning.

**Smallest safe correction:** retain exact-conversation isolation. Record/log a structured undelivered outcome with conversation, inbound message, intended destination, and reason. Retry transient API failures at the same authorized destination. If replying to the original channel should override the mirror, explicitly validate that policy and destination rather than falling back to an arbitrary main channel.

### 5. High: events arriving before task linkage never join the conversation timeline

**Production-confirmed association gap.**

`backend/routes/webhooks.py::_find_task` matches only `(guild_id, Task.pr_repo, Task.pr_number)` at ingestion. It does not resolve a conversation from a PR association independently of a task. `_should_dispatch_to_foreman` skips unmatched events, apart from separate review-request/devReady paths.

At audit time, **4,968 distinct recent events** with no task/conversation already had an exact matching task by guild/repo/PR. They remain unstamped. For `Identity-Digital/dnsid#3074`:

- 258 events from October 22 20:39 through October 23 16:57 were unlinked.
- Task `t-s5pp2s` was created October 23 16:58:54 in conversation 305.
- 347 subsequent events were correctly linked to that task/conversation.

All 576 observed `pull_request/opened` deliveries in the seven-day snapshot were unlinked. This is compatible with tasks learning the PR number after GitHub sends `opened`; it does not establish that every delivery should have triggered work.

A live example is `Identity-Digital/dnsid#3140`: review events 601313/601314 were unlinked and no matching task existed. Conversation 334 received a comment on linked PR #3067 mentioning follow-up #3140, but mentioning a URL/number does not create a durable association. That relationship should not be inferred solely from arbitrary comment text.

**Correction:** resolve explicit PR/issue conversation associations at ingest, independently of whether a task exists yet. When a task acquires coordinates, idempotently reconcile already-stored events and cache associations. Reconciliation should not blindly replay every old CI event into the LLM.

### 6. Medium: issue/PR caches cannot represent a set of conversations

**Schema/code limitation; cache rows could not be inspected in production.**

`GithubIssue` and `GithubPullRequest` each have one nullable `conversation_id`, with one cache row per `(repo, number)`. `db/github_cache.py` uses `coalesce(existing, incoming)`, so the first non-null owner sticks.

Meanwhile `_find_task` chooses the **newest task** for a PR, which can belong to another conversation. Production recent tasks demonstrate PRs spanning multiple conversations: dnsid #2925 spans three; #2869, #2990, #3021, #3030, and pioneer-square #1368 each span two. Some may be affected by finding 1, so repair that bug before classifying every multi-conversation PR as intentional.

Other gaps:

- `_handle_create_github_issue` and `_handle_create_pr` return remote coordinates but do not save a conversation association.
- Periodic `fetch_issue_state` / `fetch_pr_status` cache upserts omit conversation IDs, so refreshing an unscoped cache row does not repair ownership.
- Plain `issues` events resolve an issue conversation for the cache, but the `GithubEvent`/chat row still uses the PR-task lookup's null conversation.

**Correction if “a set of conversations” is the intended contract:** keep cache identity `(repo, number)` and add explicit conversation associations, scoped through each conversation's guild. Do not overwrite the first owner's scalar ID with the latest owner's scalar ID. Distinguish **visibility in all linked conversations** from **one designated action owner**; broadcasting a webhook into every associated Foreman risks duplicate follow-ups.

### 7. Medium: automated webhook triggers can disappear under contention

**Explicit code policy; production loss rate not observable here.**

`run_foreman_ai` locks by `(guild_id, user_id)`, not conversation, and drops concurrent automated invocations. Separate conversations belonging to the same user therefore contend. Debounce `_deliver` has consumed its batch when this happens; there is no per-event processed flag or durable retry.

The poll loop is a safety net for current state, not proof that a particular human review comment or actionable request was processed. Review-request events without a task also re-resolve a generic user's latest conversation instead of carrying a durable PR-specific conversation binding.

**Correction:** after association is fixed, preserve actionable automated work as pending/dirty per conversation or work item and drain it after the active run. Coalesce stale CI snapshots rather than saving every snapshot as a new LLM run. Consider conversation-level serialization only alongside safeguards against two conversations mutating the same task.

## What is already correct

- Discord `_persist_inbound_message` resolves the authoritative conversation-thread binding, reactivates it, returns its ID, and `_forward_to_foreman` pins that ID on the trigger.
- Conversation-specific history uses `conversation_id` alone, not an OR fallback that combines all the user's conversations. It has explicit context-budget bounds.
- The journal consistently stamps the run's conversation ID on messages and Foreman turns.
- Once a webhook has a matching task, the observed event and task-run stamping is consistent. This does not prove the task's original attribution was correct.

The known first-response mismatch described in `docs/investigation-first-ai-response-not-in-conversation.md` is already fixed locally. The remaining task-executor boundary is a separate loss of the same identity.

## Delivery order and acceptance checks

1. **Preserve run identity through task tools.** Test an old conversation A running while B becomes the user's newest; both create paths must still produce tasks in A.
2. **Stop silent inbound failures.** Direct-route errors must produce a Foreman run or explicit failure. Context-load failure must leave no queued human message without an active or durable consumer.
3. **Make outbound disposition observable.** Test missing mirror, deleted thread, permissions errors, and 429/transient failures without cross-conversation fallback.
4. **Introduce explicit issue/PR association sets and a single action owner.** Decide whether unrelated review requests create new conversations or attach to an existing designated PR owner; never use whichever unrelated conversation happens to be newest.
5. **Reconcile linkage and replay only actionable pending work.** Test webhook-before-task, task-before-webhook, two tasks/two conversations on one PR, deleted tasks, and duplicate deliveries. Include ordinary issues as well as PRs.
6. **Perform an evidence-based data repair** only after routing changes are tested. Preserve provenance and do not automatically re-trigger thousands of historical CI events.

## Verification and limits

Existing isolated tests:

```text
cd backend && python -m pytest tests/test_foreman_run.py tests/test_turn_journal.py::test_text_passes_its_exact_conversation_to_discord -q
5 passed, 1 warning in 0.02s
```

A temporary local async harness, invoked as `python /tmp/pioneer-conversation-audit.py`, exercised real router/notifier/task-tool/runner functions with mocked external boundaries. It reported four reproduced behaviors:

```text
FAILED_DIRECT_ROUTE_SWALLOWED
MISSING_MIRROR_NO_DELIVERY
TASK_RE_RESOLVES_LATEST
QUEUED_REPLY_STRANDED
4/4 audit invariants violated
```

The task-tool probe simulated a run bound to A and latest-user lookup returning B; the production creation-result/task comparisons above independently establish the real mismatch. The missing-mirror probe demonstrates current suppression policy, not that arbitrary fallback is safe.

**Access limits:** `/debug/query` allows tasks, task logs/events, workers, agents, GitHub events, usage, and Foreman turns. Requests for `messages` and `conversations` returned 400. Issue/PR caches, Discord bindings, and API request logs are also outside the documented allowlist. No attempt was made to bypass it. Consequently this audit cannot count inbound messages that never started runs, inspect actual mirror availability, establish outbound Discord HTTP outcomes, inspect issue/PR cache ownership, or verify the precise provider request context.

One unbounded text search hit the server's 30-second timeout; subsequent turn scans were bounded by recent IDs. Some stored webhook payloads are deliberately truncated and not valid JSON, so payload JSON-cast queries failed and were not used as evidence.

**Not done at audit time:** full database-backed regression suite, live Discord end-to-end tests, log access, schema migrations, behavior fixes, deployment, or historical data repair. The observations above describe the pre-fix state.

## Implementation follow-up (local, not deployed)

- Extended the debug allowlist to conversations, messages, GitHub issue/PR caches, and Discord channel/thread bindings. Authentication, read-only transactions, timeouts, and credential-table exclusions remain intact.
- Propagated the pinned conversation through the real tool executor and `ToolContext`, validating guild ownership. Both new-task paths use it instead of reselecting the user's latest conversation.
- Failed/empty direct Discord task dispatch falls back to the Foreman, with an instruction to inspect potentially partially applied mutations before retrying.
- Initial context-loading errors drain queued work before releasing the run lock. GitHub batches now queue under contention too; automated overflow cannot evict queued human input.
- PR/issue creation tools cache their conversation identity immediately, before a task exists. Cache failures do not misreport successful remote creation as failed and encourage duplicate writes.
- Webhooks pin cached or task-owned conversations through debounce. Eligible unowned review/devReady work starts a fresh conversation instead of inheriting an unrelated latest chat. Plain issue events use issue-task linkage, and chat broadcasts include the resolved conversation ID.
- Task creation/assignment and worker PR-coordinate updates reconcile previously unlinked PR events and cache rows without overwriting existing owners or replaying historical actions.
- Conversation context includes issue/PR state and shared PR activity through existing task links, allowing multiple conversations to see the same PR while retaining a single action dispatch. This is a derived association set, not a new general-purpose many-to-many tagging table; arbitrary URL mentions alone do not create durable associations.
- Added a concurrent PostgreSQL index migration, `20261026_000000`, for guild/repo/PR event lookups. Context lookups use indexed UNION branches instead of a per-delivery correlated scan.
- Discord 429 responses retry the same request up to three total attempts, honoring numeric `Retry-After` values up to 60 seconds. Other errors are not blindly retried. A missing conversation mirror now emits an explicit undelivered warning and still never falls back to another conversation or arbitrary channel.

Validation:

```text
ruff check . --fix && ruff format .
All checks passed after fixes/formatting.

cd backend && python -m pytest -q
1544 passed, 3 skipped, 13 warnings in 85.20s

Local test database: index migration downgrade to previous head and upgrade to head passed.
```

Regression coverage is in `backend/tests/test_conversation_routing_regressions.py`, alongside updated executor/debounce fixtures. The initial eleven new regression cases were run red before their fixes. Subsequent coverage checks shared-PR visibility, cross-guild isolation, remote-creation tagging, cached webhook routing, fresh review contexts, contention, overflow priority, and rate-limit retries.

Remaining limits: no deployment or historical task-ownership repair; no live Discord delivery test or production re-audit under the expanded debug access. Missing mirrors/permanent permission failures can still prevent delivery, now with explicit diagnostics. Gateway/run/debounce queues remain bounded and in memory, so process restarts or saturation can still lose pending work; a durable inbox/outbox is separate work. Existing warnings and three skipped backend tests remain. No independent implementation review or formal specification acceptance was performed.
