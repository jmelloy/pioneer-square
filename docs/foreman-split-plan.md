# Foreman API Proxy

**Status:** Current architecture. The backend owns the Foreman loop; the optional
standalone process is a thin LLM API proxy.

---

## Architecture

The embedded Foreman in `backend/foreman/` handles triggers, state snapshots,
history, tool execution, broadcasts, child-context locking, and periodic polling.
When a standalone proxy is connected, only the low-level LLM API call crosses the
WebSocket boundary.

```
browser / worker / webhook
  │
  ▼
backend embedded Foreman
  │
  ├─ direct Anthropic/Bedrock call when no proxy is connected
  │
  └─ WS: foreman-api-request ──► standalone proxy (`pioneer foreman`)
      ◄─ WS: foreman-api-response ── provider call
                                      Anthropic / Bedrock / OpenAI-compatible
```

The proxy does not read the database, persist history, execute tools, route
triggers, or broadcast to frontend/worker sockets. That keeps all product logic
in the backend and lets operators run only the provider call across a
network/firewall boundary or against a local endpoint such as Ollama.

Periodic task-health polling is scheduled entirely by the backend
(`backend/foreman/runner.py`'s `_poll_loop`) for every guild. Poll ticks call
`ws_handlers._trigger_foreman()`, which always dispatches into the embedded
runner; the runner delegates API calls to a connected proxy only when needed.

---

## Packages and files

### Standalone proxy — `foreman/`

```
foreman/
  pioneer_foreman/
    cli.py           – argument parser + entry point
    config.py        – provider/model/base-url config from TOML/env/CLI
    foreman.py       – WebSocket lifecycle and request/response handling
    runner.py        – execute one LLM API request and normalize the response
    logging_config.py
```

### Backend Foreman — `backend/foreman/`

```
backend/foreman/
  runner.py          – Foreman conversation loop; uses proxy at the LLM boundary
  proxy.py           – pending foreman-api-request registry
  tools.py           – canonical backend-side tool execution
  prompt.py          – system prompt and prompt builders
  tools_schema.py    – Foreman tool JSON schema
  message_utils.py   – Anthropic message/history helpers
  llm.py             – provider/model selection and Anthropic/Bedrock client factory
```

Shared Foreman helpers live in the backend Foreman package with the runner that owns them.

---

## WebSocket protocol

**Backend → proxy:**

| Type | Payload | Purpose |
|------|---------|---------|
| `foreman-api-request` | `{ requestId, guildId, model, maxTokens, system, messages, tools, toolChoice? }` | Execute one LLM API request |
| `foreman-registered` | `{ guildId, agentId? }` | Acknowledge active proxy registration |
| `foreman-evicted` | `{ guildId, reason }` | Another proxy registered; this one should reconnect later |

**Proxy → backend:**

| Type | Payload | Purpose |
|------|---------|---------|
| `join` | `{ agentType: "foreman", agentName: "Foreman API Proxy", external: true }` | Register as the active proxy |
| `foreman-api-response` | `{ requestId, guildId, ok, response?, error?, apiRequestId?, provider?, model? }` | Return one normalized LLM response or error |
| `foreman-disconnect` | `{ guildId? }` | Graceful shutdown notice |

The `response` payload is Anthropic Messages shaped even when the proxy calls an
OpenAI-compatible endpoint. This lets the backend keep one message/history/tool
pipeline.

---

## Configuration

**TOML (`pioneer-foreman.toml`):**

```toml
backend_url = "ws://backend:8000"
guild_id = "abc123"
log_level = "INFO"

[llm]
provider = "openai"                 # "anthropic" | "bedrock" | "openai"
model = "llama3.1"
base_url = "http://localhost:11434/v1"
api_key = "ollama"                  # optional for local unauthenticated servers
```

`[claude]` is still accepted as a backward-compatible alias for `[llm]`, but new
configs should use `[llm]`.

**Environment variables:**

| Variable | Purpose |
|----------|---------|
| `PIONEER_BACKEND_URL` or `BACKEND_WS_URL` | Backend WebSocket base URL |
| `PIONEER_GUILD_ID` or `GUILD_ID` | Guild to serve |
| `FOREMAN_PROVIDER` | `anthropic`, `bedrock`, or `openai` |
| `FOREMAN_MODEL` | Provider model ID |
| `FOREMAN_BASE_URL` or `OPENAI_BASE_URL` | OpenAI-compatible base URL |
| `FOREMAN_API_TIMEOUT` | API request timeout in seconds |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` | Anthropic credentials |
| `OPENAI_API_KEY` | OpenAI-compatible endpoint key |
| `FOREMAN_BEDROCK_MODEL`, `AWS_DEFAULT_REGION`, `AWS_PROFILE`, `AWS_*` | Bedrock config |
| `LOG_LEVEL` | Log verbosity |

**CLI flags:**

```bash
pioneer foreman [--config PATH] [--backend-url URL] [--guild-id ID]
                [--provider anthropic|bedrock|openai]
                [--model MODEL] [--bedrock-model MODEL_OR_ARN]
                [--base-url URL] [--api-key KEY] [--log-level LEVEL]
```

---

## Design decisions

### Backend-owned Foreman loop

`foreman-trigger` was removed. Triggers no longer cross the process boundary.
`ws_handlers._trigger_foreman()` always spawns the embedded
`backend.foreman.runner.run_foreman_ai()` path.

### API proxy only

The standalone process no longer has a REST client, JWT helper, tool executor,
message-history loader, task locks, or child-context supervisor. It executes one
provider call per `foreman-api-request`.

### Provider normalization

Anthropic and Bedrock are called with the Anthropic SDK. OpenAI-compatible
providers are called with `POST /chat/completions`; tools and messages are
translated at the proxy boundary, and responses are normalized back to
Anthropic content blocks for the backend.

### Single-proxy enforcement

The backend tracks one active external proxy per guild. A second `join` evicts
the first via `foreman-evicted`. Any pending API requests owned by a disconnecting
proxy socket fail immediately.
