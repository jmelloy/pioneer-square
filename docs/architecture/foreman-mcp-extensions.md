# Foreman MCP / Tool Extensions — Architecture Analysis

> **Date:** 2026-05-06  
> **Branch:** `claude/architecture-analysis-foreman-mcp-tool-extensions-t-h5cj`  
> **Scope:** How to extend the Foreman AI layer with MCP servers and external tool integrations, using [Identity-Digital/code-review-agent](https://github.com/Identity-Digital/code-review-agent) as the first concrete target.

---

## A. Current Architecture Summary

### How Foreman tools are defined today

All Foreman tools live in `backend/foreman/tools.py` as a single Python list:

```python
FOREMAN_TOOLS: list[dict] = [
    { "name": "create_task",   "input_schema": { ... } },
    { "name": "assign_task",   "input_schema": { ... } },
    { "name": "send_followup", "input_schema": { ... } },
    { "name": "finalize_task", "input_schema": { ... } },
    { "name": "redirect_task", "input_schema": { ... } },
    { "name": "cancel_task",   "input_schema": { ... } },
    { "name": "message_worker","input_schema": { ... } },
    { "name": "shutdown_worker","input_schema": { ... } },
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

These are passed verbatim to `anthropic.AsyncAnthropic().messages.create(tools=FOREMAN_TOOLS, ...)` in `backend/foreman/runner.py`. There is no MCP client layer — tools are plain Python dicts whose `input_schema` follows Anthropic's tool-calling format.

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
                ├─ _exec_one_tool("assign_task")   → UPDATE task, send task-assigned WS msg to worker
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

### Existing extension points

1. **`FOREMAN_TOOLS` list** — add any dict with `name` + `input_schema`, register a handler in `_exec_one_tool`. No other wiring needed.
2. **`exec_tools()` is async and concurrent** — new tools can be I/O-heavy without blocking the tool loop.
3. **`MAX_TOOL_RESULT_CHARS = 8_000`** — structured review data returned from external services needs to be summarised before being fed back to the Foreman.
4. **Worker MCP config** — workers invoke `claude --output-format stream-json`. Claude Code reads MCP server config from `~/.claude.json` / `--mcp-config`. Workers can be shipped with pre-configured MCP servers without backend changes.
5. **`needs-input` escalation path** — workers can pause mid-task and await human (or Foreman) input; a review service could interrupt via this path.

### What is NOT there yet

- No MCP client library in the backend; tools make direct HTTP calls or DB writes.
- No webhook receiver for GitHub PR review events flowing back into the Foreman.
- No structured artifact storage — tool results are just text blobs.

---

## B. MCP Integration Patterns

### What MCP is in this context

The Model Context Protocol (MCP) is a JSON-RPC 2.0 protocol (over stdio or HTTP/SSE) that lets an AI client discover and call tools hosted by an external server. From the Anthropic SDK's perspective, MCP tools look identical to native `tools=[]` tool definitions — the difference is that tool schemas are fetched dynamically from the MCP server at startup and tool calls are proxied through it at runtime.

The two relevant transports:
- **stdio** — MCP server is a subprocess; tool calls are sent over stdin/stdout. Zero network config, subprocess lifecycle tied to the caller.
- **HTTP/SSE** — MCP server is a persistent HTTPS service; caller connects at a URL. Supports auth, multi-tenant, and remote deployment.

### Pattern 1 — Foreman-side MCP server (Foreman calls MCP tools directly)

```
Foreman (backend process)
  │  uses anthropic.AsyncAnthropic with tools=FOREMAN_TOOLS + MCP_TOOLS
  │
  ├─ anthropic.messages.create()
  │    └─ stop_reason == "tool_use" for "review_pr"
  │
  └─ exec_tools()
       └─ MCP client → HTTP POST to code-review-agent harness
            └─ SSE stream back → summarised text → tool result
```

**How to wire it:** Add an `mcp` Python library (`mcp` or `anthropic-mcp`) as a backend dependency. At startup, fetch `tools/list` from the MCP server, merge the returned schema dicts into `FOREMAN_TOOLS`, and in `_exec_one_tool` proxy any MCP-origin tool call through the MCP client.

**Trade-offs:**

| | |
|---|---|
| ✅ Foreman sees review results in its tool-call context; can reason about them immediately | |
| ✅ Centrally managed — one config change wires all guilds | |
| ✅ Works with the existing Anthropic SDK loop without structural changes | |
| ⚠️ Adds a synchronous call in the Foreman's tool loop — SSE review streams can take 30–120 s; must be awaited or summarised | |
| ⚠️ Backend process now has an outbound network dependency; failure breaks Foreman | |
| ⚠️ Tool result size limit (8,000 chars) means review reports must be truncated | |
| ❌ DNSid mutual auth requires a backend-side identity, adding operational complexity | |

### Pattern 2 — Worker-side MCP (worker's Claude Code session calls the MCP server)

```
Worker (subprocess)
  │  runs: claude --output-format stream-json --mcp-config /etc/pioneer/mcp.json
  │
  └─ Claude Code (worker subprocess)
       ├─ reads task description: "Review PR #42…"
       └─ calls MCP tool: start_conversation(agent_url=..., initial_text="https://github.com/…/pull/42")
            └─ crv-mcp (stdio subprocess of Claude Code)
                 └─ HTTP → code-review-agent harness → SSE → structured report
```

**How to wire it:** Ship a `~/.claude.json` (or `--mcp-config`) in the worker container/venv that registers `crv-mcp`. No backend changes. Workers pick up the MCP tool at Claude Code startup. The task description tells the worker to use the review tool.

**Trade-offs:**

| | |
|---|---|
| ✅ Zero backend changes — purely a worker deployment concern | |
| ✅ Exactly how code-review-agent is designed to be used | |
| ✅ Review report is in the worker's context; Claude can act on it directly (push comments, update code) | |
| ✅ Each worker process manages its own MCP subprocess lifetime | |
| ⚠️ Foreman cannot directly observe review results — must parse worker terminal output | |
| ⚠️ Workers must have `crv-mcp` installed; adds worker provisioning complexity | |
| ⚠️ MCP server config is per-worker-machine, not per-guild | |
| ❌ Not usable when the Foreman wants to initiate a review outside of a worker task | |

### Pattern 3 — Sidecar service (GitHub webhook → Foreman trigger)

```
GitHub
  │  PR review_requested / pull_request_review / CI status event
  │
  ▼
Backend webhook receiver  (new route: POST /webhooks/github/{guild_id})
  │
  ├─ Calls code-review-agent harness (HTTP POST /jsonrpc → SSE stream)
  │    └─ Structured review report (JSON + Markdown)
  │
  ├─ Persists report to new `review_artifacts` table
  │
  └─ Injects synthetic chat message into guild:
       "[github-event] PR #42 review complete — 3 blocking issues found. <summary>"
         │
         └─ triggers run_foreman_ai()
              └─ Foreman calls send_followup() or finalize_task() based on report
```

**Trade-offs:**

| | |
|---|---|
| ✅ Fully event-driven; review happens when PR is opened, not just when a task completes | |
| ✅ Report is stored as an artifact, queryable by any future Foreman turn | |
| ✅ Decouples review latency from task execution — no Foreman wait | |
| ✅ Most resilient — code-review-agent can be restarted independently | |
| ⚠️ Requires GitHub webhook setup per repo (manageable via the `claim_github_issue` path) | |
| ⚠️ Adds a new persistent service to operate (code-review-agent harness) | |
| ⚠️ Foreman must be taught to interpret review artifacts in its state preamble | |
| ❌ Highest initial deployment complexity (DNSid identity, allowlist, ngrok/public URL) | |

### Pattern comparison matrix

| Dimension | P1: Foreman-MCP | P2: Worker-MCP | P3: Sidecar |
|---|---|---|---|
| Backend changes | Medium (MCP client, tool merger) | None | Large (webhook, artifact store) |
| Worker changes | None | Medium (crv-mcp in config) | None |
| Review latency | Blocks Foreman turn (30–120 s) | Inline during task | Async, decoupled |
| Foreman awareness | Immediate | Via terminal output | Via synthetic event |
| Auth complexity | High (backend DNSid identity) | Low (per-worker creds) | High (webhook + DNSid) |
| Multi-guild support | Built-in | Built-in | Requires guild routing |
| Resilience to CRA outage | Low (breaks Foreman) | Medium (task fails) | High (queue + retry) |

---

## C. code-review-agent Integration Design

### Recommended pattern: P2 (Worker-MCP) first, P3 (Sidecar) later

**Why P2 first:**  
code-review-agent is explicitly designed to be consumed as a Claude Code MCP plugin via `crv-mcp`. The worker already runs `claude --output-format stream-json`; adding `--mcp-config` or dropping a `~/.claude.json` is the minimal-friction path. The Foreman can instruct a worker "review PR #42 using the code-review-agent MCP tool and post a summary as a PR comment" without any backend changes.

**Why P3 later:**  
Once the integration is proven, the sidecar pattern makes reviews automatic on every opened PR — no explicit Foreman task needed. This is the right end-state but requires more infrastructure.

### New Foreman tool for P1/P3 (optional, future): `request_code_review`

```python
{
    "name": "request_code_review",
    "description": (
        "Request an automated code review for a pull request. "
        "Calls the code-review-agent service and returns a structured report with "
        "blocking issues, suggestions, and a pass/fail verdict. "
        "Use after a worker opens a PR to gate finalization."
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
                "description": "Task ID to attach the review result to (optional).",
            },
        },
        "required": ["pr_url"],
    },
}
```

Implementation in `_exec_one_tool`:

```python
async def _exec_request_code_review(guild_id, inp):
    pr_url = inp["pr_url"]
    # Call code-review-agent via its A2A HTTP endpoint
    report = await _cra_client.review(pr_url)           # blocks, ~30-120 s
    summary = _summarise_review(report, max_chars=6000)  # fit within tool result cap
    if inp.get("task_id"):
        await _persist_review_artifact(inp["task_id"], report)
    return summary
