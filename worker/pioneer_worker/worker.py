"""Main worker loop: register, listen for tasks over WebSocket, execute, report."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import signal
import socket
import string
from datetime import UTC, datetime

import anyio
import httpx

from . import auth, claude_runner, codex_runner, git_ops, github_pr, pi_runner
from . import config as config_mod
from .ws_client import WSClient

logger = logging.getLogger(__name__)


def _gen_id(prefix: str) -> str:
    return prefix + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _slug(text: str, max_len: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text[:max_len].lower()).strip("-")


_CANCEL_SENTINEL = object()  # placed in followup queue to signal task cancellation
_SHUTDOWN_SENTINEL = object()  # placed in task queue to wake idle agents during shutdown


class _AgentSlot:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.current_claude: claude_runner.ClaudeProcess | None = None
        self.current_task_id: str | None = None
        # Last state we told the backend about; resent on WS reconnect so the
        # backend (and frontend) don't show the agent stuck offline.
        self.state: str = "idle"
        # Fine-grained activity within the "working" state (reading/editing/etc.)
        self.activity: str | None = None


class Worker:
    def __init__(self, cfg: config_mod.Config) -> None:
        self.cfg = cfg
        self.ws = WSClient(cfg.ws_url)
        self.task_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._known_task_ids: set[str] = set()
        # Per-task queues for follow-up instructions; None signals finalization
        self._followup_queues: dict[str, asyncio.Queue] = {}
        # Tasks that have been cancelled (by foreman or human); checked before/during execution
        self._cancelled_tasks: set[str] = set()
        # Per-task queues for mid-run redirect instructions (SIGTERM + --resume)
        self._redirect_queues: dict[str, asyncio.Queue] = {}
        # Set to True once _join() has been called the first time so that
        # _on_ws_reconnect doesn't prematurely join before auth completes.
        self._joined = False
        # One slot per concurrent agent; each gets a stable ID for the guild.
        self.slots: list[_AgentSlot] = [_AgentSlot(_gen_id("a-")) for _ in range(cfg.max_agents)]
        # Set when graceful shutdown is requested (via WS or signal). Idle
        # agents stop immediately; busy agents finish their current task and
        # skip the follow-up window.
        self._shutdown_event = asyncio.Event()
        self.auth = auth.AuthFlow(
            claude_path=cfg.claude_path,
            worker_id_provider=lambda: self.cfg.worker_id,
            guild_id=cfg.guild_id,
            http_factory=self._http,
            send=self._send,
            emit=self._emit,
        )

    # ------------------------------------------------------------------ HTTP
    async def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.cfg.http_url, timeout=30.0)

    async def _register(self) -> None:
        async with await self._http() as client:
            resp = await client.post(
                f"/guilds/{self.cfg.guild_id}/workers",
                json={
                    "repos": self.cfg.repos,
                    "github_token": None,
                    "hostname": socket.gethostname(),
                },
            )
            resp.raise_for_status()
            wid = resp.json()["id"]
        self.cfg.worker_id = wid
        logger.info("Registered as worker %s (%d agents)", wid, len(self.slots))

    async def _fetch_github_token_if_needed(self) -> None:
        if self.cfg.github_token:
            return
        try:
            async with await self._http() as client:
                resp = await client.get(
                    "/auth/github/token",
                    params={"guild_id": self.cfg.guild_id},
                )
            if resp.status_code == 200:
                data = resp.json()
                self.cfg.github_token = data.get("access_token")
                logger.info("Fetched GitHub token for user %s", data.get("username"))
            else:
                logger.warning("No GitHub token from backend (status %d)", resp.status_code)
        except Exception as exc:
            logger.warning("Could not fetch GitHub token: %s", exc)

    async def _claude_is_authenticated(self) -> bool:
        """Thin delegate to :func:`auth.is_authenticated` (kept for tests)."""
        return await auth.is_authenticated(self.cfg.claude_path)

    async def _fetch_pending_tasks(self) -> list[dict]:
        async with await self._http() as client:
            resp = await client.get(
                f"/guilds/{self.cfg.guild_id}/workers/{self.cfg.worker_id}/tasks"
            )
            resp.raise_for_status()
            tasks = resp.json()
        return [t for t in tasks if t.get("state") in ("pending", "working")]

    # ------------------------------------------------------------------ WS helpers
    async def _send(self, payload: dict) -> None:
        await self.ws.send(payload)

    async def _emit(self, line: str, detail: dict | None = None) -> None:
        """Emit a worker-level log line (attributed to slot 0)."""
        msg: dict = {
            "type": "terminal-output",
            "agentId": self.slots[0].agent_id,
            "line": line,
            "timestamp": _now_iso(),
        }
        if detail:
            msg["detail"] = detail
        await self._send(msg)

    def _task_emit(self, task_id: str, slot: _AgentSlot):
        """Return an emit function scoped to a task and agent slot."""

        async def _emit_task(line: str, detail: dict | None = None) -> None:
            msg: dict = {
                "type": "terminal-output",
                "agentId": slot.agent_id,
                "taskId": task_id,
                "line": line,
                "timestamp": _now_iso(),
            }
            if detail:
                msg["detail"] = detail
            await self._send(msg)
            # Emit a granular agent-state update when activity changes
            if detail:
                new_activity = detail.get("activity")
                if new_activity and new_activity != slot.activity:
                    slot.activity = new_activity
                    await self._send(
                        {
                            "type": "agent-state",
                            "agentId": slot.agent_id,
                            "state": slot.state,
                            "activity": new_activity,
                        }
                    )

        return _emit_task

    async def _set_state(self, state: str, slot: _AgentSlot) -> None:
        slot.state = state
        if state != "working":
            slot.activity = None
        await self._send(
            {
                "type": "agent-state",
                "agentId": slot.agent_id,
                "state": state,
                "activity": slot.activity,
            }
        )

    async def _join(self) -> None:
        hostname = socket.gethostname()
        host_prefix = hostname[:3].upper()
        raw = (self.cfg.worker_id or "")[2:].upper()
        split = 2 + sum(ord(c) for c in raw) % 3
        droid = f"{raw[:split]}-{raw[split:]}"
        name = self.cfg.worker_name or f"{host_prefix}/{droid}"
        for slot in self.slots:
            await self._send(
                {
                    "type": "join",
                    "agentId": slot.agent_id,
                    "agentName": name,
                    "agentType": "worker",
                    "workerId": self.cfg.worker_id,
                }
            )
        await self._send(
            {
                "type": "worker-register",
                "workerId": self.cfg.worker_id,
                "repos": self.cfg.repos,
            }
        )

    async def _on_ws_reconnect(self) -> None:
        """Re-announce ourselves after the WebSocket reconnects.

        The backend marks every joined agent offline when the socket closes, so
        without this the frontend sees the worker as offline forever even though
        the worker is back online and listening.
        """
        if not self._joined:
            # Auth hasn't completed yet — don't expose agents to the backend.
            logger.info("WebSocket reconnected during pre-auth phase; skipping join")
            return
        logger.info("WebSocket reconnected — re-sending join and agent states")
        await self._join()
        # `join` resets each agent to idle in the backend. Re-send the actual
        # state so a mid-task reconnect doesn't show us as idle while we work.
        for slot in self.slots:
            if slot.state and slot.state != "idle":
                await self._send(
                    {
                        "type": "agent-state",
                        "agentId": slot.agent_id,
                        "state": slot.state,
                    }
                )

    async def _task_update(self, task_id: str, **fields: object) -> None:
        await self._send(
            {
                "type": "task-update",
                "workerId": self.cfg.worker_id,
                "taskId": task_id,
                **fields,
            }
        )

    async def _initiate_shutdown(self, reason: str) -> None:
        """Begin a graceful shutdown.

        Idle agents wake up and exit; busy agents finish their current claude
        run and skip the follow-up window. Safe to call multiple times.
        """
        if self._shutdown_event.is_set():
            return
        self._shutdown_event.set()
        logger.info("Graceful shutdown initiated: %s", reason)
        try:
            await self._emit(
                f"[worker] Shutdown requested ({reason}). Idle agents stopping; "
                "busy agents will finish their current task."
            )
        except Exception as exc:
            logger.debug("emit during shutdown failed (ignored): %s", exc)
        # Wake idle agents waiting on task_queue.get()
        for _ in self.slots:
            self.task_queue.put_nowait(_SHUTDOWN_SENTINEL)
        # Wake any agents currently parked in their follow-up window.
        for slot in self.slots:
            tid = slot.current_task_id
            if not tid:
                continue
            q = self._followup_queues.get(tid)
            if q is not None:
                try:
                    q.put_nowait(None)  # None = finalize
                except asyncio.QueueFull:
                    pass

    def _install_signal_handlers(self) -> None:
        """Wire SIGINT/SIGTERM to graceful shutdown. Second signal force-exits."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except NotImplementedError:
                logger.debug("Signal handler for %s not available on this platform", sig)

    def _on_signal(self, sig: signal.Signals) -> None:
        loop = asyncio.get_running_loop()
        if self._shutdown_event.is_set():
            # User pressed Ctrl-C twice — fall back to default handler.
            logger.warning("Received %s during shutdown — forcing exit", sig.name)
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass
            os.kill(os.getpid(), sig)
            return
        logger.info("Received %s — initiating graceful shutdown (signal again to force)", sig.name)
        loop.create_task(self._initiate_shutdown(f"signal {sig.name}"))

    # Interval between application-level pings sent to the backend. Three
    # missed heartbeats (~75s) is enough to trip the backend's 90s sweeper.
    HEARTBEAT_INTERVAL_SECONDS: float = 25.0

    async def _heartbeat(self) -> None:
        """Send an application-level ping to the backend on a steady cadence.

        The websockets library handles transport-level ping/pong on its own,
        but the backend can't see those frames — its liveness tracking runs
        on application messages. Without this loop a worker that finishes
        all its tasks would go silent and the sweeper would mark it offline
        even though the connection is healthy.
        """
        while not self._shutdown_event.is_set():
            try:
                await self._send(
                    {
                        "type": "ping",
                        "workerId": self.cfg.worker_id,
                        "timestamp": _now_iso(),
                    }
                )
            except Exception as exc:
                logger.debug("Heartbeat send failed (ignored): %s", exc)
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=self.HEARTBEAT_INTERVAL_SECONDS
                )
                return  # shutdown event fired
            except TimeoutError:
                continue

    async def _notify_offline(self) -> None:
        """Send an explicit worker-disconnect message before the WebSocket closes.

        Called from the run() finally block inside an anyio.CancelScope(shield=True)
        so the message is delivered even when the task is being cancelled.
        """
        try:
            await self._send(
                {
                    "type": "worker-disconnect",
                    "workerId": self.cfg.worker_id,
                }
            )
        except Exception as exc:
            logger.debug("worker-disconnect send failed (ignored): %s", exc)

    # ------------------------------------------------------------------ Main loop
    async def run(self) -> None:
        logger.info(
            "Worker starting: guild=%s backend=%s repos=%s worker_id=%s",
            self.cfg.guild_id,
            self.cfg.backend_url,
            self.cfg.repos,
            self.cfg.worker_id or "<unregistered>",
        )
        logger.info(
            "Paths: repos_dir=%s work_dir=%s pull_interval=%.1fs max_turns=%d"
            " max_agents=%d claude=%s codex=%s pi=%s",
            self.cfg.repos_dir,
            self.cfg.work_dir,
            self.cfg.pull_interval,
            self.cfg.claude_max_turns,
            self.cfg.max_agents,
            self.cfg.claude_path,
            self.cfg.codex_path,
            self.cfg.pi_path,
        )

        self._install_signal_handlers()

        await self._register()
        assert self.cfg.worker_id, "worker_id must be set after registration"

        await self._fetch_github_token_if_needed()

        logger.info("Connecting to backend WebSocket at %s", self.cfg.ws_url)
        self.ws.on_reconnect = self._on_ws_reconnect
        await self.ws.connect()

        # Start listener before auth so it can relay auth codes from the UI.
        # _join() is intentionally delayed until after auth — agents must not
        # be visible to the foreman until Claude is ready to accept tasks.
        listener = asyncio.create_task(self._listen())
        await self.auth.check()

        await self._join()
        self._joined = True
        logger.info(
            "Joined guild %s — worker_id=%s agents=%d",
            self.cfg.guild_id,
            self.cfg.worker_id,
            len(self.slots),
        )
        await self._emit("[worker] Online. Watching for tasks.")
        for slot in self.slots:
            await self._set_state("idle", slot)

        initial = await self._fetch_pending_tasks()
        logger.info("Initial pending-task fetch: %d task(s)", len(initial))
        for task in initial:
            logger.info("Queuing task %s: %s", task.get("id"), task.get("description", "")[:80])
            self._known_task_ids.add(task["id"])
            await self.task_queue.put(task)

        runners = [asyncio.create_task(self._agent_loop(slot)) for slot in self.slots]
        puller = asyncio.create_task(self._idle_puller())
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            # Wait for either: all runners exit (graceful shutdown), or one of
            # the auxiliary tasks crashes (unexpected).
            done, _pending = await asyncio.wait(
                [listener, puller, heartbeat, *runners],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Surface any non-cancellation exception that fired first.
            first_exc: BaseException | None = None
            for t in done:
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    first_exc = exc
                    break

            # Make sure shutdown is signalled so the rest of the loops drain.
            if not self._shutdown_event.is_set():
                reason = "task crashed" if first_exc else "task exited"
                await self._initiate_shutdown(reason)

            # Stop the auxiliary tasks; the agent loops drain themselves.
            listener.cancel()
            puller.cancel()
            heartbeat.cancel()

            await asyncio.gather(listener, puller, heartbeat, *runners, return_exceptions=True)

            if first_exc is not None:
                raise first_exc
        finally:
            with anyio.CancelScope(shield=True):
                await self._notify_offline()
            logger.info("Worker shutting down; closing WebSocket")
            await self.ws.close()

    # ------------------------------------------------------------------ Listener
    async def _listen(self) -> None:
        logger.info("Listener started")
        async for msg in self.ws.messages():
            mtype = msg.get("type")
            logger.debug("WS message: type=%s keys=%s", mtype, list(msg.keys()))

            if mtype == "pong":
                # Heartbeat reply from the backend; nothing to do.
                continue

            if mtype == "task-assigned":
                if msg.get("workerId") != self.cfg.worker_id:
                    continue
                task_id = msg.get("taskId")
                if not task_id or task_id in self._known_task_ids:
                    continue
                logger.info("Task assigned: %s — %s", task_id, (msg.get("description") or "")[:80])
                self._known_task_ids.add(task_id)
                await self.task_queue.put(
                    {
                        "id": task_id,
                        "worker_id": self.cfg.worker_id,
                        "guild_id": self.cfg.guild_id,
                        "name": msg.get("name", ""),
                        "description": msg.get("description", ""),
                        "tool": msg.get("tool", "claude"),
                        "issue_number": msg.get("issueNumber"),
                        "issue_repo": msg.get("issueRepo"),
                    }
                )

            elif mtype == "worker-message":
                if msg.get("workerId") != self.cfg.worker_id:
                    continue
                text = msg.get("message", "")
                if text:
                    # During Claude login the auth queue is open; treat the
                    # message as the auth code so the foreman can relay it.
                    if await self.auth.deliver_code(text):
                        logger.info(
                            "Auth code received via worker-message (login flow) len=%d", len(text)
                        )
                        await self._emit("[worker] Auth code received and forwarded to login flow")
                    else:
                        active = next((s for s in self.slots if s.current_claude), None)
                        if active:
                            delivered = await active.current_claude.send_message(text)
                            if delivered:
                                await self._emit(f"[worker] Injected: {text[:80]}")
                            else:
                                logger.warning("Failed to inject message (stdin closed?)")
                        else:
                            logger.debug("worker-message: no claude running; dropping")

            elif mtype == "worker-auth-response":
                msg_worker_id = msg.get("workerId")
                logger.info(
                    "worker-auth-response received: msg_workerId=%s our_workerId=%s code_len=%d queue_open=%s",
                    msg_worker_id,
                    self.cfg.worker_id,
                    len(msg.get("code", "")),
                    self.auth.auth_code_queue is not None,
                )
                if msg_worker_id != self.cfg.worker_id:
                    logger.warning("worker-auth-response workerId mismatch — ignoring")
                    continue
                code = msg.get("code", "")
                if not code:
                    logger.warning("worker-auth-response has empty code — ignoring")
                    continue
                if await self.auth.deliver_code(code):
                    logger.info("Auth code queued (len=%d)", len(code))
                else:
                    logger.warning(
                        "worker-auth-response received but no auth in progress (queue is None)"
                    )

            elif mtype == "task-followup":
                if msg.get("workerId") != self.cfg.worker_id:
                    continue
                task_id = msg.get("taskId")
                instructions = msg.get("instructions", "")
                q = self._followup_queues.get(task_id)
                if q is not None:
                    await q.put(instructions)
                    logger.info("Follow-up queued for task %s: %s", task_id, instructions[:80])
                else:
                    logger.warning(
                        "task-followup for %s but no queue (task may have ended)", task_id
                    )

            elif mtype == "task-finalize":
                if msg.get("workerId") != self.cfg.worker_id:
                    continue
                task_id = msg.get("taskId")
                q = self._followup_queues.get(task_id)
                if q is not None:
                    await q.put(None)  # None = finalize signal
                    logger.info("Finalize signal for task %s", task_id)
                else:
                    logger.debug("task-finalize for %s but no queue", task_id)

            elif mtype == "task-cancel":
                if msg.get("workerId") != self.cfg.worker_id:
                    continue
                task_id = msg.get("taskId")
                if not task_id:
                    continue
                logger.info("Cancel signal for task %s", task_id)
                self._cancelled_tasks.add(task_id)
                # Kill subprocess if this task is currently running
                active = next((s for s in self.slots if s.current_task_id == task_id), None)
                if active and active.current_claude:
                    await active.current_claude.terminate()
                    logger.info("Terminated subprocess for cancelled task %s", task_id)
                # Unblock awaiting-review loop if the task is waiting for follow-up
                q = self._followup_queues.get(task_id)
                if q is not None:
                    await q.put(_CANCEL_SENTINEL)
                    logger.info("Signalled followup queue for cancelled task %s", task_id)

            elif mtype == "worker-shutdown":
                # Targeted at a specific worker, or broadcast (no workerId) to all.
                target = msg.get("workerId")
                if target and target != self.cfg.worker_id:
                    continue
                await self._initiate_shutdown("worker-shutdown message")

            elif mtype == "task-redirect":
                if msg.get("workerId") != self.cfg.worker_id:
                    continue
                task_id = msg.get("taskId")
                instructions = msg.get("instructions", "")
                if not task_id or not instructions:
                    continue
                logger.info("Redirect for task %s: %s", task_id, instructions[:80])
                active = next((s for s in self.slots if s.current_task_id == task_id), None)
                if active and active.current_claude:
                    # Subprocess is running — terminate it, queue redirect instructions
                    await active.current_claude.terminate()
                    logger.info("Terminated subprocess for redirect of task %s", task_id)
                    rq = self._redirect_queues.get(task_id)
                    if rq is not None:
                        await rq.put(instructions)
                else:
                    # No subprocess running — task is awaiting-review; use followup queue
                    fq = self._followup_queues.get(task_id)
                    if fq is not None:
                        await fq.put(instructions)
                        logger.info("Redirect queued as followup for task %s", task_id)
                    else:
                        logger.warning(
                            "task-redirect for %s: no active subprocess or followup queue", task_id
                        )

    # ------------------------------------------------------------------ Idle puller
    async def _idle_puller(self) -> None:
        logger.info("Idle puller started (interval=%.1fs)", self.cfg.pull_interval)
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.cfg.pull_interval,
                )
                break  # shutdown event fired
            except TimeoutError:
                pass  # normal interval elapsed
            try:
                pending = await self._fetch_pending_tasks()
                new_count = 0
                for task in pending:
                    if task["id"] in self._known_task_ids:
                        continue
                    new_count += 1
                    logger.info("Picked up missed task %s via poll", task["id"])
                    self._known_task_ids.add(task["id"])
                    await self.task_queue.put(task)
                logger.debug("Poll: %d known, %d new", len(pending), new_count)
            except Exception as exc:
                logger.warning("Pending-task poll failed: %s", exc)
            all_idle = all(s.current_claude is None for s in self.slots)
            if self.task_queue.empty() and all_idle and self.cfg.repos:
                await git_ops.pull_repos(
                    self.cfg.repos_dir, self.cfg.repos, self.cfg.github_token, self._emit
                )

    # ------------------------------------------------------------------ Agent loop
    async def _agent_loop(self, slot: _AgentSlot) -> None:
        logger.info("Agent loop started for %s", slot.agent_id)
        while True:
            task = await self.task_queue.get()
            if task is _SHUTDOWN_SENTINEL:
                logger.info("Agent %s: shutdown sentinel received; exiting", slot.agent_id)
                break
            task_id = task.get("id")
            if task_id in self._cancelled_tasks:
                logger.info("Skipping cancelled task %s", task_id)
                continue
            logger.info(
                "Dequeued task %s (agent=%s queue depth %d): %s",
                task_id,
                slot.agent_id,
                self.task_queue.qsize(),
                (task.get("description") or "")[:80],
            )
            slot.current_task_id = task_id
            try:
                await self._execute_task(task, slot)
            except Exception as exc:
                logger.exception("Task %s crashed: %s", task_id, exc)
                await self._emit(f"[worker] ✗ Internal error on task {task_id}: {exc}")
                await self._task_update(task["id"], state="failed", finishedAt=_now_iso())
            finally:
                slot.current_task_id = None
        try:
            await self._set_state("offline", slot)
        except Exception as exc:
            logger.debug("Failed to send offline state for %s: %s", slot.agent_id, exc)
        logger.info("Agent loop exited for %s", slot.agent_id)

    # ------------------------------------------------------------------ Execution
    async def _run_with_redirects(
        self,
        *,
        task_id: str,
        initial_desc: str,
        runner,
        on_run_done,
        redirect_q: asyncio.Queue,
        emit,
        redirect_prefix: str,
        on_redirect=None,
        log_label: str,
    ) -> tuple[bool, str, str, bool]:
        """Run *runner*(desc); on a queued redirect, re-run with the new desc.

        Loops until the redirect queue is empty, the task is cancelled, or
        shutdown is requested. *on_run_done* is invoked after each runner
        invocation (used to capture the claude session id and clear the slot).
        Returns ``(success, stop_reason, last_text, cancelled)``.
        """
        current = initial_desc
        while True:
            success, stop_reason, last_msg = await runner(current)
            on_run_done()
            logger.info(
                "Task %s: %s done success=%s stop=%s",
                task_id,
                log_label,
                success,
                stop_reason,
            )
            if task_id in self._cancelled_tasks:
                return success, stop_reason, last_msg, True
            try:
                redir = redirect_q.get_nowait()
            except asyncio.QueueEmpty:
                redir = None
            if redir is not None and not self._shutdown_event.is_set():
                await emit(f"{redirect_prefix}: {redir[:120]}")
                current = redir
                if on_redirect is not None:
                    await on_redirect()
                continue
            return success, stop_reason, last_msg, False

    async def _execute_task(self, task: dict, slot: _AgentSlot) -> None:
        task_id = task["id"]
        desc = task.get("description") or ""
        token = self.cfg.github_token
        repos = self.cfg.repos

        logger.info("Executing task %s (agent=%s): %s", task_id, slot.agent_id, desc[:120])
        await self._set_state("working", slot)
        await self._task_update(task_id, state="working")

        name = task.get("name") or desc
        branch = f"claude/{_slug(name)}-{task_id[:6]}"
        work_dir = os.path.join(self.cfg.work_dir, self.cfg.guild_id, self.cfg.worker_id, task_id)
        logger.info("Task %s branch=%s work_dir=%s", task_id, branch, work_dir)
        os.makedirs(work_dir, exist_ok=True)

        emit = self._task_emit(task_id, slot)
        await emit(f"[worker] Task: {desc}")
        await emit(f"[worker] Branch: {branch}")

        worktree_entries: list[tuple[str, str, str]] = []
        primary_wt: str | None = None

        for repo_full in repos:
            logger.info("Task %s: preparing repo %s", task_id, repo_full)
            await emit(f"[worker] Preparing {repo_full}...")
            repo_path = await git_ops.ensure_repo(self.cfg.repos_dir, repo_full, token)
            if not repo_path:
                logger.error("Task %s: clone/fetch failed for %s", task_id, repo_full)
                await emit(f"[worker] ✗ Clone failed: {repo_full}")
                continue
            repo_name = repo_full.split("/")[-1]
            wt_path = os.path.join(work_dir, repo_name)
            logger.info("Task %s: creating worktree %s on branch %s", task_id, wt_path, branch)
            if await git_ops.create_worktree(repo_path, wt_path, branch):
                logger.info("Task %s: worktree ready at %s", task_id, wt_path)
                worktree_entries.append((repo_full, repo_path, wt_path))
                if primary_wt is None:
                    primary_wt = wt_path
            else:
                logger.error("Task %s: worktree failed for %s", task_id, repo_full)
                await emit(f"[worker] ✗ Worktree failed: {repo_full}")

        if not primary_wt:
            logger.error("Task %s: no worktrees created — aborting", task_id)
            await emit("[worker] ✗ No worktrees — aborting.")
            await self._task_update(task_id, state="failed", finishedAt=_now_iso())
            await self._set_state("error", slot)
            return

        await self._task_update(task_id, branch=branch, worktreePath=primary_wt)

        tool = (task.get("tool") or "claude").lower()

        # Tracks the last claude session ID so redirects can --resume with full context
        resume_session_id: str | None = None

        # Queue for redirect instructions; listener puts new instructions here after SIGTERM
        redirect_q: asyncio.Queue = asyncio.Queue()
        self._redirect_queues[task_id] = redirect_q

        def _capture_session_and_clear() -> None:
            """Save session_id from the just-finished ClaudeProcess, then clear the slot."""
            nonlocal resume_session_id
            if slot.current_claude is not None:
                if slot.current_claude.session_id:
                    resume_session_id = slot.current_claude.session_id
                slot.current_claude = None

        def _on_proc(proc: claude_runner.ClaudeProcess) -> None:
            slot.current_claude = proc

        try:
            # ── Main execution with redirect loop ──────────────────────────
            current_desc = desc
            if tool == "claude":
                pr_title = (task.get("name") or desc)[:72].replace('"', "'")
                closes = f" Closes #{task['issue_number']}" if task.get("issue_number") else ""
                current_desc = (
                    f"{desc}\n\n"
                    f"After completing your changes, commit, push, and open a PR:\n"
                    f'  git add -A && git commit -m "<concise commit message>"\n'
                    f"  git push origin {branch}\n"
                    f'  gh pr create --title "{pr_title}" --body "<summary of changes>{closes}"\n'
                )

            async def _run_primary(d: str) -> tuple[bool, str, str]:
                if tool == "codex":
                    logger.info("Task %s: launching codex in %s", task_id, primary_wt)
                    return await codex_runner.run_codex_auto(
                        d,
                        primary_wt,
                        emit=emit,
                        codex_path=self.cfg.codex_path,
                    )
                if tool == "pi":
                    logger.info("Task %s: launching pi in %s", task_id, primary_wt)
                    return await pi_runner.run_pi_auto(
                        d,
                        primary_wt,
                        emit=emit,
                        pi_path=self.cfg.pi_path,
                    )
                logger.info(
                    "Task %s: launching claude in %s (max_turns=%d, resume=%s)",
                    task_id,
                    primary_wt,
                    self.cfg.claude_max_turns,
                    resume_session_id,
                )
                return await claude_runner.run_claude_auto(
                    d,
                    primary_wt,
                    max_turns=self.cfg.claude_max_turns,
                    emit=emit,
                    on_proc=_on_proc,
                    claude_path=self.cfg.claude_path,
                    resume_session_id=resume_session_id,
                )

            async def _on_primary_redirect() -> None:
                await self._task_update(task_id, state="working")

            success, stop_reason, last_msg, cancelled = await self._run_with_redirects(
                task_id=task_id,
                initial_desc=current_desc,
                runner=_run_primary,
                on_run_done=_capture_session_and_clear,
                redirect_q=redirect_q,
                emit=emit,
                redirect_prefix="[worker] ↩ Redirected",
                on_redirect=_on_primary_redirect,
                log_label="run",
            )
            if cancelled:
                await emit("[worker] Task cancelled.")
                await self._task_update(task_id, state="cancelled", finishedAt=_now_iso())
                await self._set_state("idle", slot)
                return

            logger.info("Task %s: final success=%s stop_reason=%s", task_id, success, stop_reason)

            # Push the branch regardless of outcome so partial work is visible
            # and a follow-up run can build on it.
            push_ok = await github_pr.push_branch(
                branch=branch,
                worktree_path=primary_wt,
                emit=emit,
            )
            pr_url: str | None = None

            if success:
                pr_url = await github_pr.ensure_pr(
                    task=task,
                    branch=branch,
                    worktree_path=primary_wt,
                    token=token,
                    pushed=push_ok,
                    emit=emit,
                )
                logger.info("Task %s: pr_url=%s", task_id, pr_url)

                await self._task_update(
                    task_id,
                    branch=branch,
                    worktreePath=primary_wt,
                    state="awaiting-review",
                )
                await self._send(
                    {
                        "type": "task-complete",
                        "workerId": self.cfg.worker_id,
                        "taskId": task_id,
                        "branch": branch,
                        "description": desc,
                        "prUrl": pr_url or "",
                        "sessionId": resume_session_id or "",
                        "lastText": last_msg,
                    }
                )
                await self._set_state("awaiting-review", slot)
            else:
                logger.warning("Task %s failed: %s", task_id, stop_reason)
                # Don't mark finishedAt yet — the foreman may send a follow-up
                # that resumes the same worktree/session.
                await self._task_update(
                    task_id,
                    branch=branch,
                    worktreePath=primary_wt,
                    state="failed",
                )
                await self._send(
                    {
                        "type": "needs-input",
                        "workerId": self.cfg.worker_id,
                        "taskId": task_id,
                        "description": desc,
                        "branch": branch,
                        "sessionId": resume_session_id or "",
                        "stopReason": stop_reason,
                        "lastMessage": last_msg,
                    }
                )
                await self._set_state("error", slot)

            # ── Follow-up loop — runs after both success and failure so the
            # foreman can resume the same worktree (with --resume) for a while
            # before we tear it down. ─────────────────────────────────────────
            followup_q: asyncio.Queue = asyncio.Queue()
            self._followup_queues[task_id] = followup_q
            followup_cancelled = False
            last_success = success
            try:
                if self._shutdown_event.is_set():
                    await emit("[worker] Shutdown in progress — skipping follow-up window.")
                while not self._shutdown_event.is_set():
                    try:
                        instructions = await asyncio.wait_for(followup_q.get(), timeout=300.0)
                    except TimeoutError:
                        await emit("[worker] Follow-up window expired — finalizing.")
                        await self._send(
                            {
                                "type": "task-complete",
                                "workerId": self.cfg.worker_id,
                                "taskId": task_id,
                                "branch": branch,
                                "description": desc,
                                "prUrl": pr_url or "",
                                "sessionId": resume_session_id or "",
                                "finalizedBy": "timeout",
                            }
                        )
                        break
                    if instructions is _CANCEL_SENTINEL:
                        await emit("[worker] Task cancelled.")
                        followup_cancelled = True
                        break
                    if instructions is None:
                        await emit("[worker] Task finalized by foreman.")
                        break

                    await self._set_state("working", slot)
                    await self._task_update(task_id, state="working")
                    await emit(f"[worker] Follow-up: {instructions[:120]}")

                    async def _run_followup(d: str) -> tuple[bool, str, str]:
                        return await claude_runner.run_claude_auto(
                            d,
                            primary_wt,
                            max_turns=self.cfg.claude_max_turns,
                            emit=emit,
                            on_proc=_on_proc,
                            claude_path=self.cfg.claude_path,
                            resume_session_id=resume_session_id,
                        )

                    fu_ok, fu_reason, _, fu_cancelled = await self._run_with_redirects(
                        task_id=task_id,
                        initial_desc=instructions,
                        runner=_run_followup,
                        on_run_done=_capture_session_and_clear,
                        redirect_q=redirect_q,
                        emit=emit,
                        redirect_prefix="[worker] ↩ Redirected during follow-up",
                        log_label="follow-up",
                    )
                    if fu_cancelled:
                        followup_cancelled = True
                        break

                    last_success = fu_ok

                    if fu_ok:
                        push_ok = await github_pr.push_branch(
                            branch=branch,
                            worktree_path=primary_wt,
                            emit=emit,
                        )
                        # Originally-failed tasks never opened a PR; do it now
                        # that we have something worth reviewing.
                        if not pr_url:
                            pr_url = await github_pr.ensure_pr(
                                task=task,
                                branch=branch,
                                worktree_path=primary_wt,
                                token=token,
                                pushed=push_ok,
                                emit=emit,
                            )

                    await self._send(
                        {
                            "type": "task-followup-done",
                            "workerId": self.cfg.worker_id,
                            "taskId": task_id,
                            "success": fu_ok,
                            "stopReason": fu_reason,
                            "branch": branch,
                            "sessionId": resume_session_id or "",
                            "prUrl": pr_url or "",
                        }
                    )
                    await self._task_update(
                        task_id,
                        state="awaiting-review" if fu_ok else "failed",
                    )
                    await self._set_state(
                        "awaiting-review" if fu_ok else "error",
                        slot,
                    )
            finally:
                self._followup_queues.pop(task_id, None)

            if followup_cancelled:
                await self._task_update(task_id, state="cancelled", finishedAt=_now_iso())
                await self._set_state("idle", slot)
                return

            final_state = "done" if last_success else "failed"
            await self._task_update(task_id, state=final_state, finishedAt=_now_iso())
            await self._set_state("idle", slot)

        finally:
            self._redirect_queues.pop(task_id, None)
            logger.info("Task %s: cleaning up %d worktree(s)", task_id, len(worktree_entries))
            for _repo_full, repo_path, wt_path in worktree_entries:
                await git_ops.remove_worktree(repo_path, wt_path)
