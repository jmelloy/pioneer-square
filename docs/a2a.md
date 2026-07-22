# A2A (Agent-to-Agent) Wiring

Pioneer Square can act as a DNSid-authenticated A2A peer. A configured guild
accepts signed `foreman` and `pr-review` requests and the Foreman can send
RFC 9421-signed A2A requests to other DNSid principals.

## SDK installation

The integration targets `dnsid-py` 0.7.0. That distribution is not currently
available from PyPI, so do not add a non-resolving `dnsid==0.7.0` requirement to
the unified CLI yet. For development from the sibling checkout:

```bash
uv pip install -e ../dnsid-py
```

Once 0.7.0 is published to the package index used by Pioneer builds, add the
exact pin to `cli/pyproject.toml` and regenerate `cli/uv.lock`.

## Guild identity configuration

The first integration intentionally supports one CLI-provisioned identity and
one Pioneer guild per backend process:

```bash
export DNSID_CONFIG_DIR="$HOME/.dnsid-testnet/agents/pioneer.dev.dnsid.test"
export PIONEER_DNSID_GUILD="pioneer"
export PIONEER_DNSID_PUBLIC_URL="https://pioneer.dev.dnsid.test"
export PIONEER_DNSID_ALLOWLIST='{"allow":["barnji.dev.dnsid.test"]}'
```

`PIONEER_DNSID_ALLOWLIST_FILE` may point to the same JSON object instead. The
backend loads the operational key from the CLI identity directory, registers a
C2SP reader when the identity uses `c2sp-tlog`, and verifies its own identity
before completing startup. `PIONEER_DNSID_PUBLIC_URL` is also the canonical
origin used when reconstructing signed request target URIs; forwarded host and
protocol headers are not trusted. When started under `dnsid testnet env` or
`dnsid testnet run`, protocol and transport configuration comes from the
injected `DNSID_*` variables while the private key still comes from
`DNSID_CONFIG_DIR`; Pioneer refuses to start if those identities do not match.

## Architecture

```
Browser
  │ (chat message)
  ▼
DNSid peer ── signed A2A POST ──► Backend ──► Foreman ──► internal worker
                                      │
                                      └── verified sender + replay audit in DB

Backend ── signed A2A POST ──► DNSid peer
```

### Discovery flow

```
Foreman                     Remote Agent
  │ GET /.well-known/agent-card.json │
  │ ─────────────────────────────►│
  │        AgentCard (JSON)        │
  │ ◄─────────────────────────────│
  │  signed POST /                │
  │  message/send                 │
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

This is the explicit legacy compatibility path for pre-1.0 agents. The
dedicated `A2AClient` uses DNSid verification, `message/send`, and RFC 9421
signatures whenever `DNSID_CONFIG_DIR` is configured.

### `dnsid` — verification-only DNSid operations

The `dnsid` tool calls the `dnsid-py` Python library directly.

| Command   | Required params          | Optional params   | Description |
|-----------|--------------------------|-------------------|-------------|
| `resolve` | `fqdn`                   | —                 | Fully verify DNS, key roles, status, and lifecycle evidence |
| `verify`  | `jwt`, `expected_aud`    | `expected_nonce`  | Verify a JWT and its issuer through `JoseProfile` |

Arbitrary signing is deliberately not an LLM tool. Application code constructs
and signs the narrow A2A HTTP request profile.

## Known agents

### code-review-agent

Automated GitHub PR code review via the `review_pr` Foreman tool, which uses a
dedicated A2A client (`backend/foreman/a2a_client.py`) to call the agent's
`pr-review` skill directly — not the generic `call_agent` path. Defaults to
`https://agent.meyers.life`, overridable per-deployment via `REVIEWER_AGENT_URL`.
See `docs/code-review-agent.md` for details.

## Inbound receiver

Both `POST /` (A2A 1.0) and `POST /jsonrpc` (compatibility) require the shared
DNSid A2A extension and RFC 9421 signature tag. The signature must cover
`@method`, `@authority`, `@target-uri`, `content-type`, `content-digest`,
`a2a-version`, and `a2a-extensions`.

The receiver buffers at most 1 MiB, verifies through
`HttpSignatureProfile.verify_signed_http_request()`, applies the exact-FQDN
allowlist, then atomically stores the message and a unique signature replay
key. Failed authentication returns 401; a valid but disallowed DNSid returns
403. The persisted audit includes sender FQDN, governance identifier, DNSSEC
state, verification time, DNSid status, skill, and external message ID.

Only `foreman` and `pr-review` are advertised. Workers remain private runtime
identities and the Foreman retains assignment authority.

## Authorizing an agent to participate in the guild

Guild participation is closed by default. Each external agent must have an
ACTIVE DNSid identity and its normalized FQDN must appear in
`PIONEER_DNSID_ALLOWLIST` (or the allowlist file). For example, to authorize a
second participant without changing code:

```bash
export PIONEER_DNSID_ALLOWLIST='{
  "allow":["barnji.dev.dnsid.test","reviewer.dev.dnsid.test"]
}'
```

Restart Pioneer after changing the environment. The participant discovers
`/.well-known/agent-card.json`, sends a DNSid-signed `message/send` for the
advertised `foreman` or `pr-review` skill, and uses a separately signed
`tasks/get` request with the original A2A `messageId` to poll the result. A
valid identity that is not listed receives 403; a bad signature or replayed
submission receives 401.

The focused automated boundary test is:

```bash
docker compose up -d postgres-test
cd backend
python -m pytest tests/test_dnsid_a2a_receiver.py -q
```

It exercises a request signed by the real `dnsid-py` HTTP-signature profile,
signed task polling, exact-FQDN authorization, invalid signatures, and replay
rejection. The local testnet demo below is still required to prove two real
CLI-provisioned identities and live DNS/log resolution end to end.

## Adding a remote agent for Pioneer to call

1. Ensure the agent serves `GET /.well-known/agent.json` with a valid AgentCard.
2. Ensure the agent accepts DNSid-signed A2A 1.0 `message/send` requests.
3. Tell the Foreman the agent URL and skill id — no code changes required.

## Local testnet demo

```bash
dnsid testnet up
dnsid testnet agent ensure pioneer --upstream http://localhost:8000 -- \
  dnsid log issue --domain pioneer.dev.dnsid.test
dnsid testnet agent ensure barnji --upstream http://localhost:3001 -- \
  dnsid log issue --domain barnji.dev.dnsid.test

# Run Pioneer under the identity environment after exporting the Pioneer vars above.
dnsid testnet run pioneer --upstream http://localhost:8000 -- pioneer serve
```

Then send the `pr-review` message with the signed client from
`../dnsid-py/examples/a2a` or `../dnsid-ts/examples/a2a`. Both clients use the
same extension URI, signature tag, and covered components.

## AgentCard format

Pioneer Square serves AgentCards at the A2A 1.0
`GET /.well-known/agent-card.json` route and the legacy
`GET /.well-known/agent.json` route
(see `backend/routes/wellknown.py`), per the
[A2A AgentCard spec](https://google.github.io/A2A/specification/#agent-card).
The card is guild-specific and exposes only `foreman` and `pr-review`.

```json
{
  "name": "My Guild",
  "description": "...",
  "url": "https://g-abc123.pioneer-square.melloy.life",
  "version": "1.0",
  "capabilities": {"streaming": false},
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [
    {"id": "foreman", "name": "Foreman", "description": "..."},
    {"id": "pr-review", "name": "Pull request review", "description": "..."}
  ]
}
```
