# Child Foreman Context Migration — Architecture Plan

**Issue:** [#1261](https://github.com/jmelloy/pioneer-square/issues/1261)
**Related:** [#1005](https://github.com/jmelloy/pioneer-square/issues/1005) (thread audit),
[#1200](https://github.com/jmelloy/pioneer-square/issues/1200) (context collapse),
[#1167](https://github.com/jmelloy/pioneer-square/issues/1167)/[#1168](https://github.com/jmelloy/pioneer-square/issues/1168) (Foreman-owned threads)
**Date:** 2026-09-02

---

## TL;DR

**The migration this issue asks to plan has already been implemented**, by #1200
(commit `265b138`, merged 2026-08-21). `_CHILD_EXCLUDED_TOOLS`, `CHILD_FOREMAN_TOOLS`,
`CHILD_FOREMAN_SYSTEM`, `build_child_system_blocks`, `build_child_state_preamble`, the
`FOREMAN_CHILD_CONTEXTS` gate and the `child=` run parameter are all gone from the tree.
There is no child Foreman context left to migrate.

Three of #1261's four open questions are therefore moot. What is *actually* outstanding
is two things the migration left behind:

1. **A live regression** — `backend/routes/webhooks.py` still passes the removed `child=`
   kwarg, so every debounced GitHub webhook batch raises `TypeError` and silently never
   reaches the Foreman. Confirmed by execution, not inspection.
2. **The Discord half of the migration** — the backend context collapsed to one
   conversation, but Discord still runs two parallel thread systems, and task-scoped
   Foreman chat can't reach the Foreman-owned thread.

This document records what the current behaviour actually is, answers #1261's four
questions against that reality, and proposes a four-phase plan for the remainder.

---

## 1. Current behaviour

### 1.1 There is no child context

`foreman.runner.run_foreman_ai()` today:

```python
async def run_foreman_ai(
    guild_id, human_message, extra_context="", user_id=None, task_id=None,
    *, is_human=False, reply_channel_id=None, trigger=None,
) -> None:
```

Every run for a `(guild_id, user_id)` pair shares:

- **one lock** — `_guild_locks[(guild_id, user_id)]`, never keyed on `task_id`
- **one history** — `_load_history(guild_id, user_id)`, no `task_id` slice
- **one prompt** — `build_system_blocks` + `build_state_preamble`, whole-guild view
- **one tool set** — `FOREMAN_TOOLS`, all 23 tools, unfiltered

`task_id` survives as *metadata* on a turn (`ForemanTurn.task_id`, `Message.task_id`,
`ChatMsg.taskId`) driving message badges and Discord routing — not as a context boundary.
See `docs/foreman-per-task-context.md` for the model as implemented.

Notably, collapsing the lock to `(guild, user)` closed the #927 parent-vs-child
task-mutation race *by construction*: there is only one context left to race with. The
`_task_mutation_blocked` no-op #1200 kept for call-site stability has since been removed
too.

### 1.2 The tool matrix

The excluded set that #1261 asks about was:

```python
# backend/foreman/tools_schema.py @ 265b138^
_CHILD_EXCLUDED_TOOLS = frozenset({"create_task", "assign_task", "spawn_worker"})
CHILD_FOREMAN_TOOLS = [t for t in FOREMAN_TOOLS if t["name"] not in _CHILD_EXCLUDED_TOOLS]
```

Per-tool analysis — why each was excluded, and whether that reason still holds:

| Tool | Why it was excluded | Still valid? | Today |
|---|---|---|---|
| `create_task` | A child owned exactly one already-assigned task; fan-out was the parent's job. Also the #927 mutation race. | **No.** Scope-shaping, not a security boundary. The race is closed by the shared lock. | Allowed |
| `assign_task` | Same: assignment was parent-only so two contexts couldn't assign the same worker. | **No.** Same reasoning; worker assignment is already guarded by the guild lock and the worker's own state machine. | Allowed |
| `spawn_worker` | Standing up new infrastructure was deliberately parent-only. | **Partially.** Not a *context* boundary, but it is the one tool in this set with a real external cost (it starts a container and bills tokens). | Allowed |

**None of the three was a security or regulatory boundary.** All three were scope
discipline for a persona that no longer exists. The `spawn_worker` cost concern is real
but belongs in spawn settings/quota (where it now lives after #1240), not in a
context-scoped tool filter — a filter keyed on "which persona is running" is the wrong
place to enforce a budget, because the budget applies to the guild either way.

Tools that genuinely *should* be gated — `dnsid`, `message_discord_bot`,
`create_github_issue`, `create_pr` — were **never** in the excluded set. They are gated
where they belong: at credential resolution and at the guild's GitHub App installation
scope. Re-introducing a per-context tool filter would add a second, weaker place to get
that wrong.

### 1.3 Thread routing: two systems, one job

Two independent Discord-thread mechanisms currently coexist.

**System A — legacy bindings** (`backend/discord_notifier.py`, table
`discord_thread_bindings`, keyed `(subject_type, subject_key)`):

| `subject_type` | key | created by | purpose |
|---|---|---|---|
| `issue` | `repo#number` | `_ensure_thread` | one thread per canonical GitHub work item |
| `task_stream` | `task_id` | `_ensure_task_thread` | verbose per-task terminal feed (opt-in, `DISCORD_STREAM_TASKS`) |
| `conversation` | `guild_id:user_id` | `_ensure_conversation_thread` | ad-hoc Foreman chat (#1161) |

All three go through the single `_get_or_create_thread(subject_type, subject_key, channel,
thread_name)` lookup-or-create, and `issue`-keyed subjects are first normalised by
`_canonical_coords(repo, number, kind=, task_id=)`, which resolves one work item to one
`(repo, number)` — preferring `Task.issue_repo/issue_number`, then a PR's linked task,
then the cached PR's branch suffix or `Closes #N` body, then the subject as given.
`_canonical_coords` exists to stop *fragmentation* (#866): the same work item keyed once
under its PR number and once under its issue number.

**System B — Foreman-owned threads** (`backend/foreman/thread_service.py`, models
`Conversation`/`Thread`, mirrored to Discord by `backend/discord/thread_mirror.py`):

A `models.Thread` is created only as a side effect of the Foreman handling a message
(`ensure_conversation_thread` → `get_or_create_active_thread`), broadcast over WS as
`thread-created`, and mirrored into a real Discord thread whose id is stamped back onto
`Thread.discord_thread_id`. `Task.thread_id` is stamped at task-creation time, and
`resolve_thread_id(db, guild_pk, task_id=, user_id=)` prefers the task's thread over the
user's active thread. It never creates.

System B is the newer, correct model — it is the one the frontend, the WS protocol and
the sidebar all subscribe to. System A predates it and has not been retired.

### 1.4 Task lifecycle and sidebar

Child contexts never created sidebar entries, because they were not tasks. That is still
true and is still correct: a Foreman *run* is a turn in a conversation, not a unit of
work. The sidebar is driven by `Task` rows and by `thread-created`/`thread-updated`
broadcasts. A task-triggered Foreman run today is already visible twice — as a badged
message line in the owner's thread, and as the task row whose state it just changed.

---

## 2. Findings

### F1 — `routes/webhooks.py` passes the removed `child=` kwarg (live regression)

`backend/routes/webhooks.py:_deliver()` still calls:

```python
await run_foreman_ai(
    guild_id, combined, user_id=user_id, task_id=task_id,
    child=bool(task_id),          # <-- parameter removed by #1200
    trigger="github-event",
)
```

`run_foreman_ai` has no `child` parameter and no `**kwargs`. Verified by execution:

```
CONFIRMED TypeError: run_foreman_ai() got an unexpected keyword argument 'child'
```

The call runs inside a debounce timer whose done-callback (`_log_fire_error`) *logs* the
exception rather than propagating it, so the failure is silent: **every debounced GitHub
webhook batch — CI results, review submissions, PR merges, issue comments — has failed to
reach the Foreman since 2026-08-21.** `ensure_poll_loop()` on the line below never runs
either, so the safety-net poll is not re-armed by webhook activity.

The existing tests do not catch it because they patch `run_foreman_ai` with a fake whose
signature *includes* `child=False` (`test_github_webhooks_phase2.py:558`,
`test_foreman_guild_lock.py`), reproducing the removed API rather than the real one.

Fix is one line (delete the kwarg) plus dropping `child` from the test doubles so the
fakes match the real signature. Filed separately — this is not a planning deliverable.

### F2 — Task-scoped Foreman chat cannot reach the Foreman-owned thread

`discord_notifier.notify_foreman_chat` routes with an `if/elif`:

```python
if task_id:
    coords = await _canonical_coords(None, None, task_id=task_id)
    if coords:
        task_thread_id = await _lookup_thread(_SUBJECT_ISSUE, _issue_subject_key(*coords))
        if task_thread_id:
            ...; return
elif user_id:
    foreman_discord_thread = await _lookup_foreman_thread_for_user(guild_id, user_id)
    ...
await _post_foreman_chat_line(channel, content)   # flat channel
```

`foreman/journal.py` always passes **both** `task_id` and `user_id`. So a task-scoped line
whose canonical issue has no `issue` binding — a task with no linked GitHub issue, or one
created before Discord was configured — skips the `elif` entirely and lands in the **flat
main channel**, even though that user has a perfectly good Foreman thread with a Discord
mirror. This is exactly the fragmentation `_canonical_coords` was written to prevent,
reappearing one level up.

`Task.thread_id` → `Thread.discord_thread_id` is never consulted on the `task_id` path,
despite being the binding the Foreman itself stamped.

### F3 — Two thread systems with overlapping responsibility

System A's `conversation` subject and System B's `Thread` model do the same job. #1168
already made `notify_foreman_chat` *prefer* System B on the `user_id` path and treat A as
a fallback, but nothing retires A: `_ensure_conversation_thread`,
`archive_conversation_thread` and the `conversation` rows in `discord_thread_bindings`
are still live code with an inbound routing path in `discord/router.py`.

`issue` and `task_stream` are not redundant with B — they key on a GitHub work item and on
a task's terminal feed respectively, neither of which is a conversation — but they are
*unreachable* from B: given a `Thread`, there is no way to find the issue thread for the
work it concerns, and vice versa.

### F4 — Stale references to the removed model

| Location | Issue |
|---|---|
| `backend/routes/webhooks.py:206` | comment: "run in that task's isolated child context" — no longer true |
| `scripts/eval_foreman_metrics.py:315` | prints "Child Foreman Effectiveness Report" |
| `backend/tests/test_guild_api.py:68,104` | docstrings say "child-context id"/"child-context tool_use row" |
| `frontend/src/composables/useChatGrouping.ts:11` | comment: "a child-context turn's …" |
| `backend/tests/test_foreman_child_context.py` | filename describes a model that no longer exists |

Cosmetic, but they are why #1261 and #1005 were both written against a tree state that no
longer holds.

---

## 3. Decisions

### Q1 — Which tools should remain excluded from child contexts?

**None, and no per-context tool filter should be re-introduced.** The historical exclusion
set (`create_task`, `assign_task`, `spawn_worker`) encoded persona scope, not a trust
boundary; the persona is gone and the race it partly guarded is closed by the
`(guild, user)` lock. The tools that genuinely need gating are gated at credential
resolution and GitHub App installation scope, which applies uniformly and does not depend
on which run is asking.

If `spawn_worker` cost ever needs a ceiling, it belongs in spawn settings/quota (one
resolver, guild-wide), not in a tool filter keyed on run provenance.

### Q2 — Should child contexts create ephemeral tasks or full tasks in the sidebar?

**Neither.** A Foreman run is a conversation turn, not a unit of work. It is already
visible as a badged message in the owner's thread and via the `Task` rows it mutates.
Minting a sidebar entry per automated run would make the board unreadable during CI churn
— which is precisely the traffic that triggers most automated runs.

### Q3 — How do we handle thread routing when the parent is already in a task stream?

**`Thread` (System B) is the single conversational routing authority; `task_stream` stays
a one-way verbose feed.** `resolve_thread_id` already implements the precedence —
`Task.thread_id` first, user's active thread second, never create. The gap is that Discord
narration does not follow the same precedence (F2). Fix the Discord side to match the
backend rather than adding a third rule.

`task_stream` should not absorb Foreman narration: it is a firehose posted with
`SUPPRESS_NOTIFICATIONS` specifically so it never pings anyone, and Foreman narration is
the opposite — low-volume, addressed to a human, should ping.

### Q4 — What happens to run output — Discord thread, task log, both?

**Both, unchanged.** Output goes to the WS `ChatMsg`/`Message` record (badged with
`taskId`, threaded by `threadId`) and is mirrored once to exactly one Discord destination.
The only change needed is *which* destination the mirror picks (F2).

---

## 4. Phased plan

### Phase 0 — Restore GitHub webhook → Foreman delivery *(bug fix, urgent)*

- Delete `child=bool(task_id)` from `routes/webhooks.py:_deliver`.
- Fix the stale comment on the line above.
- Remove `child=` from the `run_foreman_ai` test doubles in
  `test_github_webhooks_phase2.py` and `test_foreman_guild_lock.py` so the fakes match the
  real signature.
- Add one regression test that asserts `_deliver`'s call is signature-compatible with the
  real `run_foreman_ai` (bind the recorded kwargs against
  `inspect.signature(run_foreman_ai)`), so the next parameter removal fails loudly.

Risk: none. Reverts an unintended break. Ships independently of everything below.

### Phase 1 — Unify Foreman chat routing on the Thread model

Change `notify_foreman_chat`'s `if task_id: … elif user_id:` into a single ordered
fallback chain, tried in order and stopping at the first hit:

1. explicit `channel_id` (the @-mention reply path — unchanged, still wins outright)
2. the task's canonical `issue` thread, when one exists
3. the task's own `Thread.discord_thread_id` via `Task.thread_id` *(new)*
4. the user's active `Thread.discord_thread_id`
5. the legacy `conversation` binding
6. the flat channel

Steps 3 and 4 both resolve through `_lookup_foreman_thread_for_user`-style joins; step 3
is the missing link that makes `Task.thread_id` — already stamped, already authoritative
on the backend — actually mean something in Discord.

Risk: low, additive. Only changes messages that today land in the flat channel. Worth a
metric on how often each rung fires before Phase 2 is scoped.

### Phase 2 — Retire the legacy `conversation` subject

Once Phase 1 is deployed and step-5 hits trend to zero:

- Stop calling `_ensure_conversation_thread`; make `conversation` lookup-only.
- Backfill: for each `conversation` binding with no matching `Thread.discord_thread_id`,
  stamp the existing Discord thread onto the user's active `Thread` so history is not
  orphaned.
- Fold `archive_conversation_thread` into `thread_mirror.on_thread_updated`, which already
  archives on status change — one archive path, not two.
- Drop `_SUBJECT_CONVERSATION` and its `discord/router.py` inbound branch; inbound replies
  route through `Thread.discord_thread_id`.

Leave `issue` and `task_stream` in place. They key on things that are not conversations
and have no equivalent in System B.

Risk: medium — touches inbound routing. Gate on the Phase 1 metric; the backfill is the
part to get right.

### Phase 3 — Documentation and stale-reference cleanup

- Update `docs/foreman-per-task-context.md` to note #1261 as the closing record.
- Fix the five stale references in F4; rename `test_foreman_child_context.py` to
  `test_foreman_conversation_context.py`.
- Close #1005 by reference — its two audit questions (thread keying, child tool matrix)
  are answered in §1.2 and §1.3 above.

Risk: none.

### Explicitly not planned

- **Re-introducing a per-context tool filter.** §3/Q1.
- **Merging `task_stream` into `issue` threads** (#1005's open question). They serve
  different audiences at different volumes, and `task_stream` is silent-by-design; merging
  would either spam the issue thread or silence it. Keep them separate.
- **Sidebar entries for automated runs.** §3/Q2.
