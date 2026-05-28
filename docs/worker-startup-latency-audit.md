# Worker Startup & Task Assignment Latency Audit

Audit date: 2026-05-28  
Branch: `claude/audit-worker-startup-latency-t-w8tz`

---

## Summary

Task assignment is **push-based over WebSocket** — the server broadcasts `task-assigned` immediately when a task is created with no intentional queue delay. The dominant latency risks are:

1. **Fallback poll interval: 300 s** (`pull_interval` default) — tasks missed during WS downtime are not re-fetched until 5 minutes later.
2. **Sequential startup sequence** — six sequential I/O steps (registration, GitHub token, repo discovery, tool auth checks, WS connect, Claude auth) must all complete before the worker announces itself as ready.
3. **Claude auth flow: up to 300 s** — if Claude credentials are absent, the worker blocks on a manual auth-code paste before joining.

---

## 1. Hardcoded Sleep / Wait Durations

| File | Line | Value | Purpose |
|------|------|-------|---------|
| `worker/pioneer_worker/worker.py` | 568 | `asyncio.sleep(0.2)` | Auth PTY: delay between writing auth code and CR (Ink batching workaround) |
| `worker/pioneer_worker/worker.py` | 665 | `asyncio.sleep(15.0)` | Auth login watchdog: periodic log tick while waiting for `claude setup-token` |
| `worker/pioneer_worker/worker.py` | 545 | `asyncio.wait_for(..., timeout=300.0)` | **Auth code queue wait** — blocks startup for up to 5 minutes if Claude credentials are missing |

The `0.2 s` and `15 s` sleeps are inside the auth login flow and do not block normal (already-authenticated) startups.

---

## 2. Queue Polling Intervals

### Idle puller — `worker.py:1492–1502`

```python
async def _idle_puller(self) -> None:
    ...
    await asyncio.wait_for(
        self._shutdown_event.wait(),
        timeout=self.cfg.pull_interval,   # default 300.0 s
    )
```

- **Default**: `300.0 s` (5 minutes) — `config.py:52`
- **Configurable** via `pull_interval` in `pioneer-worker.toml` or the `pull_interval` override key.
- This is the **fallback** for tasks missed while the WebSocket was down. During normal operation the WS push arrives immediately; the idle puller is only needed for recovery.
- At reconnect time, `_on_ws_reconnect` (`worker.py:800–806`) calls `_fetch_pending_tasks()` immediately, so the 300 s gap only applies if the WS stays up but a push was lost.

### GitHub repo refresh — `worker.py:1517–1521`, constant at line 56

```python
REPO_REFRESH_INTERVAL_SECONDS = 20 * 60   # 1200 s = 20 minutes
```

- Not on the critical task-pickup path; only refreshes the list of repos the worker advertises.

---

## 3. Backoff / Retry Logic

### WebSocket reconnect — `ws_client.py:57–91`

```python
def _backoff_delay(attempt: int, base: float, cap: float) -> float:
    exp = min(cap, base * (2**attempt))
    return random.uniform(0, exp)

# defaults: base_backoff=1.0, max_backoff=30.0
```

| Attempt | Max delay |
|---------|-----------|
| 0 | 1 s |
| 1 | 2 s |
| 2 | 4 s |
| 3 | 8 s |
| 4 | 16 s |
| 5+ | 30 s (capped) |

- Retries **indefinitely** (`ws_client.py:59`: "Retries indefinitely — the worker is a long-running daemon").
- While reconnecting, the worker cannot receive push task assignments; the reconnect backoff is the upper bound on how long a task sits undelivered before the WS is re-established.

### WebSocket send retry — `ws_client.py:113–130`

- Up to `send_retries=3` attempts with the same exponential backoff.
- If all 3 fail, the send is dropped (error logged).

### WS transport / recv error backoff — `ws_client.py:160,164`

- Uses `_backoff_delay(0, base_backoff, max_backoff)` = up to 1 s random jitter before reconnecting on a recv error.

---

## 4. Task Assignment Flow

### Push path (normal)

```
Foreman calls assign_task tool
  → backend/routes/workers.py broadcasts { type: "task-assigned", ... } over WS
  → worker._listen() receives message, enqueues to task_queue (asyncio.Queue)
  → _agent_loop() dequeues and starts execution
```

No intentional delay in this path. The only latency is network RTT.

Reference: `backend/routes/workers.py` broadcasts `task-assigned` at the point of assignment; `worker.py:1094–1119` (listener) enqueues it.

### Poll fallback path

```
task-assigned WS push missed (e.g. worker offline or WS flap)
  → _idle_puller() fires after pull_interval (default 300 s)
  → _fetch_pending_tasks() GET /guilds/{id}/workers/{id}/tasks
  → tasks enqueued for execution
```

Maximum extra wait: **300 s** on default config.

### Reconnect recovery

`_on_ws_reconnect` (`worker.py:800–806`) calls `_fetch_pending_tasks()` immediately on reconnection, bypassing the idle puller interval for this specific case.

---

## 5. Initialization Steps Before Worker Signals Ready

All steps are **sequential** (`worker.py:971–990`). The worker does not appear in the backend's agent list until `_join()` at line 990.

