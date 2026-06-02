"""Optional HTTP control API for a live worker.

Real workers receive their work from the backend/foreman over WebSocket. That
makes them awkward to exercise in isolation: you need a running backend, a
guild, and a frontend (or the foreman) to assign a task before the worker does
anything. This module exposes a small HTTP server — disabled by default,
enabled with ``--api-port`` / ``[api].port`` — that lets you inspect the
worker and inject tasks straight into its queue, bypassing the foreman.

It runs the real execution path (clone, worktree, claude/codex/pi, push, PR),
so it's a faithful way to smoke-test a worker against a backend without driving
the UI. It is intentionally unauthenticated and meant for local/dev use; bind
it to localhost (the default) and don't expose it publicly.

Endpoints (default ``127.0.0.1:9200``):

    GET  /                     status JSON (worker id, agents, queue depth)
    GET  /status               alias of /
    GET  /agents               list of agent slots with current state
    GET  /tasks                known task ids + queue depth
    POST /tasks                inject a task; body:
                                 {description, name?, tool?, phase?, repos?,
                                  issueNumber?, issueRepo?, id?,
                                  followupInstructions?, followupBranch?}
    POST /control/shutdown     graceful shutdown

The handler runs on a background thread; all worker state is touched only on
the worker's event loop via ``run_coroutine_threadsafe`` / ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .worker import Worker

logger = logging.getLogger(__name__)


class ControlServer:
    """Threaded HTTP control API bound to a :class:`Worker`."""

    def __init__(self, worker: Worker, *, host: str = "127.0.0.1", port: int = 9200) -> None:
        self.worker = worker
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler_cls = _make_handler(self.worker)
        try:
            self._server = ThreadingHTTPServer((self.host, self.port), handler_cls)
        except OSError as exc:
            logger.error("Control API could not bind to %s:%d — %s", self.host, self.port, exc)
            raise
        bound_host, bound_port = self._server.server_address[:2]
        self.port = bound_port  # in case 0 was passed in
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="worker-control-api", daemon=True
        )
        self._thread.start()
        logger.info("Control API listening on http://%s:%d", bound_host, bound_port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def _make_handler(worker: Worker) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to *worker*."""

    def _run_coro(coro):
        """Schedule *coro* on the worker's loop and wait for the result."""
        assert worker._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, worker._loop)
        return fut.result(timeout=30.0)

    def _call_sync(fn, *args, **kwargs):
        """Run a callable on the worker's loop (the loop owns shared state)."""
        assert worker._loop is not None
        result: dict = {}

        def _runner():
            result["value"] = fn(*args, **kwargs)

        worker._loop.call_soon_threadsafe(_runner)
        deadline, step, elapsed = 5.0, 0.005, 0.0
        while "value" not in result and elapsed < deadline:
            threading.Event().wait(step)
            elapsed += step
        return result.get("value", {"error": "timeout"})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A003
            logger.debug("[control-api] %s - %s", self.address_string(), format % args)

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

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/", "/status"):
                self._send_json(200, _call_sync(worker._status_snapshot))
                return
            if self.path == "/agents":
                snap = _call_sync(worker._status_snapshot)
                self._send_json(200, {"agents": snap.get("agents", [])})
                return
            if self.path == "/tasks":
                snap = _call_sync(worker._status_snapshot)
                self._send_json(
                    200,
                    {
                        "knownTaskIds": snap.get("knownTaskIds", []),
                        "queueDepth": snap.get("queueDepth", 0),
                    },
                )
                return
            self._send_json(404, {"error": "not found", "path": self.path})

        def do_POST(self) -> None:  # noqa: N802
            body = self._read_json()
            try:
                if self.path == "/tasks":
                    result = _run_coro(worker._inject_task(body))
                    self._send_json(200 if "ok" in result else 400, result)
                    return
                if self.path == "/control/shutdown":
                    _run_coro(worker._initiate_shutdown("control API shutdown"))
                    self._send_json(200, {"ok": True})
                    return
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("[control-api] handler crashed: %s", exc)
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(404, {"error": "not found", "path": self.path})

    return Handler
