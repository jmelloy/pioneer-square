# Foreman Context Analysis

_Code paths: `backend/foreman/prompt.py`, `backend/foreman/runner.py`, `backend/foreman/tools.py`_

---

## 1. How the context is assembled

Every call to `run_foreman_ai()` reconstructs context from scratch in three layers:

### 1a. System prompt (rebuilt on every call)

`build_system_prompt()` in `prompt.py` concatenates:

| Section | Content | Approx. size |
|---|---|---|
| Base instructions (`FOREMAN_SYSTEM`) | Role, responsibilities, multi-step workflow, GitHub guidance | ~1,450 chars (~360 tokens) |
| Primary repo line | `"The primary repository for this guild is \`{repo}\`."` (optional) | ~80 chars |
| Workers block | JSON array of all registered workers: `id`, `state`, `repos[]`, `agent_count` | ~80–150 chars per worker |
| Tasks block | JSON array of up to **6** tasks (most recent, no state filter): `id`, `worker_id`, `description`, `state`, `branch`, `pr_url` | **variable — see §2** |
| Extra context | Injected by escalation/followup callers (optional) | varies |

Total system prompt: typically **600–1,000 tokens** but can balloon past **3,000 tokens** when task descriptions are long.

### 1b. Conversation history (DB-backed, windowed)

`_load_history()` in `runner.py` reads all `foreman_turns` rows for the guild+user and applies a sliding window:

- Constant `_HUMAN_TURN_WINDOW = 5` — walk backwards until 5 human-initiated (non-tool-response) turns are found; discard everything before that cutoff.
- All assistant turns and tool-result turns between those 5 human turns are included intact (to avoid orphaned `tool_use` blocks).
- In practice this yields **15–30 total turns** for an active session, roughly **1,500–6,000 chars**.

### 1c. Tool results (400-char hard cap)

After each `exec_tools()` call, results are truncated before both storage and the next API call:

```python
_RESULT_MAX = 400   # runner.py:23
trimmed = [
    {**r, "content": r["content"][:_RESULT_MAX] + " …[truncated]"}
    if len(r.get("content", "")) > _RESULT_MAX else r
    for r in tool_results
]
```

This applies uniformly to all tools. Maximum 400 chars per result regardless of tool type.

---

## 2. Recent tasks — the dominant cost

**Query:** `SELECT … FROM tasks WHERE guild_id = ? ORDER BY created_at DESC LIMIT 10` then sliced to `[:6]`.

**No state filter.** Done, failed, and cancelled tasks are included at full fidelity alongside active ones.

