# Pioneer Square Worker

Standalone worker agent for [Pioneer Square](../README.md). Connects to a backend
session over WebSocket, listens for task assignments, runs
`claude --dangerously-skip-permissions` against a fresh git worktree, then
pushes the result and opens a GitHub PR.

The worker is a separate process from the backend so it can run on a different
machine — typically one with the cloned repos, a `claude` CLI install, and a
GitHub token.

## Install

```bash
cd worker
python -m venv venv
source venv/bin/activate
pip install -e .
```

This installs the `pioneer-worker` console script.

## Expected toolchain

A worker shells out to a coding agent (`claude`, `codex`, or `pi`), which in
turn runs whatever the task needs — installing dependencies, running tests,
linting, building, opening PRs. To keep agents from having to bootstrap a
language runtime mid-task, every worker host should have the following
pre-installed and on `PATH`:

**Core**

- `git`, `curl`, `jq`, `make`, `unzip`, `openssh-client`, `ripgrep`
- A C/C++ toolchain (`build-essential` on Debian — needed to compile native
  Python wheels and Node addons)
- [`gh`](https://cli.github.com/) — used by agents to inspect issues and PRs

**Python 3.11+**

- `python3`, `pip`, `venv`
- [`ruff`](https://docs.astral.sh/ruff/) (lint + format)
- [`pytest`](https://docs.pytest.org/)
- [`uv`](https://docs.astral.sh/uv/) (fast resolver/installer used by some repos)
- [`pipx`](https://pipx.pypa.io/) (sandboxed CLI installs)

**Node.js 24+**

- `node`, `npm`, `npx`
- `corepack` enabled, so repos pinned to `pnpm`/`yarn` via `packageManager` work
- The agent CLIs themselves: `@anthropic-ai/claude-code`, `@openai/codex`,
  `@mariozechner/pi-coding-agent`

**Go 1.23+**

- `go` on `PATH`, with `GOPATH` writable by the worker user

The `worker/Dockerfile` provisions exactly this set, so anything `docker
compose --profile worker up` builds is already correct. If you run the worker
directly on a host, install the equivalents through your package manager
(`apt`, `brew`, etc.) before launching it.

Anything outside this baseline (Rust, Java, Ruby, .NET, Terraform, etc.) is
expected to be installed on demand by the task itself, or added to the
Dockerfile in a follow-up PR if it becomes a recurring need.

## Configure

Copy the example config and edit it:

```bash
cp pioneer-worker.toml.example pioneer-worker.toml
$EDITOR pioneer-worker.toml
```

Required keys:

- `backend_url` — `http://host:port` (or `ws://...`); the worker derives the
  WebSocket URL automatically.
- `session_id` — the 6-char session id from the Pioneer Square UI.
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
2. Opens a WebSocket to `/ws/{session_id}` and announces itself with `join`
   and `worker-register` messages.
3. Fetches any pending tasks via REST so it can resume work across restarts.
4. Listens for `task-assigned` messages whose `workerId` matches its own id.
5. For each task: clones the configured repos under `repos_dir`, creates a
   worktree under `work_dir`, runs `claude` in the worktree, then pushes the
   branch and opens a PR via the GitHub API.
6. Reports state changes (`working`, `done`, `failed`, branch, PR url) back
   to the backend via `task-update` WebSocket messages.

## Multiple workers

Run more than one worker against the same session by giving each its own
config directory (so the sidecar state file doesn't collide):

```bash
mkdir -p ~/.pioneer/builder ~/.pioneer/tester
pioneer-worker --config ~/.pioneer/builder/pioneer-worker.toml &
pioneer-worker --config ~/.pioneer/tester/pioneer-worker.toml  &
```
