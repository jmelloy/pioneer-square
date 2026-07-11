# A2A (Agent-to-Agent) Wiring

Pioneer Square's Foreman can call remote A2A-compatible agents using the
`call_agent` tool, and DNS identity operations via the `dnsid` tool.

## Architecture

```
Browser
  │ (chat message)
  ▼
Backend ─── Foreman AI (Claude) ─── call_agent tool ──► HTTP A2A agents
                                │                        (e.g. agent.meyers.life)
                                └── dnsid tool ──────► dnsid-py library
                                                         (resolve / sign / verify)
```

### Discovery flow

```
Foreman                     Remote Agent
  │  GET /.well-known/agent.json  │
  │ ─────────────────────────────►│
  │        AgentCard (JSON)        │
  │ ◄─────────────────────────────│
  │  POST /jsonrpc                │
  │  (tasks/send or message/stream)│
  │ ─────────────────────────────►│
  │         result (JSON)         │
  │ ◄─────────────────────────────│
```

## Foreman tools

### `call_agent` — HTTP A2A agents

| Parameter   | Type   | Required | Description |
|-------------|--------|----------|-------------|
| `agent_url` | string | yes      | Base URL of the target agent |
| `skill`     | string | yes      | Skill id to invoke |
| `params`    | object | no       | Skill-specific parameters (JSON object) |

Fetches `{agent_url}/.well-known/agent.json`, verifies the skill exists, then
POSTs a JSON-RPC `tasks/send` to `{agent_url}/jsonrpc`.

### `dnsid` — DNSid operations

The `dnsid` tool calls the `dnsid-py` Python library directly.

| Command   | Required params          | Optional params   | Description |
|-----------|--------------------------|-------------------|-------------|
| `resolve` | `fqdn`                   | —                 | Look up an FQDN's `_dnsid` TXT record and JWKS |
| `sign`    | `claims` (object)        | —                 | Sign a JWT using the calling guild's Ed25519 key (stored in the DB, same key served at `/.well-known/jwks.json`) |
| `verify`  | `jwt`, `expected_aud`    | `expected_nonce`  | Verify a JWT against its DNSid record |

All three return JSON (`{"ok": true, ...}`); `sign` fails if the guild has no
signing key yet.

## Known agents

### code-review-agent

Automated GitHub PR code review via the `review_pr` Foreman tool, which uses a
dedicated A2A client (`backend/foreman/a2a_client.py`) to call the agent's
`pr-review` skill directly — not the generic `call_agent` path. Defaults to
`https://agent.meyers.life`, overridable per-deployment via `REVIEWER_AGENT_URL`.
See `docs/code-review-agent.md` for details.

### dnsid-py

A Python library for DNS identity operations (`resolve` / `sign` / `verify`,
per the table above). The Foreman calls it via the `dnsid` tool rather than
`call_agent`. Install: `pip install dnspython cryptography` (listed in
`cli/pyproject.toml`).

## Adding a new agent

1. Ensure the agent serves `GET /.well-known/agent.json` with a valid AgentCard.
2. Ensure the agent accepts `POST /jsonrpc` with JSON-RPC `tasks/send` payloads.
3. Tell the Foreman the agent URL and skill id — no code changes required.

## Running the live integration test

```bash
# Test code-review-agent + dnsid-py library:
python scripts/test_a2a_live.py

# Enable sign/verify checks (requires an Ed25519 private key PEM):
DNSID_PRIVATE_KEY_PEM="$(cat path/to/key.pem)" python scripts/test_a2a_live.py
```

Prints `[PASS]`/`[FAIL]`/`[SKIP]` per check and exits 0 only if all reachable
checks pass; sign/verify are skipped without `DNSID_PRIVATE_KEY_PEM`.

## AgentCard format

Pioneer Square itself serves an AgentCard at `GET /.well-known/agent.json`
(see `backend/routes/wellknown.py`), per the
[A2A AgentCard spec](https://google.github.io/A2A/specification/#agent-card).
The card is guild-specific: a `foreman` skill is always included, plus one
skill per online worker. Simplified example:

```json
{
  "name": "My Guild",
  "description": "...",
  "url": "https://g-abc123.pioneer-square.melloy.life",
  "version": "1.0",
  "capabilities": {"streaming": true},
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [
    {"id": "foreman", "name": "Foreman", "description": "..."},
    {"id": "w-xyz", "name": "Python worker", "description": "..."}
  ]
}
```