| Step | Code location | Notes |
|------|---------------|-------|
| 1. `_register()` | `worker.py:971` | POST to backend; establishes `worker_id` |
| 2. `_fetch_github_token_if_needed()` | `worker.py:974` | HTTP GET if token absent from config/env |
| 3. `_refresh_github_repos()` | `worker.py:975` | GitHub API call; discovers org repos |
| 4. `_check_gh_auth()` | `worker.py:976` | Runs `gh auth status` — **10 s timeout** (`worker.py:231`) |
| 5. `_check_codex_doctor()` | `worker.py:977` | Runs `codex doctor` — **20 s timeout** (`worker.py:267`) |
| 6. `_ensure_codex_api_key()` | `worker.py:978` | Validates OpenAI key in config |
| 7. `ws.connect()` | `worker.py:982` | WS connect with exponential backoff (up to 30 s per attempt, infinite retries) |
| 8. `_check_claude_auth()` | `worker.py:988` | **Blocks up to 300 s** if credentials missing (manual auth-code paste) |
| 9. `_join()` | `worker.py:990` | Sends `join` + `worker-register` — **worker becomes visible** |
| 10. Clone / fetch repos | `worker.py:999–1006` | Parallelized with `asyncio.gather`; runs after join |
| 11. `_initial_worktree_sweep()` | `worker.py:1018` | **30 s timeout**; prunes stale worktrees |
| 12. `_fetch_pending_tasks()` | `worker.py:1022` | Catches any tasks assigned before worker connected |
| 13. Start agent loops, puller, heartbeat, sweeper | `worker.py:1029–1032` | Worker now fully operational |

**Steps 4–6 always execute even if Codex/Pi runners are not in use.** `_check_codex_doctor()` is guarded (`worker.py:262–278`) and only logs a warning on failure, but still runs the subprocess and waits up to 20 s.

---

## 6. Connection / Handshake Timeouts

| Location | File | Line | Value |
|----------|------|------|-------|
| HTTP client default timeout | `worker.py` | 158 | `30.0 s` |
| `gh auth status` subprocess | `worker.py` | 231 | `10.0 s` |
| `codex doctor` subprocess | `worker.py` | 267 | `20.0 s` |
| `claude auth status --json` subprocess | `worker.py` | 326 | `10.0 s` |
| Auth code queue wait (manual login) | `worker.py` | 545 | `300.0 s` |
| Initial worktree sweep | `worker.py` | 1018 | `30.0 s` |
| WS ping interval (transport) | `ws_client.py` | 71 | `ping_interval=20` |
| WS ping timeout (transport) | `ws_client.py` | 71 | `ping_timeout=20` |
| WS reconnect backoff cap | `ws_client.py` | 40 | `30.0 s` |
| Worker offline detection (backend) | `backend/main.py` | ~69 | `WORKER_OFFLINE_AFTER_SECONDS = 90` |
| Backend stale-agent sweep interval | `backend/main.py` | ~71 | `WORKER_SWEEP_INTERVAL_SECONDS = 30` |

---

## 7. Heartbeat

- Interval: **25 s** (`worker.py:900`, `HEARTBEAT_INTERVAL_SECONDS = 25.0`)
- Backend marks worker offline after **90 s** without ping.
- With 25 s intervals, 3 missed heartbeats (~75 s) triggers offline marking; the comment at line 899 confirms this is intentional.

---

## 8. GitHub Actions CI

`.github/workflows/ci.yml` — no worker-startup-related delays. The postgres service uses `--health-interval 5s` / `--health-retries 5` (max 25 s wait) before backend tests start. No sleep commands.

---

## Key Findings by Severity

### High impact
- **`pull_interval = 300 s`** (`config.py:52`, `worker.py:1498`): If the WS push is missed (e.g. worker was briefly offline when task was assigned), the task won't be picked up for up to 5 minutes. The reconnect path (`worker.py:803`) mitigates this for WS-down scenarios, but not for a task assigned while the WS is up and the push is simply lost.

### Medium impact
- **Sequential startup** (`worker.py:971–990`): Pre-join steps run one after another with no parallelization. On a slow network or cold machine the sequence can take 30–60 s before the worker is visible. Steps 4–6 (`gh auth status`, `codex doctor`, `claude auth status`) add 10–20 s each even when the corresponding tools are not configured for use.
- **WS reconnect backoff cap 30 s** (`ws_client.py:40`): After 5+ failed connection attempts the worker waits up to 30 s between retries. During this window push tasks are undeliverable.

### Low impact (by design or configurable)
- **`asyncio.sleep(0.2)`** (`worker.py:568`): Only inside auth PTY interaction; no impact on normal task pickup.
- **`asyncio.sleep(15.0)` watchdog** (`worker.py:665`): Only runs during interactive `claude setup-token` flow.
- **`WORKTREE_SWEEP_INTERVAL_SECONDS = 3600`** (`worker.py:52`): Background cleanup; not on task-pickup path.
- **`REPO_REFRESH_INTERVAL_SECONDS = 1200`** (`worker.py:56`): Background; not on task-pickup path.
- **Heartbeat 25 s** (`worker.py:900`): Well within the 90 s threshold; no task-latency impact.
