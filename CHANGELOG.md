# Changelog

All notable changes to Pioneer Square are recorded here.

## Unreleased

### Breaking changes

- **`_status_snapshot` dict key renamed: `"slots"` → `"agents"`** (worker / mock-worker).
  The status snapshot returned by `MockWorker._status_snapshot()` previously used the
  key `"slots"` for the list of agent records. It has been renamed to `"agents"` to
  match the terminology introduced in the Worker-class refactor (PR #412).

  A backward-compatibility alias (`snap["slots"] = snap["agents"]`) is kept for **one
  release** so existing consumers don't break immediately. The `"slots"` key will be
  **removed** in the next release. Migrate any code that reads `snap["slots"]` to use
  `snap["agents"]` instead.

### Refactoring

- **Worker class: agent/task ownership clarity** (PR #412, closes #411).
  The `Worker` class was refactored to make two ownership relationships explicit:
  - *Agents are owned by a Worker* — agent lifecycle is managed inside the Worker
    (created/destroyed with the Worker process). The `Agent` inner class now documents
    this clearly.
  - *Tasks are not owned by workers* — tasks execute *on* a worker/agent pair but
    belong to the task system. `Worker.agents` (formerly `Worker.slots`) holds the
    execution contexts; the task queue and outcomes remain separate concerns.
  - Renamed: `slots` → `agents` throughout `worker.py`, `mock_worker.py`, and tests.
