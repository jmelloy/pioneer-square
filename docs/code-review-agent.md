# Automated PR Reviews via code-review-agent

Pioneer Square's Foreman AI can request automated code reviews from the
external [code-review-agent](https://github.com/Identity-Digital/code-review-agent),
an A2A-protocol agent. The Foreman calls its `review_pr` tool to get a
structured review posted directly to a GitHub pull request.

## Architecture

```
Foreman AI ──► A2AClient ──HTTPS (JSON-RPC/SSE)──► code-review-agent ──► Claude worker
        (backend/foreman/a2a_client.py)         (agent.meyers.life)   (does the review)
```

`review_pr` uses a dedicated `A2AClient` (not the generic `call_agent` tool)
to fetch the agent's card, negotiate DNSid auth if the card requires it, and
dispatch the `pr-review` skill over the `message/stream` JSON-RPC method.

## Setup

By default the Foreman targets `https://agent.meyers.life`. To point at a
different deployment, set in `backend/.env` (or the root `.env` used by
`docker-compose`):

```env
REVIEWER_AGENT_URL=https://your-code-review-agent.example.com
```

No other configuration is required — the guild's Ed25519 identity (the same
key served at `/.well-known/jwks.json`) is used automatically if the agent's
card declares DNSid auth. If a guild has no key yet, one is generated lazily
on first request (see `backend/routes/wellknown.py`), so there's nothing to
provision ahead of time.

## How it works

When the Foreman calls `review_pr(pr_url)`:

1. `A2AClient` fetches the agent's `/.well-known/agent.json` card and, if it
   declares DNSid auth, runs a challenge-response using the guild's key.
2. It POSTs a `message/stream` JSON-RPC request for the `pr-review` skill with
   the PR URL, and collects the resulting SSE artifact/status events.
3. The Foreman extracts the verdict (`APPROVE` / `REQUEST_CHANGES` / `COMMENT`)
   and review body from the result, then posts it to GitHub via
   `POST /repos/{owner}/{repo}/pulls/{number}/reviews` using the guild's OAuth
   token.

If the external agent is unavailable, `review_pr_internal` performs a
self-contained review (Foreman AI directly analyses the diff) as a fallback.

## Feedback loop

When code-review-agent posts a review requesting changes, GitHub emits a
`pull_request_review` webhook (`action: "submitted"`,
`review.state: "changes_requested"`). Pioneer Square routes this to the
Foreman AI, which can call `send_followup` to dispatch a worker to address the
comments — closing the loop automatically, with no extra configuration.

## Env vars reference

| Variable | Required | Description |
|---|---|---|
| `REVIEWER_AGENT_URL` | no | Base URL of the code-review-agent; defaults to `https://agent.meyers.life` |

## Timeouts

`A2AClient` uses a 180 s timeout for both the agent-card fetch and the
review request (reviews of large PRs can take a couple of minutes). This is
currently a fixed constant in `backend/foreman/a2a_client.py`, not
configurable via env var.
