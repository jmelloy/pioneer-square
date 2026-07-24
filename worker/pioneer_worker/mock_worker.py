"""Mock worker for e2e tests.

Subclass of :class:`Worker` that connects to the backend over WebSocket the
same way a real worker does (same ``join`` / ``worker-register`` /
``agent-state`` / ``terminal-output`` / ``task-*`` frames), but skips every
side effect — no GitHub clones, no worktrees, no claude subprocess, no
``gh`` invocations. A small HTTP API on ``--mock-api-port`` lets the test
harness (Playwright) drive agent state and task lifecycle deterministically.

HTTP endpoints (default ``127.0.0.1:9100``):

    GET  /                              status JSON
    GET  /agents                        list of slots with current state
    POST /agents/{agentId}/state        body: {state, activity?, taskId?}
    POST /agents/{agentId}/output       body: {line, taskId?, detail?}
    POST /tasks/{taskId}/complete       body: {branch?, prUrl?, stopReason?,
                                               lastText?, sessionId?}
    POST /tasks/{taskId}/fail           body: {stopReason?, lastMessage?}
    POST /control/shutdown              graceful shutdown

Per-task scripted runs are supported by embedding ``MOCK_SCRIPT: [...]`` in
the task description; if present the mock runs the script to completion. If
absent the slot stays in ``working`` until a ``/tasks/{taskId}/complete`` or
``/tasks/{taskId}/fail`` request lands.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as config_mod
from .worker import Worker, _now_iso

logger = logging.getLogger(__name__)


_MOCK_SCRIPT_PREFIX = "MOCK_SCRIPT:"


def _extract_script(task: dict) -> list[dict] | None:
    """Return a parsed script from the task description, or None.

    Looks for a line beginning with ``MOCK_SCRIPT:`` followed by a JSON array.
    Each step is one of:

      {"state": "<idle|thinking|working|busy|error>", "activity": "...", "delay_ms": 0}
      {"output": "<line>", "taskId?": "...", "delay_ms": 0}
      {"complete": {"prUrl?": "...", "stopReason?": "...", "lastText?": "..."}}
      {"fail": {"stopReason?": "...", "lastMessage?": "..."}}
    """
    desc = task.get("description") or ""
    for line in desc.splitlines():
        stripped = line.strip()
        if stripped.startswith(_MOCK_SCRIPT_PREFIX):
            payload = stripped[len(_MOCK_SCRIPT_PREFIX) :].strip()
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError as exc:
                logger.warning("MOCK_SCRIPT JSON parse failed: %s", exc)
                return None
            if isinstance(parsed, list):
                return parsed
            logger.warning("MOCK_SCRIPT must be a JSON array (got %s)", type(parsed).__name__)
            return None
    return None


class MockWorker(Worker):
    """Worker that fakes every external dependency and exposes an HTTP control API."""

    def __init__(
        self,
        cfg: config_mod.Config,
        *,
        mock_api_port: int = 9100,
        mock_api_host: str = "127.0.0.1",
    ) -> None:
        super().__init__(cfg)
        self.mock_api_port = mock_api_port
        self.mock_api_host = mock_api_host
        # Future per active task; resolved by the HTTP handler to signal the
        # agent loop that the task should complete or fail.
        self._task_outcomes: dict[str, asyncio.Future] = {}
        # Captured at run() start so the HTTP handler thread can schedule
        # coroutines onto the worker's loop via run_coroutine_threadsafe.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._http_server: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None

    # ── Skip real-world setup ───────────────────────────────────────────────
    async def _fetch_github_token_if_needed(self) -> None:
        logger.info("[mock] skipping GitHub token fetch")

    async def _refresh_github_repos(self) -> None:
        logger.debug("[mock] skipping GitHub repo refresh")

    async def _check_gh_auth(self) -> None:
        logger.info("[mock] skipping gh auth check")

    async def _detect_available_tools(self) -> None:
        # Skip real PATH/credential probing; mock workers can run any tool.
        self._available_tools = list(self.cfg.tools or ["claude", "codex", "pi"])
        logger.info("[mock] available tools: %s", self._available_tools)

    async def _initial_worktree_sweep(self) -> None:
        return  # nothing on disk to sweep

    async def _worktree_sweeper(self) -> None:
        # Park until shutdown; no worktrees to manage.
        await self._shutdown_event.wait()

    async def _idle_puller(self) -> None:
        """REST-poll loop without the git-pull side effect."""
        logger.info("[mock] idle puller started (interval=%.1fs)", self.cfg.pull_interval)
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=self.cfg.pull_interval)
                return
            except TimeoutError:
                pass
            try:
                pending = await self._fetch_pending_tasks()
                for task in pending:
                    if task["id"] in self._known_task_ids:
                        continue
                    logger.info("[mock] picked up missed task %s via poll", task["id"])
                    self._known_task_ids.add(task["id"])
                    await self.task_queue.put(task)
            except Exception as exc:
                logger.warning("[mock] pending-task poll failed: %s", exc)

    # ── Run lifecycle ───────────────────────────────────────────────────────
    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._start_http_server()
        try:
            await super().run()
        finally:
            self._stop_http_server()

    def _start_http_server(self) -> None:
        handler_cls = _make_handler(self)
        try:
            self._http_server = ThreadingHTTPServer(
                (self.mock_api_host, self.mock_api_port), handler_cls
            )
        except OSError as exc:
            logger.error(
                "[mock] could not bind HTTP API to %s:%d — %s",
                self.mock_api_host,
                self.mock_api_port,
                exc,
            )
            raise
        bound_host, bound_port = self._http_server.server_address[:2]
        self.mock_api_port = bound_port  # in case 0 was passed in
        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever,
            name="mock-api",
            daemon=True,
        )
        self._http_thread.start()
        logger.info("[mock] HTTP control API listening on %s:%d", bound_host, bound_port)

    def _stop_http_server(self) -> None:
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None
        if self._http_thread is not None:
            self._http_thread.join(timeout=2.0)
            self._http_thread = None

    # ── Task execution (replaces claude/codex/pi runners) ──────────────────
    async def _execute_task(self, task: dict, agent) -> None:
        task_id = task["id"]
        desc = task.get("description") or ""
        is_followup = bool(task.get("followup_instructions"))
        branch = task.get("followup_branch") or f"mock/{task_id[:8]}"

        await self._set_state("working", agent)
        await self._task_update(task_id, agent=agent, state="working", branch=branch)

        emit = self._task_emit(task_id, agent)
        await emit(f"[mock] Task {task_id}: {desc[:120]}")
        await emit(f"[mock] Branch: {branch}")

        script = _extract_script(task)
        outcome: dict | None = None
        if script is not None:
            outcome = await self._run_script(task_id, agent, script, emit)

        if outcome is None:
            # No script (or script didn't terminate the task) — wait for HTTP.
            assert self._loop is not None
            future = self._loop.create_future()
            self._task_outcomes[task_id] = future
            try:
                outcome = await future
            finally:
                self._task_outcomes.pop(task_id, None)

        await self._finalize_task(task_id, agent, outcome, desc, branch, is_followup)

    async def _run_script(self, task_id: str, agent, script: list[dict], emit) -> dict | None:
        """Execute a script of steps. Returns an outcome dict if the script
        terminated the task; otherwise None (caller will wait on HTTP)."""
        for step in script:
            if not isinstance(step, dict):
                logger.warning("[mock] skipping non-dict script step: %r", step)
                continue
            delay_ms = step.get("delay_ms")
            if delay_ms:
                await asyncio.sleep(float(delay_ms) / 1000.0)
            if "state" in step:
                new_state = step["state"]
                if "activity" in step:
                    agent.activity = step["activity"]
                await self._set_state(new_state, agent)
                if new_state == "working" and agent.current_task_id is None:
                    agent.current_task_id = task_id
                continue
            if "output" in step:
                detail = step.get("detail")
                await emit(step["output"], detail=detail)
                continue
            if "complete" in step:
                spec = step["complete"] or {}
                return {"kind": "complete", **spec}
            if "fail" in step:
                spec = step["fail"] or {}
                return {"kind": "fail", **spec}
            logger.warning("[mock] unrecognized script step: %r", step)
        return None

    async def _finalize_task(
        self,
        task_id: str,
        agent,
        outcome: dict,
        desc: str,
        branch: str,
        is_followup: bool,
    ) -> None:
        kind = outcome.get("kind", "complete")
        if kind == "complete":
            pr_url = outcome.get("prUrl", "")
            session_id = outcome.get("sessionId", "")
            last_text = outcome.get("lastText", "mock complete")
            stop_reason = outcome.get("stopReason", "end_turn")
            final_branch = outcome.get("branch", branch)

            await self._task_update(
                task_id,
                agent=agent,
                branch=final_branch,
                prUrl=pr_url,
                state="awaiting-review",
            )
            if is_followup:
                await self._send(
                    {
                        "type": "task-followup-done",
                        "workerId": self.cfg.worker_id,
                        "agentId": agent.agent_id,
                        "taskId": task_id,
                        "success": True,
                        "stopReason": stop_reason,
                        "branch": final_branch,
                        "sessionId": session_id,
                        "prUrl": pr_url,
                    }
                )
            else:
                await self._send(
                    {
                        "type": "task-complete",
                        "workerId": self.cfg.worker_id,
                        "agentId": agent.agent_id,
                        "taskId": task_id,
                        "branch": final_branch,
                        "description": desc,
                        "prUrl": pr_url,
                        "sessionId": session_id,
                        "lastText": last_text,
                    }
                )
        else:  # fail
            stop_reason = outcome.get("stopReason", "mock_failure")
            last_message = outcome.get("lastMessage", "mock failure")
            await self._task_update(task_id, agent=agent, branch=branch, state="failed")
            await self._send(
                {
                    "type": "needs-input",
                    "workerId": self.cfg.worker_id,
                    "agentId": agent.agent_id,
                    "taskId": task_id,
                    "description": desc,
                    "branch": branch,
                    "sessionId": "",
                    "stopReason": stop_reason,
                    "lastMessage": last_message,
                }
            )
            await self._set_state("error", agent)
        await self._set_state("idle", agent)

    # ── Called from HTTP handler thread via run_coroutine_threadsafe ──────
    async def _http_set_state(
        self, agent_id: str, state: str, activity: str | None, task_id: str | None
    ) -> dict:
        slot = next((s for s in self.agents if s.id == agent_id), None)
        if slot is None:
            return {"error": f"unknown agentId {agent_id!r}"}
        if task_id is not None:
            slot.current_task_id = task_id
        await self._set_state(state, slot)
        # _set_state nulls activity on non-working transitions; reapply if the
        # caller explicitly asked for one so tests can pin any (state, activity)
        # combination they want.
        if activity is not None and slot.activity != activity:
            slot.activity = activity
            await self._emit_agent_state(slot)
        return {
            "ok": True,
            "agentId": slot.id,
            "state": slot.state,
            "activity": slot.activity,
            "taskId": slot.current_task_id,
        }

    async def _http_emit_output(
        self, agent_id: str, line: str, task_id: str | None, detail: dict | None
    ) -> dict:
        slot = next((s for s in self.agents if s.id == agent_id), None)
        if slot is None:
            return {"error": f"unknown agentId {agent_id!r}"}
        msg: dict = {
            "type": "terminal-output",
            "workerId": self.cfg.worker_id,
            "agentId": slot.id,
            "line": line,
            "timestamp": _now_iso(),
        }
        if task_id:
            msg["taskId"] = task_id
        if detail:
            msg["detail"] = detail
        await self._send(msg)
        return {"ok": True}

    def _resolve_task_outcome(self, task_id: str, outcome: dict) -> dict:
        future = self._task_outcomes.get(task_id)
        if future is None or future.done():
            return {"error": f"no active task {task_id!r}"}
        future.set_result(outcome)
        return {"ok": True, "taskId": task_id, "kind": outcome.get("kind")}

    def _status_snapshot(self) -> dict:
        snap = {
            "workerId": self.cfg.worker_id,
            "workerName": self._worker_name,
            "guildId": self.cfg.guild_id,
            "agents": [
                {
                    "agentId": s.id,
                    "agentName": s.name,
                    "state": s.state,
                    "activity": s.activity,
                    "taskId": s.current_task_id,
                }
                for s in self.agents
            ],
            "activeTasks": list(self._task_outcomes.keys()),
            "queueDepth": self.task_queue.qsize(),
        }
        # DEPRECATED: "slots" was renamed to "agents". Keep it for one release
        # so external consumers don't hard-break; will be removed next release.
        snap["slots"] = snap["agents"]
        return snap


# ────────────────────────────────────────────────────────────────────────────
# HTTP handler factory
# ────────────────────────────────────────────────────────────────────────────


def _make_handler(worker: MockWorker) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to *worker*."""

    def _run_coro(coro):
        """Schedule *coro* on the worker's event loop and wait for the result."""
        assert worker._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, worker._loop)
        return fut.result(timeout=10.0)

    def _call_sync(fn, *args, **kwargs):
        """Run a callable on the worker's loop (the loop owns shared state)."""
        assert worker._loop is not None
        result: dict = {}

        def _runner():
            result["value"] = fn(*args, **kwargs)

        worker._loop.call_soon_threadsafe(_runner)
        # Spin briefly until the call_soon ran. The loop's tick is fast enough
        # for test traffic; this avoids needing an extra coroutine wrapper.
        deadline = 5.0
        step = 0.005
        elapsed = 0.0
        while "value" not in result and elapsed < deadline:
            threading.Event().wait(step)
            elapsed += step
        return result.get("value", {"error": "timeout"})

    class Handler(BaseHTTPRequestHandler):
        # Quiet down the default access log.
        def log_message(self, format: str, *args) -> None:  # noqa: A003 - stdlib name
            logger.debug("[mock-api] %s - %s", self.address_string(), format % args)

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode() or "{}")
            except json.JSONDecodeError:
                return {}

        # ── GET ────────────────────────────────────────────────────────────
        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            if self.path in ("/", "/status"):
                self._send_json(200, _call_sync(worker._status_snapshot))
                return
            if self.path == "/agents":
                snap = _call_sync(worker._status_snapshot)
                self._send_json(200, {"agents": snap.get("agents", [])})
                return
            self._send_json(404, {"error": "not found"})

        # ── POST ───────────────────────────────────────────────────────────
        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            parts = [p for p in self.path.split("/") if p]
            body = self._read_json()
            try:
                if len(parts) == 3 and parts[0] == "agents" and parts[2] == "state":
                    agent_id = parts[1]
                    state = body.get("state")
                    if not state:
                        self._send_json(400, {"error": "state is required"})
                        return
                    result = _run_coro(
                        worker._http_set_state(
                            agent_id, state, body.get("activity"), body.get("taskId")
                        )
                    )
                    self._send_json(200 if "ok" in result else 404, result)
                    return

                if len(parts) == 3 and parts[0] == "agents" and parts[2] == "output":
                    agent_id = parts[1]
                    line = body.get("line", "")
                    result = _run_coro(
                        worker._http_emit_output(
                            agent_id, line, body.get("taskId"), body.get("detail")
                        )
                    )
                    self._send_json(200 if "ok" in result else 404, result)
                    return

                if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "complete":
                    task_id = parts[1]
                    outcome = {"kind": "complete", **body}
                    result = worker._resolve_task_outcome(task_id, outcome)
                    self._send_json(200 if "ok" in result else 404, result)
                    return

                if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "fail":
                    task_id = parts[1]
                    outcome = {"kind": "fail", **body}
                    result = worker._resolve_task_outcome(task_id, outcome)
                    self._send_json(200 if "ok" in result else 404, result)
                    return

                if self.path == "/control/shutdown":
                    _run_coro(worker._initiate_shutdown("mock HTTP shutdown"))
                    self._send_json(200, {"ok": True})
                    return

            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("[mock-api] handler crashed: %s", exc)
                self._send_json(500, {"error": str(exc)})
                return

            self._send_json(404, {"error": "not found", "path": self.path})

    return Handler
