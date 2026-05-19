# Foreman Response Pattern Ordering

**Issue:** [#366](https://github.com/jmelloy/pioneer-square/issues/366)

## Problem

When the Foreman makes tool calls, the UI displays all tool invocations first and then narrative text afterward — reversing the logical flow. This happens because `runner.py` accumulates all text blocks across every tool-call round and broadcasts them as one message only after every round finishes.

**Current rendering order:**
```
[tool_use: create_task]
[tool_use: assign_task]
✓
I've created a task and assigned it to the worker.   ← arrives last
```

**Desired rendering order:**
```
Assigning this to the worker now.                    ← intent, arrives first
[tool_use: create_task]
[tool_use: assign_task]
✓
```

---

## Root Cause

In `backend/foreman/runner.py`, `run_foreman_ai`:

```python
# BEFORE (broken ordering)
text_parts = []
for round_num in range(MAX_FOREMAN_ROUNDS):
    resp = await client.messages.create(...)

    # Text collected but NOT broadcast yet
    text_parts += [b.text for b in resp.content if b.type == "text" and b.text.strip()]

    tool_uses = [b for b in resp.content if b.type == "tool_use"]
    if not tool_uses:
        break

    # Tool calls broadcast immediately ← UI shows these first
    for tu in tool_uses:
        await broadcast(guild_id, {tool_use message})

    tool_results = await exec_tools(...)
    for result in trimmed:
        await broadcast(guild_id, {tool_result message})

# All text broadcast AFTER all tool rounds ← UI shows this last
response_text = "\n".join(text_parts).strip()
if response_text:
    await broadcast(guild_id, {text message})
```

The Anthropic API returns text blocks _before_ tool_use blocks in the response content array — Claude already writes intent text first. The runner just needs to broadcast it in that same order.

---

## Fix

Broadcast each round's text immediately — before the tool-use events for that round.

```python
# AFTER (correct ordering)
for round_num in range(MAX_FOREMAN_ROUNDS):
    resp = await client.messages.create(...)

    round_texts = [b.text for b in resp.content if b.type == "text" and b.text.strip()]
    tool_uses  = [b for b in resp.content if b.type == "tool_use"]

    # Broadcast text FIRST — intent text if tools follow, final response if not
    if round_texts:
        _now = datetime.now(UTC).isoformat()
        _text = "\n".join(round_texts)
        await broadcast(guild_id, {"type": "chat", "from": "foreman", "content": _text, ...})
        # also persist to Message table here, not at the end

    if not tool_uses:
        break  # end_turn — text already broadcast above

    # Tool calls broadcast after their round's text
    for tu in tool_uses:
        await broadcast(guild_id, {tool_use message})

    tool_results = await exec_tools(...)
    for result in trimmed:
        await broadcast(guild_id, {tool_result message})

# No separate final broadcast needed — every round's text was sent inline
```

This also means each intermediate text segment is persisted to the `messages` table immediately, matching what was broadcast.

---

## Foreman Response Structure Guidelines

### How responses should be structured

```
[Round 1]
  text: "Checking the issue and assigning to a worker."   ← intent (one sentence)
  tool_use: get_github_issue
  tool_use: create_task                                    ← parallel: independent
  tool_result: ...
  tool_result: ...

[Round 2]
  text: "Issue confirmed, task created — assigning now."  ← brief bridge (optional)
  tool_use: assign_task                                    ← sequential: needs task_id from round 1
  tool_result: ...

[Round 3]
  text: "Assigned to w-abc123. Task t-xyz is running."    ← final summary
  (stop_reason: end_turn)
```

### Rules for sequential vs. parallel tool calls

**Use parallel (same round) when operations are independent:**
- Getting status of multiple tasks simultaneously
- Listing GitHub issues while also checking a PR
- Creating a task while fetching worker state

```python
# Claude emits these in one response → exec_tools runs them concurrently
tool_use: get_task_status(task_id="t-aaa")
tool_use: get_task_status(task_id="t-bbb")
tool_use: list_github_prs(repo="owner/repo")
```

**Use sequential (separate rounds) when the next call needs a prior result:**
- `create_task` → `assign_task` (assign needs the `task_id` returned by create)
- `get_github_issue` → `create_task` (task description should include issue details)
- `get_pr_status` → `send_followup` (followup instructions depend on PR review content)

```python
# Round 1: create_task returns task_id
tool_use: create_task(name="Fix login bug")
tool_result: {"task_id": "t-abc123", ...}

# Round 2: assign_task uses task_id from round 1
tool_use: assign_task(task_id="t-abc123", ...)
```

### Intent/result text placement

| Situation | Text to emit | When |
|-----------|-------------|------|
| About to make tool calls | One sentence: what and why | Before tool_use blocks |
| After tools return non-obvious result | One sentence: what happened | Start of next round |
| After tools return obvious result | Nothing | — |
| Final turn (no more tools) | Summary if > 1 thing happened | In the end_turn response |

**Keep each text segment to one sentence.** Multi-sentence summaries belong only in the final turn when `stop_reason == end_turn`.

### System prompt guidance (prompt.py)

The `FOREMAN_SYSTEM` prompt should include:

```
## Response structure
When a turn requires tool calls:
1. Open with one short sentence of intent before any tool call
   ("Assigning this to w-abc123 now." / "Checking the PR status.")
2. Make your tool call(s) — group independent calls in the same response so
   they execute concurrently; use a separate round when the next call needs
   the result of the previous one
3. After results arrive, add a follow-up sentence only if the outcome is
   non-obvious or the human needs to act on it

Keep each text block to one sentence. Save multi-sentence summaries for the
final turn when no more tool calls follow.
```

---

## Edge Cases

### Tool call fails (is_error: true)

The tool_result is broadcast with `isError: true`. On the next round, Claude sees the error in the tool_result content. The pattern is the same:

```
[Round N text]  "Trying to assign task..."
[tool_use]      assign_task(...)
[tool_result]   {"is_error": true, "content": "Worker not found: w-bad"}
[Round N+1 text] "Assignment failed — no worker with that ID. Trying an idle worker instead."
[tool_use]      assign_task(preferred_worker_id=...)
```

### Partial failure (some tools error, some succeed)

`exec_tools` runs all tool calls concurrently and returns results for all of them. Claude receives all results together. The next round's intent text should address the failure:

```
[Round N text]  "Creating task and fetching issue details in parallel."
[tool_use]      create_task(...)
[tool_use]      get_github_issue(...)
[tool_result]   {"task_id": "t-abc"}
[tool_result]   {"is_error": true, "content": "Issue not found"}
[Round N+1 text] "Task created. Issue lookup failed — assigning without issue link."
[tool_use]      assign_task(task_id="t-abc")
```

### MAX_FOREMAN_ROUNDS hit (else clause)

When the 10-round safety cap triggers, a wrap-up call is made with `tool_choice: none`. That response text should be broadcast inline in the else clause, not accumulated:

```python
else:
    wrap_resp = await client.messages.create(..., tool_choice={"type": "none"})
    _wrap_texts = [b.text for b in wrap_resp.content if b.type == "text" and b.text.strip()]
    _wrap_texts.append(f"_(Foreman hit {MAX_FOREMAN_ROUNDS}-round safety cap and stopped.)_")
    _wrap_body = "\n".join(_wrap_texts).strip()
    if _wrap_body:
        await broadcast(guild_id, {"type": "chat", "from": "foreman", "content": _wrap_body, ...})
        # persist to Message table
```

### Foreman produces no text in a round

This is valid — a round may have only tool calls with no accompanying text. The tool_use events are still broadcast and the frontend grouping (`useChatGrouping.ts`) handles them correctly.

---

## Files Changed

| File | Change |
|------|--------|
| `backend/foreman/runner.py` | Broadcast text per-round before tool_use events; remove end-of-loop `text_parts` accumulation |
| `backend/foreman/prompt.py` | Add `## Response structure` section to `FOREMAN_SYSTEM` |

### Frontend (no changes required)

The frontend `useChatGrouping.ts` composable already handles the correct ordering:

- Consecutive `tool_use` messages → collapsed `ToolUseGroup`
- `tool_result` messages → toggle group pending state (hidden from view)
- Regular chat messages → rendered in order received

Since messages arrive in WebSocket order and are appended to `guildStore.messages`, emitting text before tool_use ensures the text message has a lower array index and renders above the tool group.
