# MCP endpoint

`POST /mcp` is a stateless MCP server (JSON-RPC 2.0 over HTTP, streamable-HTTP transport in
JSON-response mode). It implements `initialize`, `ping`, `tools/list` and `tools/call`; it does not
use the `mcp` Python SDK. Code: `backend/routes/mcp.py`.

## Auth tiers

| Tier | Credentials | Tools |
|------|-------------|-------|
| Normal | `Authorization: Bearer <member login token \| worker token \| foreman JWT>`, authorized against the `guild_id` tool argument | `list_tasks`, `get_task` |
| Debug | Normal Bearer token **plus** `X-Debug-Token: $DEBUG_TOKEN` and an `X-Guild-Id` header (to attribute the call to a principal) | `debug_query`, `cloudflare_logs` |

Debug tools are hidden from `tools/list` and rejected (403) unless the debug token is valid; the
debug token alone is never enough (401). If `DEBUG_TOKEN` is unset, debug tools are disabled.

## Debug tools

- `debug_query {sql}` — read-only SELECT, same guards as `/debug/query` (single statement,
  allow-listed operational tables, `SET TRANSACTION READ ONLY`, 30s timeout, 500-row cap).
- `cloudflare_logs {script?, search?, minutes?, limit?}` — queries the Cloudflare Workers
  observability API. Needs `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`. The request shape
  has not been exercised against the live API; verify before relying on it.

## Audit and rate limiting

Every debug call logs principal, tool and arguments to the `mcp.audit` logger; denials and
rate-limit hits log at WARNING. Debug calls are limited to 30 per 60s per principal (in-memory,
per process, so multi-instance deployments multiply the limit).

## Example

```bash
curl -s localhost:8000/mcp -H "Authorization: Bearer $TOKEN" -H "X-Debug-Token: $DEBUG_TOKEN" \
  -H "X-Guild-Id: abc123" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"debug_query","arguments":{"sql":"SELECT id,state FROM tasks"}}}'
```