**Full `description` field** is included. Task descriptions are free-form and can be multi-paragraph (they are literally the prompt sent to the worker's Claude subprocess). A single verbose task description can be 500–3,000 chars. Multiplied across 6 tasks:

| Scenario | Description size | 6-task total |
|---|---|---|
| Typical short tasks | ~300 chars | ~2,000 chars (~500 tokens) |
| Detailed tasks | ~1,000 chars | ~6,500 chars (~1,600 tokens) |
| Complex multi-step prompts | ~3,000 chars | ~20,000 chars (~5,000 tokens) |

In a moderately active guild that has completed several multi-step tasks, **the tasks block alone can exceed the base instructions, workers block, and entire conversation history combined.**

### What the tasks block actually looks like today

A realistic three-task excerpt (abridged here — real descriptions are often 3–5× longer):

```json
[
  {
    "id": "t-abc123",
    "worker_id": "w-xyz789",
    "description": "Implement OAuth 2.0 login flow for the GitHub integration. The backend needs a new /auth/github/login endpoint that redirects to GitHub's OAuth page with the correct scopes (read:user, repo). The callback endpoint /auth/github/callback should exchange the code for an access token, store it in the github_tokens table keyed by github_user_id, issue a short-lived login_token, and redirect the frontend to /?login_token=...&github_username=.... The frontend should pick up these params, store them in localStorage, and set the Authorization header on all subsequent REST calls. Make sure to handle the case where the user has already linked GitHub (upsert, not insert).",
    "state": "done",
    "branch": "claude/oauth-login-abc123",
    "pr_url": "https://github.com/acme/backend/pull/42"
  },
  {
    "id": "t-def456",
    "worker_id": "w-xyz789",
    "description": "Add a WebSocket heartbeat. Workers should ping every 30 s; the backend should mark workers offline if no ping in 90 s. This prevents zombie workers holding task slots. See the existing _listen() loop in worker.py for where to add the ping task.",
    "state": "working",
    "branch": "claude/heartbeat-def456",
    "pr_url": null
  },
  {
    "id": "t-ghi789",
    "worker_id": "foreman",
    "description": "Review the heartbeat PR and check reconnection edge cases.",
    "state": "pending",
    "branch": null,
    "pr_url": null
  }
]
```

This three-task block is ~1,200 chars. Six tasks with verbose descriptions easily reaches 6,000–12,000 chars.

---

## 3. Tool result sizing — the 400-char cap is both helpful and limiting

The 400-char cap keeps stored turns small, but it's a blunt instrument:

| Tool | Raw output size | After cap |
|---|---|---|
| `create_task` / `assign_task` / `finalize_task` | 40–80 chars | unchanged |
| `send_followup` / `message_worker` | 40–60 chars | unchanged |
| `list_github_issues` (20 issues) | ~3,000 chars | **truncated to 400** |
| `get_github_issue` (body capped at 2,000 + 20 comments × 500) | up to 12,000 chars before own limits; ~3,000 typical | **truncated to 400** |
| `search_github_issues` (10 results) | ~800 chars | may be truncated |
| `get_task_status` (10 log lines) | ~800–1,500 chars | **truncated to 400** |
| `list_github_prs` (20 PRs) | ~1,200 chars | **truncated to 400** |

For diagnostic tools like `get_task_status`, the 400-char cap can strip most of the log output the foreman needs to diagnose stalls. The JSON envelope (`id`, `name`, `state`, `phase`, timestamps) alone takes ~200 chars, leaving room for only 1–2 short log lines.

### What a clipped get_task_status result looks like today

```
{"id": "t-def456", "name": "WebSocket heartbeat", "state": "working", "phase": "execute",
"worker_id": "w-xyz789", "agent": {"agent_id": "a-mn0", "agent_state": "thinking"},
"branch": "claude/heartbeat-def456", "pr_url": null, "created_at": "2026-04-30T18:00:00Z",
"finished_at": null, "recent_logs": [{"time": "2026-04-30T18:05:12Z", "line": "Reading work …[truncated]
```

The foreman sees the task state but none of the actual log lines — exactly what it needs to judge whether the task is progressing.

---

## 4. Conversation history — tool call chains for completed tasks

### What a full task lifecycle looks like in history

A single task that required one follow-up generates roughly this turn sequence:

```
[1] user:       "implement the heartbeat mechanism"
[2] assistant:  <tool_use: create_task>  <tool_use: assign_task>
[3] user:       <tool_result: "Task t-def456 created">  <tool_result: "Task t-def456 assigned to w-xyz789">
[4] assistant:  "Created and assigned heartbeat task t-def456."
--- automated callback: task-complete ---
[5] user:       "task-complete: t-def456 — worker finished"
[6] assistant:  <tool_use: get_task_status>
[7] user:       <tool_result: "{id: t-def456, state: awaiting-review, recent_logs: […]}">
[8] assistant:  <tool_use: send_followup>  (add unit tests)
[9] user:       <tool_result: "Follow-up sent to w-xyz789 for task t-def456">
--- automated callback: task-followup-done ---
[10] user:      "task-followup-done: t-def456"
[11] assistant: <tool_use: finalize_task>
[12] user:      <tool_result: "Task t-def456 finalized">
[13] assistant: "Heartbeat task is complete. PR is at …"
```

That's 13 turns for one task cycle. With the 5-human-turn window those turns persist in context until five later human interactions push them out. Turns 6–12 (the `get_task_status`, `send_followup`, and `finalize_task` calls and their results) are the most expensive and, once the task is done, convey very little: the final state is already reflected in the task block as `"state": "done"`.

---

## 5. Reduction opportunities — ranked by impact

### 5.1 Prioritise open/pending tasks; truncate completed ones to a meaningful summary [**Highest impact**]

**Current behaviour:** 6 most-recent tasks ordered only by `created_at`, all states at equal verbosity. Completed tasks with 1,000-char descriptions sit beside a two-word pending task.

**Proposed:** Always surface active tasks first (any state that is not `done`/`failed`/`cancelled`). Fill remaining slots with recently-completed tasks, but cap their descriptions at ~200 chars — enough to know what was accomplished without reproducing the full worker brief.

```python
# runner.py — replace the task_rows slice before json.dumps
active = [t for t in task_rows if t["state"] not in ("done", "failed", "cancelled")]
terminal = [t for t in task_rows if t["state"] in ("done", "failed", "cancelled")]

# Active tasks: truncate description at 500 chars (they're being worked on, context matters)
active_trimmed = [
    {**t, "description": t["description"][:500]}
    for t in active
]
# Completed tasks: compact summary — enough to know what was done
terminal_trimmed = [
    {
        "id": t["id"],
        "state": t["state"],
        "description": t["description"][:200],
        "branch": t["branch"],
        "pr_url": t["pr_url"],
    }
    for t in terminal
]

tasks_block = json.dumps((active_trimmed + terminal_trimmed)[:6], indent=2)
```

**What the same tasks block looks like after:**

```json
[
  {
    "id": "t-def456",
    "worker_id": "w-xyz789",
    "description": "Add a WebSocket heartbeat. Workers should ping every 30 s; the backend should mark workers offline if no ping in 90 s. This prevents zombie workers holding task slots. See the existing _listen() loop in …",
    "state": "working",
    "branch": "claude/heartbeat-def456",
    "pr_url": null
  },
  {
    "id": "t-ghi789",
    "worker_id": "foreman",
    "description": "Review the heartbeat PR and check reconnection edge cases.",
    "state": "pending",
    "branch": null,
    "pr_url": null
  },
  {
    "id": "t-abc123",
    "state": "done",
    "description": "Implement OAuth 2.0 login flow for the GitHub integration. The backend needs a new /auth/github/login endpoint that redirects to GitHub's OAuth page with the correct scopes (read:user, repo)…",
    "branch": "claude/oauth-login-abc123",
    "pr_url": "https://github.com/acme/backend/pull/42"
  }
]
```

Active tasks come first and retain enough context. The completed task is still recognisable (200 chars covers the goal and approach) but no longer reproduces the full worker brief.

**Estimated saving:** 500–5,000 tokens per call depending on how many completed tasks are in the window. The tasks block goes from unbounded to predictably small. **Biggest single lever.**

**Trade-off:** Active tasks are still capped at 500 chars; the foreman can use `get_task_status` to recover the full description for a specific task. Raise the `get_task_status` cap (§5.3) in tandem.

---

### 5.2 Strip intermediate tool results from completed-task history exchanges [**High impact**]

**Current behaviour:** Every `get_task_status`, `send_followup`, and `finalize_task` call — plus their results — is stored in `foreman_turns` and replayed in the context window until pushed out by the 5-human-turn limit.

**Proposed:** When loading history, detect tool-result turns whose `tool_use_id` refers to a tool call made during a task that is now terminal (done/failed/cancelled), and drop them — keeping only the last tool result (typically the `finalize_task` acknowledgement). The final assistant text turn ("Task done, PR at …") is already enough for the foreman to know what happened.

Concretely, after loading history the 13-turn lifecycle from §4 would be collapsed to:

```
[1] user:       "implement the heartbeat mechanism"
[4] assistant:  "Created and assigned heartbeat task t-def456."
[5] user:       "task-complete: t-def456 — worker finished"
[11] assistant: <tool_use: finalize_task>
[12] user:      <tool_result: "Task t-def456 finalized">   ← last tool result kept
[13] assistant: "Heartbeat task is complete. PR is at …"
```

Turns 2, 3, 6, 7, 8, 9, 10 are dropped — 7 turns saved per completed task cycle.

**Implementation sketch:** In `_load_history()`, after fetching turns, identify the `parent_id` chain for tool-result turns, cross-reference against current task states, and filter out intermediate tool exchanges for terminal tasks. This requires one additional query to fetch current task states for any task IDs mentioned in the turn content.

**Estimated saving:** 7–15 turns per completed task cycle still in the window. For a guild that completed 2–3 tasks within the last 5 human turns: **50–70% reduction in history turn count**.

**Trade-off:** Slightly more complex load logic; some debugging signal is lost (can't reconstruct the full foreman decision path from the stored turns alone). Mitigated by the fact that the raw turns are still in the DB — only the in-context window is pruned.

---

### 5.3 Per-tool result size caps [**Medium impact**]

**Current behaviour:** Flat 400-char cap for all tool results.

**Proposed:** Differentiate caps by tool class:

| Category | Tools | Suggested cap |
|---|---|---|
| Acknowledgement tools | `create_task`, `assign_task`, `finalize_task`, `send_followup`, `message_worker`, `cancel_task`, `redirect_task`, `claim_github_issue` | 200 chars |
| Diagnostic / status | `get_task_status` | 2,000 chars (needs log lines to be useful) |
| GitHub read (list) | `list_github_issues`, `list_github_prs`, `search_github_issues` | 1,500 chars |
| GitHub read (detail) | `get_github_issue` | 2,500 chars |
| GitHub write | `create_github_issue` | 300 chars |

```python
# runner.py
_RESULT_MAX_BY_TOOL = {
    "get_task_status": 2000,
    "get_github_issue": 2500,
    "list_github_issues": 1500,
    "list_github_prs": 1500,
    "search_github_issues": 1500,
}
_RESULT_MAX_DEFAULT = 200

trimmed = [
    {**r, "content": r["content"][:cap] + " …[truncated]"}
    if len(r.get("content", "")) > (cap := _RESULT_MAX_BY_TOOL.get(r.get("tool_name", ""), _RESULT_MAX_DEFAULT))
    else r
    for r in tool_results
]
```

(Note: `tool_use_id` is on the result block but tool name is not — the runner needs to pass the tool name alongside each result, or build a `id→name` map from `tool_uses` before truncating.)

**Estimated impact:** Net neutral on average token count (some results get larger, others smaller), but dramatically improves foreman decision quality for diagnostics and GitHub reads — meaning fewer redundant follow-up tool calls, which is a net token saving over the full session.

---

### 5.4 Prune old DB turns (lazy cleanup) [**Low-to-medium impact on DB**]

**Current behaviour:** `foreman_turns` rows accumulate indefinitely.

**Proposed:** On each `_load_history()` call, after determining the cutoff index, delete rows before it:

```python
if cutoff > 0:
    old_ids = [t.id for t in turns[:cutoff]]
    await db.execute(delete(ForemanTurn).where(ForemanTurn.id.in_(old_ids)))
    await db.commit()
```

**Estimated saving:** No token saving (window is already applied in Python). Keeps the DB small and speeds up the load query on long-running guilds.

**Trade-off:** Deletes rows that might be useful for debugging. Consider keeping rows but adding a `pruned` flag, or logging them to a cold-storage table, before deletion.

---

### 5.5 Slim tasks block for automated callbacks [**Low impact**]

**Current behaviour:** All `run_foreman_ai()` callers get the same 6-task block regardless of invocation type.

**Proposed:** For automated callbacks (`task-complete`, `task-followup-done`, `needs-input`) where `extra_context` already identifies the triggering task, pass that task at full detail and the rest as compact summaries.

**Estimated saving:** 100–500 tokens per automated callback. Low implementation risk; adds branching at the call site.

---

## 6. Summary table

| Opportunity | Token saving estimate | Complexity | Risk |
|---|---|---|---|
| 5.1 Prioritise active tasks; truncate completed to 200 chars | 500–5,000 tokens/call | Low — filter + slice | Low |
| 5.2 Strip intermediate tool results for completed tasks | 50–70% fewer history turns | Medium — cross-ref task states | Low |
| 5.3 Per-tool result caps | 0 net (quality improvement) | Medium — dict lookup + name plumbing | Low |
| 5.4 DB turn pruning | 0 tokens (DB only) | Low | Low |
| 5.5 Slim tasks for callbacks | 100–500 tokens/call | Medium | Medium |

**Recommended first moves:** Implement §5.1 (one-liner filter + truncate) immediately — it is the single highest-impact, lowest-risk change and requires no schema or protocol changes. Follow with §5.2 (history pruning for completed tasks) and §5.3 (raised cap for `get_task_status`) together, since they are complementary: §5.2 removes stale tool exchanges from history while §5.3 ensures the foreman can get full diagnostic output when it needs it.
