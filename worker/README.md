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

Optional:

- `worker_id` — pre-existing worker id from the backend. If omitted, the worker
  registers itself on first launch and writes the assigned id to
  `.pioneer-worker.state.json` next to the config.
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