```

### Feedback loop (P3 full design)

```
Worker
  │  opens PR via GitHub API
  │  sends task-complete → backend
  │
Backend
  │  triggers run_foreman_ai()
  │  Foreman calls request_code_review(pr_url)
  │    └─ code-review-agent returns report
  │
  ├─ if report.verdict == "pass":
  │    Foreman calls finalize_task(task_id)
  │
  └─ if report.verdict == "fail":
       Foreman calls send_followup(task_id,
         f"The automated review found {n} issues:\n{issues}\nPlease fix and push.")
         │
         └─ Worker re-runs Claude on same worktree/branch
              └─ Pushes fix commits → new task-complete
                   └─ Loop repeats (up to MAX_TOOL_RESULT_CHARS rounds)
```

### Sequence diagram

```
User        Foreman      Worker      GitHub     code-review-agent
 │            │            │           │               │
 │ "Fix bug   │            │           │               │
 │  in #38"   │            │           │               │
 │──────────► │            │           │               │
 │            │ create_task│           │               │
 │            │ assign_task│           │               │
 │            │───────────►│           │               │
 │            │            │ git worktree + claude     │
 │            │            │───────────────────────►   │
 │            │            │ push branch               │
 │            │            │──────────►│               │
 │            │            │ open PR   │               │
 │            │            │──────────►│               │
 │            │            │ task-complete             │
 │            │◄───────────│           │               │
 │            │ request_code_review(pr_url)            │
 │            │────────────────────────────────────────►
 │            │            │           │  SSE stream   │
 │            │◄────────────────────────────────────── │
 │            │ [verdict: fail, 2 blocking issues]     │
 │            │ send_followup(task_id, "Fix: …")       │
 │            │───────────►│           │               │
 │            │            │ (re-runs claude, pushes)  │
 │            │            │ task-followup-done        │
 │            │◄───────────│           │               │
 │            │ request_code_review(pr_url)            │
 │            │────────────────────────────────────────►
 │            │◄────────────────────────────────────── │
 │            │ [verdict: pass]        │               │
 │            │ finalize_task(task_id) │               │
