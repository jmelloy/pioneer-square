# Foreman MCP / Tool Extensions — Architecture Analysis

> **Date:** 2026-05-06 (revised)
> **Branch:** `claude/architecture-analysis-foreman-mcp-tool-extensions-t-h5cj`
> **Scope:** How to evolve the Foreman/Guild into a dual MCP server + MCP client, decouple it from the Anthropic SDK, and integrate [Identity-Digital/code-review-agent](https://github.com/Identity-Digital/code-review-agent) as the first concrete external tool.

---

## Core thesis

The Foreman and Guild should be **both an MCP server and an MCP client**:

- **As an MCP server** — any MCP-compatible client (Claude Desktop, Cursor, VS Code, other LLMs, custom scripts) can connect and drive Pioneer Square without going through the Anthropic Claude API.
- **As an MCP client** — the Foreman dispatches to external MCP servers (code-review-agent, static analysis tools, security scanners, etc.) using the same standard protocol, with tool schemas discovered dynamically rather than hardcoded.

Both roles are LLM-agnostic: MCP is the interface layer, so the underlying model (Claude, GPT-4o, Gemini, Ollama, etc.) becomes a pluggable backend.

---

## A. Current Architecture Summary

### How Foreman tools are defined today

All Foreman tools live in `backend/foreman/tools.py` as a single Python list of Anthropic-format dicts:

```python
FOREMAN_TOOLS: list[dict] = [
    { "name": "create_task",      "input_schema": { ... } },
    { "name": "assign_task",      "input_schema": { ... } },
    { "name": "send_followup",    "input_schema": { ... } },
    { "name": "finalize_task",    "input_schema": { ... } },
    { "name": "redirect_task",    "input_schema": { ... } },
    { "name": "cancel_task",      "input_schema": { ... } },
    { "name": "message_worker",   "input_schema": { ... } },
    { "name": "shutdown_worker",  "input_schema": { ... } },
    # GitHub API tools (8):
    { "name": "list_github_issues",   ... },
    { "name": "get_github_issue",     ... },
    { "name": "list_github_prs",      ... },
    { "name": "claim_github_issue",   ... },
    { "name": "create_github_issue",  ... },
    { "name": "get_pr_status",        ... },
    { "name": "search_github_issues", ... },
    { "name": "get_task_status",      ... },
]
```

These are passed verbatim to `anthropic.AsyncAnthropic().messages.create(tools=FOREMAN_TOOLS, ...)` in `backend/foreman/runner.py`. There is no MCP layer — tools are plain Python dicts and new tools require manual edits to both the schema list and `_exec_one_tool`.

### Tool execution flow

```
run_foreman_ai()
  │
  ├─ build_system_blocks()       # cacheable system prompt
  ├─ build_state_preamble()      # live workers/tasks snapshot injected as user-turn prefix
  ├─ _load_history()             # last 5 human turns + tool-result chains from DB
  │
  └─ loop (≤10 rounds):
       anthropic.messages.create(tools=FOREMAN_TOOLS)
         │
         └─ if stop_reason == "tool_use":
              exec_tools(tool_calls)
                ├─ _exec_one_tool("create_task")   → INSERT tasks row, broadcast WS event
                ├─ _exec_one_tool("assign_task")   → UPDATE task, send task-assigned WS msg
                ├─ _exec_one_tool("get_pr_status") → GitHub REST API call
                └─ ...
              tool_results appended to messages
         │
         └─ if stop_reason == "end_turn":  break
```

Each round's messages are persisted to `ForemanTurn` (DB). Tool results are capped at 8,000 characters before being stored or fed back.

### Where results flow

| Tool category | Result destination |
|---|---|
| Task lifecycle (`create_task`, `assign_task`, …) | SQLite `tasks` table + WebSocket broadcast to all guild clients |
| GitHub API (`get_pr_status`, `list_github_prs`, …) | Returned as tool-result text to Foreman; not persisted |
| Worker control (`message_worker`, `send_followup`) | WebSocket message sent directly to the target worker agent |
| Diagnostics (`get_task_status`) | In-memory DB query result returned as text |

### Anthropic SDK coupling points

The following locations in `backend/foreman/runner.py` are tightly coupled to the Anthropic SDK and would need to change for LLM-agnostic operation:

| Location | Coupling |
|---|---|
| `import anthropic as _anthropic` (line 17–21) | Hard SDK import; `HAS_ANTHROPIC` guard makes the whole Foreman conditional |
| `_anthropic_client: AsyncAnthropic` (line 42) | Module-level client singleton |
| `client.messages.create(model="claude-sonnet-4-6", ...)` (line 576) | Model name and Anthropic messages API |
| `resp.stop_reason`, `resp.content`, `resp.usage` (lines 583–595) | Anthropic response object attributes |
| `b.type == "tool_use"`, `b.text`, `b.model_dump()` (lines 603–605) | Anthropic `ContentBlock` types |
| `cache_control: {"type": "ephemeral"}` (line 94) | Anthropic prompt-caching feature; not in MCP or other SDKs |
| `FOREMAN_TOOLS` using `input_schema` key | Anthropic's tool format; MCP uses `inputSchema` (camelCase) |

### Existing extension points

1. **`FOREMAN_TOOLS` list** — add any dict with `name` + `input_schema`, register a handler in `_exec_one_tool`. No other wiring needed today.
2. **`exec_tools()` is async and concurrent** — new tools can be I/O-heavy without blocking the tool loop.
3. **`MAX_TOOL_RESULT_CHARS = 8_000`** — structured review data returned from external services needs to be summarised before being fed back.
4. **Worker MCP config** — workers invoke `claude --output-format stream-json`. Claude Code reads MCP server config from `~/.claude.json` / `--mcp-config`. Workers can carry pre-configured MCP servers without backend changes.
5. **`needs-input` escalation path** — workers can pause mid-task and await human (or Foreman) input; a review service could interrupt via this path.

### What is NOT there yet

- No MCP server surface — no external client can drive the Foreman via standard protocol.
- No MCP client library in the backend — tools make direct HTTP calls or DB writes.
- No dynamic tool discovery — adding a tool requires editing Python source.
- No webhook receiver for GitHub PR review events flowing back into the Foreman.
- No LLM abstraction layer — swapping models requires rewriting `runner.py`.

---

## B. MCP Integration Patterns

### What MCP is in this context

The Model Context Protocol (MCP) is a JSON-RPC 2.0 protocol (over stdio or HTTP/SSE) that standardises how AI agents expose and consume tools. A server advertises its tools via `tools/list`; a client calls them via `tools/call`. From any LLM's perspective, MCP tools look identical to native function-calling schemas — the protocol is the portability layer.

The two relevant transports:
- **stdio** — MCP server is a subprocess; calls go over stdin/stdout. Zero network config; subprocess lifetime tied to caller.
- **HTTP/SSE** — MCP server is a persistent HTTPS service. Supports auth, multi-tenant, and remote deployment.

### Pattern 1 — External MCP client → Foreman MCP server (Foreman as server)

The Foreman exposes its own MCP server so that *any* MCP-compatible client can connect and drive it — Claude Desktop, Cursor, VS Code Copilot, custom scripts, or other LLMs.

```
Claude Desktop / Cursor / VS Code / custom client
  │
  │  MCP  (stdio or HTTP/SSE)
  ▼
Foreman MCP Server  (new: backend/foreman/mcp_server.py)
  │  tools/list → ["create_task", "assign_task", "get_task_status", …]
  │  tools/call("assign_task", {worker_id, task_id})
  ▼
Existing exec_tools() dispatcher → DB + WebSocket broadcast
```

**MCP tool surface the Foreman would expose:**

| MCP tool | Maps to | Notes |
|---|---|---|
| `create_task` | `_exec_create_task` | Creates task row, returns task_id |
| `assign_task` | `_exec_assign_task` | Sends task-assigned WS msg to worker |
| `send_followup` | `_exec_send_followup` | Re-queues work on existing branch |
| `finalize_task` | `_exec_finalize_task` | Marks task done, soft-deletes |
| `cancel_task` | `_exec_cancel_task` | Cancels pending/working task |
| `get_task_status` | `_exec_get_task_status` | Returns task state JSON |
| `list_workers` | DB query | Returns online workers + repos |
| `message_worker` | `_exec_message_worker` | Sends direct WS msg to worker |
| `list_github_prs` | GitHub API | Returns open PRs for guild repo |
| `get_pr_status` | GitHub API | Returns PR review status |

**Schema translation:** The existing `FOREMAN_TOOLS` dicts use Anthropic's `input_schema` key. MCP uses `inputSchema` (camelCase). The translation is mechanical — a small adapter function converts at server startup:

```python
def _to_mcp_tool(anthropic_tool: dict) -> dict:
    return {
        "name": anthropic_tool["name"],
        "description": anthropic_tool.get("description", ""),
        "inputSchema": anthropic_tool["input_schema"],
    }
```

This means the canonical tool definitions live in `FOREMAN_TOOLS` and are served to both the Anthropic SDK loop and MCP clients from the same source of truth.

**Trade-offs:**

| | |
|---|---|
| ✅ Any MCP-compatible client can drive Pioneer Square — not just Claude | |
| ✅ Claude Desktop / Cursor users can directly call `create_task` from their IDE | |
| ✅ No LLM API key required to use the Foreman programmatically | |
| ✅ `FOREMAN_TOOLS` is already the right abstraction; the MCP server is mostly a protocol adapter | |
| ⚠️ Adds a persistent HTTP service (or stdio management) to the backend | |
| ⚠️ Auth: MCP server needs an auth layer (token / mTLS) so arbitrary clients can't drive the guild | |
| ⚠️ The existing `run_foreman_ai()` loop is the LLM driving tools; the MCP server exposes tools *for* an LLM driver — the two must coexist cleanly | |

### Pattern 2 — Foreman → external MCP server (Foreman as client)

The Foreman connects to external MCP servers as a client and calls their tools during its dispatch loop. Tool schemas are discovered dynamically via `tools/list` at startup; `_exec_one_tool` proxies MCP-origin calls through the MCP client at runtime.

```
Foreman tool-call loop (exec_tools)
  │
  ├─ local tools (create_task, assign_task, …)  → handled inline as today
  │
  └─ MCP-sourced tools (review_pr, run_semgrep, …)
       │
       └─ MCP client (new: backend/foreman/mcp_client.py)
            │  tools/call("review_pr", {pr_url: "…"})
            ▼
       code-review-agent MCP server  (HTTP/SSE or stdio)
            │  SSE stream → structured report
            ▼
       summarised tool result → back into Foreman message chain
```

**Dispatch layer changes needed:**

Today `exec_tools()` in `tools.py` is a flat `if/elif` chain keyed on tool name. With MCP client support it becomes a two-phase dispatch:

```python
async def exec_tools(guild_id, tool_uses, *, user_id=None):
    local_names = {t["name"] for t in LOCAL_FOREMAN_TOOLS}
    mcp_registry  = await _get_mcp_registry()   # name → MCPClient instance

    results = await asyncio.gather(*[
        _exec_one_tool(guild_id, tu, user_id=user_id)
        if tu.name in local_names
        else mcp_registry[tu.name].call(tu.name, tu.input)
        for tu in tool_uses
    ])
    return results
```

`_get_mcp_registry()` fetches `tools/list` from each configured MCP server on first call (cached with TTL), returning a flat `{tool_name: client}` map. New tools become available to the Foreman without code changes — only a config entry in `mcp_servers` (e.g. in `backend/.env` or a guild-level config table).

**Trade-offs:**

| | |
|---|---|
| ✅ Integrations (code-review-agent, scanners, etc.) are deployed as MCP servers — no backend source changes per integration | |
| ✅ Tools are discovered at runtime; adding an integration is a config change, not a code change | |
| ✅ MCP servers can be co-located (stdio) or remote (HTTP) — same client code | |
| ✅ The Foreman sees review results in its tool-call context and can reason about them immediately | |
| ⚠️ Adds a network dependency per MCP server — failure of any server can break the Foreman's tool loop | |
| ⚠️ Tool result size limit (8,000 chars) means review reports must be summarised before feeding back | |
| ⚠️ SSE review streams can take 30–120 s; must be awaited or given a generous timeout | |
| ⚠️ MCP server auth (API keys, mTLS) must be managed per server in backend config | |

### Pattern 3 — Worker-side MCP (worker's Claude Code session calls the MCP server)

```
Worker (subprocess)
  │  runs: claude --output-format stream-json --mcp-config /etc/pioneer/mcp.json
  │
  └─ Claude Code (worker subprocess)
       ├─ reads task description: "Review PR #42…"
       └─ calls MCP tool: review_pr(pr_url="https://github.com/…/pull/42")
            └─ crv-mcp (stdio subprocess of Claude Code)
                 └─ HTTP → code-review-agent MCP server → SSE → structured report
```

**How to wire it:** Ship a `~/.claude.json` (or `--mcp-config`) in the worker container that registers `crv-mcp`. No backend changes. Workers pick up the MCP tool at Claude Code startup.

**Trade-offs:**

| | |
|---|---|
| ✅ Zero backend changes — purely a worker deployment concern | |
| ✅ Exactly how code-review-agent is designed to be used | |
| ✅ Review report is in the worker's context; Claude can act on it directly (push comments, update code) | |
| ✅ LLM-agnostic at the worker level too — any Claude Code-compatible runner picks up the MCP config | |
| ⚠️ Foreman cannot directly observe review results — must parse worker terminal output | |
| ⚠️ Workers must have `crv-mcp` installed; adds provisioning complexity | |
| ⚠️ MCP server config is per-worker-machine, not per-guild | |
| ❌ Not usable when the Foreman wants to initiate a review outside of a worker task | |

### Pattern comparison matrix

| Dimension | P1: Foreman as MCP server | P2: Foreman as MCP client | P3: Worker-side MCP |
|---|---|---|---|
| Who drives the Foreman | External MCP clients (any LLM) | Internal LLM loop | Worker subprocess |
| Backend changes | Medium (new MCP server layer) | Medium (MCP client + dynamic dispatch) | None |
| Worker changes | None | None | Medium (crv-mcp in config) |
| LLM-agnostic | Yes — client brings its own LLM | Partially — Foreman LLM still needed | Yes — worker uses Claude Code |
| Dynamic tool discovery | N/A (Foreman is the server) | Yes — from connected MCP servers | Yes — Claude Code discovers tools |
| Integration complexity | Low per integration | Low per integration (config only) | Medium per worker |
| Auth complexity | Medium (Foreman server auth) | Medium (per MCP server creds) | Low (per-worker creds) |
| Foreman awareness of results | Full (Foreman is the executor) | Full (tool result in context) | Via terminal output only |

---

## C. LLM-Agnostic Design

### Why this matters

The current `runner.py` is a thin wrapper around `anthropic.AsyncAnthropic`. Every LLM-touching operation uses Anthropic SDK types: `ContentBlock`, `ToolUseBlock`, `stop_reason`, `cache_control`. Swapping to GPT-4o or a local model via Ollama requires rewriting `run_foreman_ai()` wholesale.

MCP provides the natural abstraction boundary: if the Foreman exposes its actions as MCP tools (Pattern 1) and discovers external tools via MCP (Pattern 2), the LLM becomes a detail — it just needs to support tool calling and speak JSON.

### Proposed `LLMBackend` abstraction

```python
# backend/foreman/llm_backend.py

from typing import Protocol, AsyncIterator

class ToolCall:
    id: str
    name: str
    input: dict

class LLMResponse:
    text_parts: list[str]
    tool_calls: list[ToolCall]
    stop_reason: str   # "end_turn" | "tool_use" | "max_tokens"

class LLMBackend(Protocol):
    async def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],          # MCP-format: {"name", "description", "inputSchema"}
        max_tokens: int,
        tool_choice: str | None,
    ) -> LLMResponse: ...
```

`runner.py` calls only `LLMBackend.complete()`. Concrete implementations:

| Class | Backend |
|---|---|
| `AnthropicBackend` | Wraps `anthropic.AsyncAnthropic`; translates `inputSchema` ↔ `input_schema`, maps `ContentBlock` → `LLMResponse` |
| `OpenAIBackend` | Wraps `openai.AsyncOpenAI`; maps function-calling response to `LLMResponse` |
| `OllamaBackend` | HTTP to local Ollama `/api/chat` with `tools` param (Ollama ≥ 0.3) |

**Anthropic-specific features that don't port cleanly:**
- `cache_control: {"type": "ephemeral"}` — Anthropic prompt caching. Abstract as an optional `supports_caching: bool` flag on the backend; `AnthropicBackend` sets it `True` and injects cache control blocks; others ignore it.
- `model="claude-sonnet-4-6"` — move to backend constructor config, not hardcoded in `runner.py`.

### Coupling points to change in `runner.py`

```python
# Before (Anthropic-coupled):
client = _get_anthropic_client()
resp = await client.messages.create(
    model="claude-sonnet-4-6",
    system=system_blocks,
    messages=messages,
    tools=FOREMAN_TOOLS,            # Anthropic format
    max_tokens=1024,
)
tool_uses = [b for b in resp.content if b.type == "tool_use"]

# After (LLM-agnostic):
backend = _get_llm_backend()        # returns configured LLMBackend
resp = await backend.complete(
    system=_flatten_system(system_blocks),
    messages=messages,
    tools=_to_mcp_tools(FOREMAN_TOOLS),   # convert once at startup
    max_tokens=1024,
)
tool_uses = resp.tool_calls
```

The rest of `runner.py` — history loading, DB persistence, WebSocket broadcasting, the tool-call loop — is already LLM-agnostic and needs no changes.

---

## D. code-review-agent Integration Design

### code-review-agent as an MCP server

[Identity-Digital/code-review-agent](https://github.com/Identity-Digital/code-review-agent) ships `crv-mcp`, an MCP stdio server that wraps its review harness. The Foreman connects to it as an MCP client (Pattern 2).

The MCP server exposes tools including:
- `start_conversation` — initiate a review session for a PR URL
- (additional tools for querying review state, posting comments)

### Recommended pattern: P2 (Foreman as MCP client) primary, P3 (Worker-side) for self-service

**Why P2 primary:** The Foreman can gate `finalize_task` on a passing review without any worker involvement. Review results land directly in the Foreman's tool-call context. No worker provisioning changes needed.

**Why P3 as complement:** Workers can self-service a review during task execution (before even sending `task-complete`) — catching issues earlier in the loop.

### New Foreman tool: `request_code_review`

This tool is registered locally in `FOREMAN_TOOLS` but its implementation delegates to the code-review-agent MCP server via the MCP client dispatcher:

```python
{
    "name": "request_code_review",
    "description": (
        "Request an automated code review for a pull request via code-review-agent. "
        "Returns a structured report: blocking issues, suggestions, verdict (pass/fail). "
        "Call after a worker opens a PR to gate finalization."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pr_url": {
                "type": "string",
                "description": "Full GitHub PR URL, e.g. https://github.com/org/repo/pull/42",
            },
            "task_id": {
                "type": "string",
                "description": "Task ID to attach the review result to.",
            },
        },
        "required": ["pr_url"],
    },
}
```

Implementation delegates to the MCP client:

```python
async def _exec_request_code_review(guild_id, inp):
    mcp = await _get_mcp_client("code-review-agent")
    # MCP call: tools/call → start_conversation
    result = await mcp.call("start_conversation", {
        "agent_url": CRA_AGENT_URL,
        "initial_text": inp["pr_url"],
    })
    summary = _summarise_review(result, max_chars=6000)
    if inp.get("task_id"):
        await _persist_review_artifact(inp["task_id"], result)
    return summary
```

### Feedback loop

```
Worker
  │  opens PR via GitHub API
  │  sends task-complete → backend
  │
Backend
  │  triggers run_foreman_ai()
  │  Foreman calls request_code_review(pr_url)
  │    └─ MCP client → code-review-agent MCP server (SSE stream)
  │    └─ returns structured report
  │
  ├─ if report.verdict == "pass":
  │    Foreman calls finalize_task(task_id)
  │
  └─ if report.verdict == "fail":
       Foreman calls send_followup(task_id,
         f"Automated review found {n} issues:\n{issues}\nPlease fix and push.")
         │
         └─ Worker re-runs Claude on same worktree/branch
              └─ Pushes fix commits → new task-complete
                   └─ Loop repeats (cap: 3 rounds)
```

### Sequence diagram (MCP protocol messages)

```
User        Foreman (MCP client)   Worker    GitHub    CRA MCP Server
 │                  │                 │          │            │
 │  "Fix bug #38"   │                 │          │            │
 │─────────────────►│                 │          │            │
 │                  │ create_task     │          │            │
 │                  │ assign_task     │          │            │
 │                  │────────────────►│          │            │
 │                  │                 │ worktree+claude       │
 │                  │                 │──────────────────►    │
 │                  │                 │ push branch           │
 │                  │                 │─────────►│            │
 │                  │                 │ open PR  │            │
 │                  │                 │─────────►│            │
 │                  │                 │ task-complete         │
 │                  │◄────────────────│          │            │
 │                  │                 │          │            │
 │                  │  MCP: tools/call("start_conversation",  │
 │                  │       {agent_url, initial_text: pr_url})│
 │                  │────────────────────────────────────────►│
 │                  │                 │          │  SSE stream│
 │                  │◄────────────────────────────────────────│
 │                  │  MCP result: {verdict:"fail", issues:[…]}
 │                  │                 │          │            │
 │                  │ send_followup(task_id, "Fix: …")        │
 │                  │────────────────►│          │            │
 │                  │                 │ (re-runs claude, pushes)
 │                  │                 │ task-followup-done    │
 │                  │◄────────────────│          │            │
 │                  │                 │          │            │
 │                  │  MCP: tools/call("start_conversation",  │
 │                  │────────────────────────────────────────►│
 │                  │◄────────────────────────────────────────│
 │                  │  MCP result: {verdict:"pass"}           │
 │                  │                 │          │            │
 │                  │ finalize_task(task_id)      │            │
```

### Auth and secrets required

| Secret | Where stored | Notes |
|---|---|---|
| `CRA_MCP_SERVER_URL` | Backend env / `.env` | HTTP/SSE endpoint of code-review-agent MCP server; or path to `crv-mcp` binary for stdio |
| `CRA_TRANSPORT` | Backend env | `"stdio"` or `"http"` |
| GitHub token | Already in `github_tokens` table | Forwarded to CRA MCP server per-call |
| `crv-mcp` config | Worker `~/.claude.json` or `--mcp-config` | For P3; points to local `crv-mcp` binary |

---

## E. Other High-Value Tool Extensions

The same MCP client dispatch pattern applies to a range of integrations. Each becomes an MCP server; the Foreman discovers and calls it via `tools/list` + `tools/call`.

### Static analysis / linting (`run_static_analysis`)
- **MCP server:** Wraps `ruff`, `eslint`, `mypy`, or `semgrep`; exposes `lint_pr(pr_url)` tool
- **Pattern:** P2 (Foreman calls after `task-complete`) or P3 (worker runs before pushing)
- **Output:** Line-level findings → Foreman includes in `send_followup`
- **Complexity:** Low — no external service dependency; MCP server is a thin subprocess wrapper

### Security scanning (`request_security_scan`)
- **MCP server:** Wraps `bandit`, `safety`, `trivy`, or `semgrep` with security rulesets; exposes `scan_pr(pr_url)` tool
- **Pattern:** P2 — Foreman gates finalization on severity < HIGH
- **Output:** SARIF or JSON findings → can be posted to GitHub Code Scanning API
- **Complexity:** Medium — SARIF posting requires additional GitHub API calls

### Dependency update automation (`create_dependency_update_task`)
- **MCP server:** Queries GitHub dependency graph API or runs `pip-audit` / `npm audit`; exposes `list_outdated_deps(repo)` tool
- **Pattern:** P2 — Foreman creates a worker task per outdated package group
- **Complexity:** Low — GitHub REST API already wired; MCP server is a thin adapter

### Test generation (`generate_tests`)
- **Pattern:** P3 — worker task phase where Claude generates test files and pushes to same branch
- **No MCP server needed** — pure Foreman orchestration via `send_followup`
- **Complexity:** Low

### Performance profiling (`profile_pr`)
- **MCP server:** CI-triggered benchmark runner; exposes `profile_branch(branch)` tool
- **Pattern:** P2/P3 — results posted as PR comment; Foreman notified
- **Complexity:** Medium — needs a runner environment with the app running

### Documentation generation (`update_docs`)
- **Pattern:** P3 — worker task phase after code changes
- **Complexity:** Low — pure worker task

---

## F. Recommended Roadmap

Items are ordered by value/effort ratio. Each is scoped to fit as a single worker task in pioneer-square itself.

### Phase 0 — LLM abstraction (enables everything else)

| # | Item | Effort |
|---|---|---|
| 0a | **`LLMBackend` protocol**: define `LLMResponse`, `ToolCall`, `LLMBackend` in `backend/foreman/llm_backend.py` | XS |
| 0b | **`AnthropicBackend` implementation**: wraps existing `anthropic.AsyncAnthropic`; translates `input_schema` ↔ `inputSchema`, maps `ContentBlock` → `LLMResponse`; preserves `cache_control` optimisation | S |
| 0c | **Refactor `runner.py`**: replace direct Anthropic SDK calls with `backend.complete()`; hardcoded `model=` moves to env config | S |

### Phase 1 — Foreman as MCP server (makes Foreman drivable from any client)

| # | Item | Effort |
|---|---|---|
| 1a | **`ForemanMCPServer` class** in `backend/foreman/mcp_server.py`: HTTP/SSE MCP server using `mcp` Python library; serves `tools/list` from `FOREMAN_TOOLS` (auto-translated to MCP format) and dispatches `tools/call` to existing `exec_tools()` | M |
| 1b | **FastAPI mount**: expose MCP server at `/mcp/{guild_id}` (guild-scoped); add bearer token auth | S |
| 1c | **Tool schema adapter**: `_to_mcp_tool()` utility that converts `input_schema` → `inputSchema`; ensure canonical defs live in `FOREMAN_TOOLS` and are shared | XS |
| 1d | **Claude Desktop / Cursor config example** in `docs/`: show how to connect an MCP client to a running Pioneer Square instance | XS |

### Phase 2 — Foreman as MCP client (enables external integrations)

| # | Item | Effort |
|---|---|---|
| 2a | **`MCPClientRegistry`** in `backend/foreman/mcp_client.py`: connects to configured MCP servers at startup (stdio or HTTP); caches `tools/list` results; exposes `call(tool_name, input)` | M |
| 2b | **Dynamic dispatch in `exec_tools()`**: two-phase dispatch — local tools handled inline; MCP-origin tools proxied through `MCPClientRegistry` | S |
| 2c | **MCP server config**: guild-level or backend-level config for MCP server endpoints (env vars or new `mcp_servers` DB table) | S |
| 2d | **`review_artifacts` table**: `(task_id, type, payload JSON, created_at)` + `get_review_artifacts(task_id)` Foreman tool | S |

### Phase 3 — code-review-agent integration

| # | Item | Effort |
|---|---|---|
| 3a | **Deploy code-review-agent MCP server**: Dockerfile + compose service; expose at `CRA_MCP_SERVER_URL`; wire into `MCPClientRegistry` config | M |
| 3b | **`request_code_review` Foreman tool**: schema + local handler that calls `MCPClientRegistry`; summarises result to fit `MAX_TOOL_RESULT_CHARS` | S |
| 3c | **Review-gated finalization**: Foreman delays `finalize_task` until verdict is `pass`; loops `send_followup` on failures (cap: 3 rounds) | M |
| 3d | **Worker crv-mcp onboarding** (P3 complement): worker install script writes `~/.claude.json` MCP entry; task descriptions can reference `review_pr` tool directly | S |

### Phase 4 — Additional integrations and webhook automation

| # | Item | Effort |
|---|---|---|
| 4a | **Static analysis MCP server**: thin wrapper around `ruff` / `eslint`; registered in `MCPClientRegistry` | S |
| 4b | **Security scan MCP server**: wraps `bandit` / `trivy`; blocks finalization on severity ≥ HIGH | M |
| 4c | **GitHub webhook receiver**: `POST /webhooks/github/{guild_id}`; validates HMAC; handles `pull_request.opened` → injects `[github-event]` Foreman message | M |
| 4d | **Foreman state preamble: pending reviews**: `build_state_preamble()` includes open review results from `review_artifacts` so Foreman is always aware of blockers | S |

### Estimated sizing

- **XS** = < 1 hour (schema/type definitions, adapters)
- **S** = 2–4 hours (single focused PR)
- **M** = ~1 day (may span multiple PRs / follow-ups)

Phase 0 is a prerequisite for nothing operationally (existing Anthropic path continues to work) but unlocks Phases 1–4 and should be done first. Phases 1 and 2 are independent and can be parallelised across workers. Phase 3 depends on Phase 2 (`MCPClientRegistry`). Phase 4 depends on Phase 3 being stable.

---

## Appendix: Key files for implementors

| File | Relevance |
|---|---|
| `backend/foreman/tools.py` | `FOREMAN_TOOLS` — canonical tool defs; add new tools here; `exec_tools()` dispatcher |
| `backend/foreman/runner.py` | `run_foreman_ai()` — main LLM loop; coupling points listed in §A |
| `backend/foreman/prompt.py` | `build_state_preamble()` — add pending review summaries here |
| `backend/foreman/llm_backend.py` | **New (Phase 0)** — `LLMBackend` protocol + `AnthropicBackend` |
| `backend/foreman/mcp_server.py` | **New (Phase 1)** — `ForemanMCPServer`; MCP server over HTTP/SSE |
| `backend/foreman/mcp_client.py` | **New (Phase 2)** — `MCPClientRegistry`; MCP client dispatcher |
| `worker/pioneer_worker/claude_runner.py` | Add `--mcp-config` flag to `run_claude_auto()` for P3 |
| `worker/pioneer_worker/worker.py` | Pass `mcp_config_path` from worker config to `run_claude_auto` |
| `backend/models.py` | Add `ReviewArtifact` ORM model (Phase 2d) |
| `backend/alembic/versions/` | Migration for `review_artifacts` table |
| `backend/main.py` | Mount MCP server at `/mcp/{guild_id}`; add webhook route |
