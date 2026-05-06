# Automated PR Reviews via code-review-agent

Pioneer Square's Foreman AI can request automated code reviews through the
[code-review-agent](https://github.com/Identity-Digital/code-review-agent) MCP
server. When enabled, the Foreman can call `review_pr` to get a structured
review posted directly to a GitHub pull request.

## Architecture

```
Foreman AI ──► MCPClient ──stdio──► crv-mcp ──HTTPS──► crv harness ──► Claude worker
                                   (MCP server)        (HTTP service)   (does the review)
```

The integration involves two components from the code-review-agent repository:

- **crv harness** (`crv start`): long-running HTTP service that spawns Claude
  review workers. Runs on its own host/port.
- **crv-mcp** (`bin/crv-mcp`): stdio MCP server that the Foreman backend
  launches as a subprocess. It proxies JSON-RPC calls to the harness.

## Setup

### 1. Install code-review-agent

Follow the [code-review-agent README](https://github.com/Identity-Digital/code-review-agent)
to install and start the harness. You will need:
- A running `crv start` harness process
- The `crv-mcp` binary accessible in the backend container's `PATH` (or at an
  absolute path you configure)

### 2. Configure the backend

Add the following to your `backend/.env` (or the root `.env` used by
`docker-compose`):

```env
# Shell command to launch the crv-mcp stdio MCP server.
# Must be reachable from inside the backend container.
REVIEWER_MCP_CMD=/opt/code-review-agent/bin/crv-mcp

# Alternatively, point at an MCP-over-HTTP endpoint:
# REVIEWER_MCP_URL=http://crv-mcp:9000/mcp

# URL of the code-review-agent harness (the crv start process).
REVIEWER_AGENT_URL=https://crv.example.com
```

Only one of `REVIEWER_MCP_CMD` or `REVIEWER_MCP_URL` is required.
`REVIEWER_AGENT_URL` is always required (it is passed to the MCP server so it
knows which harness to call).

### 3. Restart the backend

```bash
docker compose restart backend
# or
uvicorn main:app --reload --port 8000
```

## How it works

When the Foreman calls `review_pr(pr_url)`:

1. **MCPClient** (`backend/foreman/mcp_client.py`) launches `crv-mcp` as a
   subprocess (or POSTs to `REVIEWER_MCP_URL`) and performs the MCP
   initialization handshake.
2. The Foreman calls the MCP tool `start_conversation` with:
   - `agent_url`: the harness URL from `REVIEWER_AGENT_URL`
   - `capability`: `"review_pr"`
   - `initial_text`: the GitHub PR URL
3. `crv-mcp` forwards the request to the harness via HTTPS, which spawns a
   Claude worker that fetches the PR diff and produces a structured JSON review
   report.
4. The report is returned to the MCPClient as a tool result. Pioneer Square
   extracts the verdict (`approved` / `changes-requested` / `comment`) and the
   Markdown review body.
5. The Foreman posts the review to GitHub via the Pull Request Reviews API
   (`POST /repos/{owner}/{repo}/pulls/{number}/reviews`) using the guild's
   OAuth token.

## Feedback loop

When the code-review-agent posts a `REQUEST_CHANGES` review, GitHub emits a
`pull_request_review` webhook event with `action: "submitted"` and
`review.state: "changes_requested"`. Pioneer Square receives this as a
`[github-event]` message, which triggers the Foreman AI. The Foreman
recognises the `changes_requested` state and calls `send_followup` to dispatch
the relevant worker to address the review comments — closing the loop
automatically.

This path is handled entirely by the existing Foreman webhook integration and
requires no additional configuration.

## Env vars reference

| Variable | Required | Description |
|---|---|---|
| `REVIEWER_MCP_CMD` | one of these two | Shell command to launch `crv-mcp` |
| `REVIEWER_MCP_URL` | one of these two | HTTP endpoint of an MCP-over-HTTP server |
| `REVIEWER_AGENT_URL` | yes | Base URL of the `crv start` harness |

## Timeouts

The `crv-mcp` MCP server blocks while the harness runs the review (default
timeout 120 s, set via `CRV_MCP_TURN_TIMEOUT_SECONDS`). Pioneer Square's
`MCPClient` uses a 180 s timeout, so the review has time to complete. Reviews
of large PRs (many files, high diff size) may need a longer `CRV_MCP_TURN_TIMEOUT_SECONDS`.