```

### Auth and secrets required

| Secret | Where stored | Notes |
|---|---|---|
| `CRA_SERVICE_URL` | Backend env / `.env` | HTTPS endpoint of code-review-agent harness |
| `CRA_DNSID_AGENT_CONFIG` | Backend env | Path to DNSid identity file for backend-as-caller |
| GitHub token | Already in `github_tokens` table | Needed by code-review-agent worker subprocess |
| `crv-mcp` config | Worker `~/.claude.json` or `--mcp-config` | For P2; points to local `crv-mcp` binary |

---

## D. Other High-Value Tool Extensions

The same patterns apply to a range of other integrations. Brief notes on each:

### Static analysis / linting (`run_static_analysis`)
- **Tool:** `ruff`, `eslint`, `mypy`, or `semgrep` as a subprocess
- **Pattern:** P2 (worker runs linter before pushing) or P1 (Foreman calls a lint-runner service)
- **Output:** Line-level findings → Foreman can include them in `send_followup` instructions
- **Complexity:** Low — no external service needed, just a worker config change

### Security scanning (`request_security_scan`)
- **Tool:** `bandit`, `safety`, `trivy`, `semgrep` with security rulesets
- **Pattern:** P3 sidecar is ideal — scan runs on every pushed branch, blocks merging if CVEs found
- **Output:** SARIF or JSON findings → parsed into Foreman tool result
- **Note:** SARIF can be posted to GitHub Code Scanning API, making findings visible natively in PRs

### Dependency update automation (`create_dependency_update_task`)
- **Tool:** `dependabot` (already configured), `pip-audit`, `npm audit`
- **Pattern:** P1 — Foreman adds a new tool that queries GitHub's dependency graph API and creates tasks for out-of-date packages
- **Complexity:** Low — GitHub REST API already wired; just a new Foreman tool

### Test generation (`generate_tests`)
- **Tool:** Worker runs `claude` with a "write tests for this PR" prompt, or uses a specialised MCP tool
- **Pattern:** P2 — add a `generate_tests` task phase; worker's Claude creates test files and pushes them to the same branch
- **Complexity:** Low — pure Foreman orchestration, no external service

### Performance profiling (`profile_pr`)
- **Tool:** `py-spy`, `clinic.js`, or a benchmark runner
- **Pattern:** P3 — CI-triggered profile run, results posted as PR comment, Foreman notified
- **Complexity:** Medium — needs a runner environment with the app running

### Documentation generation (`update_docs`)
- **Tool:** Claude Code worker run in "docs" mode; or MCP tool calling a docs service
- **Pattern:** P2 — worker task phase after code changes
- **Complexity:** Low

---

## E. Recommended Roadmap

Items are ordered by value/effort ratio. Each is scoped to fit as a single worker task in pioneer-square itself.

### Phase 1 — Foundation (zero new services)

| # | Item | Pattern | Effort |
|---|---|---|---|
| 1 | **Worker MCP config support**: add `--mcp-config` flag to `claude_runner.py`; worker reads `mcp_config_path` from `pioneer-worker.toml` | P2 plumbing | S |
| 2 | **`run_static_analysis` Foreman tool**: after `task-complete`, Foreman auto-runs `ruff check` / `eslint` on the PR diff via GitHub API and includes findings in follow-up prompt | P1 (no service) | S |
| 3 | **Review artifact schema**: add `review_artifacts` table (task_id, type, payload JSON, created_at) and a `get_review_artifacts(task_id)` Foreman tool | DB prep | S |
| 4 | **Foreman `request_code_review` stub**: add the tool schema + `_exec_one_tool` handler that makes a plain HTTP POST to a configurable `CRA_SERVICE_URL`; returns mock report if URL is unset | P1 scaffold | S |

### Phase 2 — code-review-agent integration (P2, worker-side)

| # | Item | Pattern | Effort |
|---|---|---|---|
| 5 | **Deploy code-review-agent harness**: Dockerfile + compose service for the CRA harness; expose via internal DNS | P3 infra | M |
| 6 | **Worker crv-mcp onboarding**: worker install script runs `crv install` (launchd/systemd) and writes `~/.claude.json` MCP entry | P2 worker | S |
| 7 | **Foreman "review" task phase**: Foreman creates a `phase: review` task after a worker pushes a PR; worker uses `crv-mcp` tools to review and post comments | P2 orchestration | M |
| 8 | **Review-gated finalization**: Foreman delays `finalize_task` until CRA returns `verdict: pass`; loops `send_followup` on failures (cap: 3 rounds) | P2/P1 logic | M |

### Phase 3 — sidecar / webhook automation (P3)

| # | Item | Pattern | Effort |
|---|---|---|---|
| 9 | **GitHub webhook receiver**: `POST /webhooks/github/{guild_id}` route; validates HMAC signature; handles `pull_request.opened` and `pull_request_review` events | P3 backend | M |
| 10 | **Auto-review on PR open**: webhook handler calls CRA harness, stores artifact, injects `[github-event]` Foreman message | P3 full | M |
| 11 | **Foreman state preamble: pending reviews**: `build_state_preamble()` includes open review requests from `review_artifacts` table so Foreman is always aware of blockers | P3 UX | S |
| 12 | **Security scan integration**: wire `bandit`/`trivy` into the same review artifact pipeline; Foreman blocks finalization on severity ≥ HIGH findings | P3 extension | M |

### Estimated sizing

- **S** = ~2–4 hours of worker time (single focused PR)
- **M** = ~1 day of worker time (may span multiple PRs / follow-ups)

Phases 1 items (#1–4) are entirely internal and carry no external dependencies — they can be done in any order and immediately improve the Foreman's ability to reason about code quality. Phase 2 items depend on the CRA harness being deployed. Phase 3 items depend on Phase 2 being stable.

---

## Appendix: Key files for implementors

| File | Relevance |
|---|---|
| `backend/foreman/tools.py` | Add new tool dicts to `FOREMAN_TOOLS`; add handler to `_exec_one_tool` |
| `backend/foreman/runner.py` | `MAX_TOOL_RESULT_CHARS`, `MAX_FOREMAN_ROUNDS` — tune for review latency |
| `backend/foreman/prompt.py` | Update `FOREMAN_SYSTEM` to describe review tools; update `build_state_preamble` to include pending reviews |
| `worker/pioneer_worker/claude_runner.py` | Add `--mcp-config` flag to `run_claude_auto()` |
| `worker/pioneer_worker/worker.py` | Pass `mcp_config_path` from worker config to `run_claude_auto` |
| `backend/models.py` | Add `ReviewArtifact` ORM model |
| `backend/alembic/versions/` | New migration for `review_artifacts` table |
| `backend/main.py` | New webhook route (`/webhooks/github/{guild_id}`) |
