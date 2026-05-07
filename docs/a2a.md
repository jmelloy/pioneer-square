# A2A (Agent-to-Agent) Wiring

Pioneer Square's Foreman can call remote A2A-compatible agents using the
`call_agent` tool.  This document explains the protocol, the architecture,
and how to add new agents.

## Architecture

```
Browser
  │ (chat message)
  ▼
Backend ─── Foreman AI (Claude) ─── call_agent tool ──► HTTP A2A agents
                                │                        (e.g. agent.meyers.life)
                                └── dnsid tool ──────► dnsid-sdk CLI
                                                         (resolve / sign / verify)
```

### Discovery flow

```
Foreman                     Remote Agent
  │                               │
  │  GET /.well-known/agent.json  │
  │ ─────────────────────────────►│
  │        AgentCard (JSON)        │
  │ ◄─────────────────────────────│
  │                               │
  │  POST /a2a  (tasks/send)      │
  │  { skill_id, message }        │
  │ ─────────────────────────────►│
  │         result (JSON)         │
  │ ◄─────────────────────────────│
```

## Foreman tools

### `call_agent` — HTTP A2A agents

The `call_agent` tool calls any HTTP-based A2A agent.

| Parameter   | Type   | Required | Description |
|-------------|--------|----------|-------------|
| `agent_url` | string | yes      | Base URL of the target agent |
| `skill`     | string | yes      | Skill id to invoke |
| `params`    | object | no       | Skill-specific parameters (JSON object) |

How it works: fetches `{agent_url}/.well-known/agent.json`, verifies the skill
exists, then POSTs a JSON-RPC `tasks/send` to `{agent_url}/a2a`.

### `dnsid` — DNSid CLI operations

The `dnsid` tool shells out to the `dnsid-sdk` binary
(configured via `DNSID_SDK_BIN`, default `~/dnsid-go/bin/dnsid-sdk`).

| Command   | Required params          | Optional params   | Description |
|-----------|--------------------------|-------------------|-------------|
| `resolve` | `fqdn`                   | —                 | Look up an FQDN's `_dnsid` TXT record and JWKS |
| `sign`    | `claims` (object)        | —                 | Sign a JWT with the agent's Ed25519 identity (`DNSID_AGENT_CONFIG`) |
| `verify`  | `jwt`, `expected_aud`    | `expected_nonce`  | Verify a JWT against its DNSid record |

All three return the CLI's JSON output (`{"ok": true, ...}`).

Example Foreman prompts:

> "Use dnsid to resolve the identity record for example.com."
```json
{"command": "resolve", "fqdn": "example.com"}
```

> "Use dnsid to verify this token against aud=pioneer-square.melloy.life."
```json
{"command": "verify", "jwt": "<token>", "expected_aud": "pioneer-square.melloy.life"}
```

## Known agents

### code-review-agent (`https://agent.meyers.life`)

Automated GitHub PR code review via the `review_pr` Foreman tool (MCP transport).
The Foreman also exposes the `review_pr` tool which is purpose-built for this
agent; `call_agent` is an alternative generic path.

### dnsid-sdk (`Identity-Digital/dnsid-go`)

A CLI binary for DNS identity operations — **not** an HTTP server.
The Foreman calls it via the `dnsid` tool rather than `call_agent`.

```
dnsid resolve <fqdn>                          # look up _dnsid record + JWKS
dnsid sign [--config path]                    # sign JWT (reads claims JSON on stdin)
dnsid verify --jwt <tok> --expected-aud <aud> # verify JWT via DNS trust chain
```

Binary path: `~/dnsid-go/bin/dnsid-sdk` (override with `DNSID_SDK_BIN`).
Signing identity: JSON config file at `DNSID_AGENT_CONFIG` pointing to an Ed25519 PKCS#8 PEM key.

## Adding a new agent

1. Ensure the agent serves `GET /.well-known/agent.json` with a valid AgentCard.
2. Ensure the agent accepts `POST /a2a` with JSON-RPC `tasks/send` payloads.
3. Tell the Foreman the agent URL and skill id — no code changes required.

## Running the live integration test

```bash
# Test code-review-agent + dnsid-sdk CLI:
python scripts/test_a2a_live.py

# Override the binary path:
DNSID_SDK_BIN=/custom/path/dnsid-sdk python scripts/test_a2a_live.py

# Enable sign/verify checks (requires a configured agent identity):
DNSID_AGENT_CONFIG=/path/to/config.json python scripts/test_a2a_live.py
```

The script prints `[PASS]`, `[FAIL]`, or `[SKIP]` per check and exits 0 only
if all reachable checks pass. sign/verify are skipped when `DNSID_AGENT_CONFIG`
is not set.

## AgentCard format

Pioneer Square itself serves an AgentCard at
`GET /.well-known/agent.json` (see `backend/routes/wellknown.py`).  The card
is guild-specific: the guild's workers become skills in the card.

```json
{
  "name": "My Guild",
  "description": "...",
  "url": "https://g-abc123.pioneer-square.melloy.life",
  "version": "1.0",
  "capabilities": {"streaming": true},
  "skills": [
    {"id": "w-xyz", "name": "Python worker", "description": "..."}
  ]
}
```
