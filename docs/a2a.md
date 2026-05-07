# A2A (Agent-to-Agent) Wiring

Pioneer Square's Foreman can call remote A2A-compatible agents using the
`call_agent` tool.  This document explains the protocol, the architecture,
and how to add new agents.

## Architecture

```
Browser
  │ (chat message)
  ▼
Backend ─── Foreman AI (Claude) ─── call_agent tool
                                         │
                    ┌────────────────────┤
                    │                    │
                    ▼                    ▼
        code-review-agent         dnsid-go agent
        (agent.meyers.life)       (localhost:8080)
        /.well-known/agent.json   /.well-known/agent.json
        POST /a2a                 POST /a2a
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

## `call_agent` Foreman tool

The `call_agent` tool is the generic A2A entrypoint for the Foreman.

| Parameter   | Type   | Required | Description |
|-------------|--------|----------|-------------|
| `agent_url` | string | yes      | Base URL of the remote agent |
| `skill`     | string | yes      | Skill id to invoke |
| `params`    | object | no       | Skill-specific parameters (JSON object) |

### How it works

1. Fetches `{agent_url}/.well-known/agent.json` (the AgentCard).
2. Verifies the requested `skill` id is listed in the card's `skills` array
   (skipped if the card advertises no skills).
3. POSTs a JSON-RPC `tasks/send` message to `{agent_url}/a2a`:
   ```json
   {
     "jsonrpc": "2.0",
     "method": "tasks/send",
     "params": {
       "skill_id": "<skill>",
       "message": {
         "parts": [{"type": "text", "text": "<params as JSON>"}]
       }
     },
     "id": 1
   }
   ```
4. Returns the agent's result as JSON to the Foreman.

### Example Foreman prompt

> "Use call_agent to ask the dnsid-go agent at http://localhost:8080 to run
> verify_identity_record for domain example.com."

The Foreman will call:
```json
{
  "agent_url": "http://localhost:8080",
  "skill": "verify_identity_record",
  "params": {"domain": "example.com"}
}
```

## Known agents

### code-review-agent (`https://agent.meyers.life`)

Automated GitHub PR code review via the `review_pr` Foreman tool (MCP transport).
The Foreman also exposes the `review_pr` tool which is purpose-built for this
agent; `call_agent` is an alternative generic path.

### dnsid-go (`Identity-Digital/dnsid-go`)

DNS identity record operations.  When the dnsid-go A2A server is running it
advertises two skills:

| Skill id | Description |
|---|---|
| `verify_identity_record` | Verify a DNSID TXT record for a domain |
| `lookup_dnsid` | Resolve and return raw DNSID records for a domain |

Start it with:
```bash
dnsid-go serve --port 8080
```

## Adding a new agent

1. Ensure the agent serves `GET /.well-known/agent.json` with a valid AgentCard.
2. Ensure the agent accepts `POST /a2a` with JSON-RPC `tasks/send` payloads.
3. Tell the Foreman the agent URL and skill id — no code changes required.

## Running the live integration test

```bash
# Test both code-review-agent (always) and dnsid-go (if reachable):
python scripts/test_a2a_live.py

# Override the dnsid-go URL:
DNSID_AGENT_URL=http://myhost:9090 python scripts/test_a2a_live.py
```

The script prints `[PASS]`, `[FAIL]`, or `[SKIP]` per check and exits 0 only
if all reachable agents pass.

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
