# Pioneer Square Worker

Standalone worker agent for [Pioneer Square](../README.md). Connects to a backend
guild over WebSocket, listens for task assignments, runs
`claude --dangerously-skip-permissions` against a fresh git worktree, then
pushes the result and opens a GitHub PR.

The worker is a separate process from the backend so it can run on a different
machine — typically one with the cloned repos, a `claude` CLI install, and a
GitHub token.

## Install

The worker installs from the repo's unified CLI package (one install serves the
backend, foreman, and worker):

```bash
uv venv && source .venv/bin/activate
uv pip install -e cli
```

This provides the `pioneer` command; run a worker with `pioneer worker`. The
`pioneer-worker` console script is a backward-compatible alias used below for
brevity.

## Expected toolchain

A worker shells out to a coding agent (`claude`, `codex`, or `pi`), which runs
whatever the task needs. Every worker host should have the following
pre-installed and on `PATH`:

- **Core**: `git`, `curl`, `jq`, `make`, `unzip`, `openssh-client`, `ripgrep`, a
  C/C++ toolchain (`build-essential` on Debian), [`gh`](https://cli.github.com/)
- **Python 3.11+**: `python3`, `pip`, `venv`, [`ruff`](https://docs.astral.sh/ruff/),
  [`pytest`](https://docs.pytest.org/), [`uv`](https://docs.astral.sh/uv/), [`pipx`](https://pipx.pypa.io/)
- **Node.js 24+**: `node`, `npm`, `npx`, `corepack` enabled, plus the agent
  CLIs (`@anthropic-ai/claude-code`, `@openai/codex`, `@mariozechner/pi-coding-agent`)
- **Go 1.23+**: `go` on `PATH`, with `GOPATH` writable by the worker user

The `worker` target in the root `Dockerfile` currently provisions this set, so
`docker compose --profile worker up` needs no extra setup. Running the worker
directly on a host requires installing the equivalents yourself. Anything
outside this baseline (Rust, Java, Ruby, .NET, Terraform, etc.) is expected to
be installed on demand by the task itself.

## Configure

Copy the example config and edit it:

```bash
cp pioneer-worker.toml.example pioneer-worker.toml
$EDITOR pioneer-worker.toml
```

Required keys:

- `backend_url` — `http://host:port` (or `ws://...`); the worker derives the
  WebSocket URL automatically.
- `guild_id` — the 6-char guild id from the Pioneer Square UI.
- `[github] repos` — list of `owner/repo` strings the worker may operate on.
- `[github] org` — GitHub organisation name (e.g. `"jmelloy"`). When set, the
  worker accepts tasks targeting *any* repo under that org and clones repos
  lazily the first time a task arrives. Can be used alongside `repos` (union)
  or instead of it.

Optional:

- `worker_id` — pre-existing worker id from the backend. If omitted, the worker
  registers itself on first launch and writes the assigned id to
  `pioneer-worker.state.json` next to the config.
- `[github] token` — inline token, or `"env:VAR"` to read from the environment.
  Required to push and open PRs against private repos.

## Run

```bash
pioneer-worker                       # reads ./pioneer-worker.toml
pioneer-worker --config /path.toml   # explicit path
pioneer-worker --log-level DEBUG     # verbose
```

The worker stays running, reconnecting if the backend restarts.

## How it works

1. On startup, registers (or claims) a worker id with the backend.
2. Opens a WebSocket to `/ws/{guild_id}` and announces itself with `join`
   and `worker-register` messages.
3. Fetches any pending tasks via REST so it can resume work across restarts.
4. Listens for `task-assigned` messages whose `workerId` matches its own id.
5. For each task: clones the configured repos under `repos_dir`, creates a
   worktree under `work_dir`, runs `claude` in the worktree, then pushes the
   branch and opens a PR via the GitHub API.
6. Reports state changes (`working`, `done`, `failed`, branch, PR url) back
   to the backend via `task-update` WebSocket messages.

## Control API (drive a live worker without a frontend)

Enable the optional HTTP control API to inject tasks straight into the
worker's queue and inspect its state without driving the UI — the full
execution path (clone, worktree, claude/codex/pi, push, PR) still runs.

```bash
pioneer-worker --api-port 9200             # enable on 127.0.0.1:9200
pioneer-worker --api-port 9200 --api-host 0.0.0.0
```

Or in config / env:

```toml
[api]
port = 9200
host = "127.0.0.1"
```

```bash
PIONEER_API_PORT=9200 pioneer-worker
```

It is unauthenticated; keep it bound to localhost (the default) and use it for
local/dev only.

| Method | Path                  | Body                                                                                          | Description                          |
| ------ | --------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------ |
| GET    | `/` or `/status`      | —                                                                                             | Worker id, repos, agents, queue depth |
| GET    | `/agents`             | —                                                                                             | Agent slots with current state       |
| GET    | `/tasks`              | —                                                                                             | Known task ids + queue depth         |
| POST   | `/tasks`              | `{description, name?, tool?, phase?, repos?, issueNumber?, issueRepo?, id?, followupInstructions?, followupBranch?}` | Inject a task into the queue |
| POST   | `/control/shutdown`   | —                                                                                             | Graceful shutdown                    |

```bash
# Inject a task and watch the worker run it
curl -s localhost:9200/tasks \
  -H 'content-type: application/json' \
  -d '{"description": "Add a README badge", "tool": "claude", "repos": ["owner/repo"]}'
curl -s localhost:9200/ | jq .
```

## Multiple workers

Run more than one worker against the same guild by giving each its own
config directory (so the sidecar state file doesn't collide):

```bash
mkdir -p ~/.pioneer/builder ~/.pioneer/tester
pioneer-worker --config ~/.pioneer/builder/pioneer-worker.toml &
pioneer-worker --config ~/.pioneer/tester/pioneer-worker.toml  &
```

## Mock mode (for e2e tests)

```bash
pioneer-worker --mock --backend-url http://localhost:8000 --guild-id <id>
pioneer-worker --mock --mock-api-port 9100   # default
```

In mock mode the worker:

- still registers with the backend and joins the guild over WebSocket using
  the same `join` / `worker-register` / `agent-state` / `terminal-output` /
  `task-*` frames a real worker emits, so the frontend (and foreman) see a
  real-looking worker;
- skips the GitHub token fetch, the `claude` and `gh` auth checks, all
  cloning/worktree/PR work, and the claude/codex/pi subprocesses;
- exposes a small HTTP control API (default `127.0.0.1:9100`) so test code
  can drive agent state and task lifecycle deterministically.

### HTTP control API

| Method | Path                              | Body                                                                     | Effect                                                       |
| ------ | --------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------ |
| GET    | `/`                               | —                                                                        | Worker + slots + active tasks snapshot                       |
| GET    | `/agents`                         | —                                                                        | Just the slots array                                         |
| POST   | `/agents/{agentId}/state`         | `{state, activity?, taskId?}`                                            | Emit an `agent-state` frame                                  |
| POST   | `/agents/{agentId}/output`        | `{line, taskId?, detail?}`                                               | Emit a `terminal-output` frame                               |
| POST   | `/tasks/{taskId}/complete`        | `{branch?, prUrl?, stopReason?, lastText?, sessionId?}`                  | Send `task-complete` (or `task-followup-done`); slot → idle  |
| POST   | `/tasks/{taskId}/fail`            | `{stopReason?, lastMessage?}`                                            | Send `needs-input`; slot → error → idle                      |
| POST   | `/control/shutdown`               | —                                                                        | Graceful shutdown                                            |

### Per-task scripts

Embed a `MOCK_SCRIPT:` line in the task description and the mock will run it
without waiting for HTTP control. Each step is one of:

```json
[
  {"state": "thinking", "delay_ms": 100},
  {"output": "[mock] reading source", "delay_ms": 50},
  {"state": "working", "activity": "editing"},
  {"complete": {"prUrl": "https://example.com/pr/1", "lastText": "done"}}
]
```

If the script doesn't end with `complete` / `fail`, the slot parks on the
future and finishes once the HTTP API resolves it. Mixed mode (script then
HTTP) lets you script the noisy bits and have the test assert the terminal
state.
