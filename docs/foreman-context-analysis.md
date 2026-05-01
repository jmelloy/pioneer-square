# Foreman Context Analysis

_Analysed against commit on branch `claude/in-repo-jmelloy-pioneer-square-investigate-t-02fd`  
Code paths: `backend/foreman/prompt.py`, `backend/foreman/runner.py`, `backend/foreman/tools.py`_

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

---

## 4. Conversation history growth

Turns are accumulated indefinitely in the DB. The window query loads _all_ turns to scan backwards; old rows past the window are never deleted. For a long-running guild:

- DB rows grow unbounded (no TTL, no periodic cleanup).
- The load query still fetches all rows for that guild+user, then discards most of them — unnecessary DB I/O as history grows.
- The 5-turn window is applied uniformly to all invocation types (human messages, automated `task-complete` callbacks, `needs-input` escalations). Automated callbacks don't need full human-turn history.

---

## 5. Reduction opportunities — ranked by impact

### 5.1 Truncate task descriptions in the system prompt [**Highest impact**]

**Current behaviour:** Full `description` field (unlimited length) for each of the 6 recent tasks.

**Proposed:** Truncate descriptions to ~150 chars in the tasks block. Store the full description in the DB (already there — `tasks.description`); the foreman can fetch full detail via `get_task_status` when it needs to act on a specific task.

```python
# runner.py, when building task_rows
task_rows = [
    {
        **dict(r._mapping),
        "description": (dict(r._mapping).get("description") or "")[:150],
    }
    for r in task_result.fetchall()
]
```

**Estimated saving:** 500–5,000 tokens per call depending on task verbosity. **Biggest single lever.**

**Trade-off:** The foreman loses inline context for older tasks; must call `get_task_status` if it needs the full brief. Raise the `get_task_status` tool result cap (see §5.3) in tandem so the foreman can recover it.

---

### 5.2 Exclude terminal tasks from the system prompt (or collapse to one line) [**High impact**]

**Current behaviour:** Done, failed, and cancelled tasks appear at the same verbosity as active ones.

**Proposed:** In the tasks query, show only non-terminal tasks at full detail; represent terminal tasks as a compact summary line (or omit them entirely when there are enough active tasks to fill the 6-task limit).

```python
# Separate active from terminal; keep up to 6 active, fill remainder with collapsed terminal
active = [t for t in task_rows if t["state"] not in ("done", "failed", "cancelled")]
terminal = [t for t in task_rows if t["state"] in ("done", "failed", "cancelled")]

collapsed_terminal = [
    {"id": t["id"], "state": t["state"], "description": t["description"][:60]}
    for t in terminal
]

tasks_block = json.dumps((active + collapsed_terminal)[:6], indent=2)
```

**Estimated saving:** In a guild with mostly completed tasks (typical after any non-trivial session) this could halve the tasks block size. Combined with §5.1, the tasks block becomes bounded and predictable.

**Trade-off:** The foreman has less context on what was already done. Mitigated by the 6-char task IDs in conversation history — completed task IDs are already mentioned in prior turns.

---

### 5.3 Per-tool result size caps (raise cap for diagnostic tools) [**Medium impact**]

**Current behaviour:** Flat 400-char cap for all tool results.

**Proposed:** Differentiate caps by tool class:

| Category | Tools | Suggested cap |
|---|---|---|
| Acknowledgement tools | `create_task`, `assign_task`, `finalize_task`, `send_followup`, `message_worker`, `cancel_task`, `redirect_task`, `claim_github_issue` | 200 chars (current is already fine) |
| Diagnostic / status | `get_task_status` | 2,000 chars (needs log lines to be useful) |
| GitHub read (list) | `list_github_issues`, `list_github_prs`, `search_github_issues` | 1,500 chars |
| GitHub read (detail) | `get_github_issue` | 2,500 chars (body already capped at 2,000 in the tool; comment list adds ~200) |
| GitHub write | `create_github_issue` | 300 chars (response is a URL + number) |

Implement as a `_RESULT_MAX_BY_TOOL` dict in `runner.py` and look up the cap before truncating.

**Estimated impact:** Net neutral on average token count (some results get larger, others stay small), but dramatically improves foreman decision quality for diagnostics and GitHub reads — meaning fewer redundant follow-up tool calls, which is a net token saving overall.

**Trade-off:** Slightly more complex truncation logic; stored history turns for diagnostic tools become larger.

---

### 5.4 Prune old DB turns (TTL or count-based) [**Low-to-medium impact on DB, negligible on token count**]

**Current behaviour:** `foreman_turns` rows accumulate indefinitely; no cleanup except `clear_foreman_history` (manual).

**Proposed:** Add a periodic or lazy cleanup:

```python
# Option A: on each load, delete rows older than the cutoff index
async def _load_and_prune_history(guild_id, user_id):
    turns = ...  # existing load
    if cutoff > 0:
        old_ids = [t.id for t in turns[:cutoff]]
        await db.execute(delete(ForemanTurn).where(ForemanTurn.id.in_(old_ids)))
        await db.commit()
    return ...

# Option B: background task that deletes foreman_turns older than N days
```

**Estimated saving:** No token saving (window is already applied in Python). Saves DB storage and speeds up the load query as history grows.

**Trade-off:** Option A (lazy prune) is simple but deletes history that might be useful for debugging. Option B is safer but requires a scheduler.

---

### 5.5 Use task count as context signal (trim tasks block for automated callbacks) [**Low impact**]

**Current behaviour:** All `run_foreman_ai()` callers get the same 6-task block regardless of invocation type.

**Proposed:** For automated callbacks (`task-complete`, `task-followup-done`, `needs-input`) where `extra_context` already contains the relevant task detail, pass only the specific task in question plus a compact summary of other active tasks rather than the full 6-task list.

```python
# For automated callbacks, pass only the triggering task at full detail
tasks_block = json.dumps([relevant_task] + compact_others, indent=2)
```

**Estimated saving:** 30–60% reduction in tasks block size for automated callbacks. Low implementation risk.

**Trade-off:** The foreman loses full visibility into concurrent tasks during a callback. Usually acceptable since `extra_context` already identifies the task being reviewed.

---

## 6. Summary table

| Opportunity | Token saving estimate | Implementation complexity | Risk |
|---|---|---|---|
| 5.1 Truncate descriptions (150 chars) | 500–5,000 tokens/call | Low — one-liner | Low |
| 5.2 Collapse terminal tasks | 200–3,000 tokens/call | Low — filter + slice | Low |
| 5.3 Per-tool result caps | 0 net (quality improvement) | Medium — dict lookup | Low |
| 5.4 DB turn pruning | 0 tokens (DB only) | Low–Medium | Low |
| 5.5 Slim tasks for callbacks | 100–500 tokens/call | Medium | Medium |

**Recommended first moves:** Implement §5.1 and §5.2 together — they are independent one-liners in `runner.py`, have no API surface change, and together cap the largest source of unbounded context growth. Then raise the `get_task_status` cap (part of §5.3) so the foreman can retrieve full task detail when it needs it.
