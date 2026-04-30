"""Main worker loop: register, listen for tasks over WebSocket, execute, report."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import string
from datetime import datetime, timezone
from typing import Optional

import anyio
import httpx

from . import claude_runner, codex_runner, pi_runner, config as config_mod, git_ops, github_pr
from .ws_client import WSClient

logger = logging.getLogger(__name__)


def _gen_id(prefix: str) -> str:
    return prefix + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str, max_len: int = 45) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text[:max_len].lower()).strip("-")


_CANCEL_SENTINEL = object()  # placed in followup queue to signal task cancellation


class _AgentSlot:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.current_claude: Optional[claude_runner.ClaudeProcess] = None
        self.current_task_id: Optional[str] = None
        # Last state we told the backend about; resent on WS reconnect so the
        # backend (and frontend) don't show the agent stuck offline.
        self.state: str = "idle"


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
        # One slot per concurrent agent; each gets a stable ID for the guild.
        self.slots: list[_AgentSlot] = [
            _AgentSlot(_gen_id("a-")) for _ in range(cfg.max_agents)
        ]

    # ------------------------------------------------------------------ HTTP
    async def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.cfg.http_url, timeout=30.0)

    async def _register(self) -> None:
        async with await self._http() as client:
            resp = await client.post(
                f"/guilds/{self.cfg.guild_id}/workers",
                json={"repos": self.cfg.repos, "github_token": None},
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
        return _emit_task

    async def _set_state(self, state: str, slot: _AgentSlot) -> None:
        slot.state = state
        await self._send({
            "type": "agent-state",
            "agentId": slot.agent_id,
            "state": state,
        })

    async def _join(self) -> None:
        for slot in self.slots:
            raw = slot.agent_id[2:].upper()
            split = 2 + sum(ord(c) for c in raw) % 3
            name = self.cfg.worker_name or f"{raw[:split]}-{raw[split:]}"
            await self._send({
                "type": "join",
                "agentId": slot.agent_id,
                "agentName": name,
                "agentType": "worker",
                "workerId": self.cfg.worker_id,
            })
        await self._send({
            "type": "worker-register",
            "workerId": self.cfg.worker_id,
            "repos": self.cfg.repos,
        })

    async def _on_ws_reconnect(self) -> None:
        """Re-announce ourselves after the WebSocket reconnects.

        The backend marks every joined agent offline when the socket closes, so
        without this the frontend sees the worker as offline forever even though
        the worker is back online and listening.
        """
        logger.info("WebSocket reconnected — re-sending join and agent states")
        await self._join()
        # `join` resets each agent to idle in the backend. Re-send the actual
        # state so a mid-task reconnect doesn't show us as idle while we work.
        for slot in self.slots:
            if slot.state and slot.state != "idle":
                await self._send({
                    "type": "agent-state",
                    "agentId": slot.agent_id,
                    "state": slot.state,
                })

    async def _task_update(self, task_id: str, **fields: object) -> None:
        await self._send({
            "type": "task-update",
            "workerId": self.cfg.worker_id,
            "taskId": task_id,
            **fields,
        })

    async def _notify_offline(self) -> None:
        """Send an explicit worker-disconnect message before the WebSocket closes.

        Called from the run() finally block inside an anyio.CancelScope(shield=True)
        so the message is delivered even when the task is being cancelled.
        """
        try:
            await self._send({
                "type": "worker-disconnect",
                "workerId": self.cfg.worker_id,
            })
        except Exception as exc:
            logger.debug("worker-disconnect send failed (ignored): %s", exc)

    # ------------------------------------------------------------------ Main loop
    async def run(self) -> None:
        logger.info(
            "Worker starting: guild=%s backend=%s repos=%s worker_id=%s",
            self.cfg.guild_id, self.cfg.backend_url, self.cfg.repos,
            self.cfg.worker_id or "<unregistered>",
        )
        logger.info(
            "Paths: repos_dir=%s work_dir=%s pull_interval=%.1fs max_turns=%d"
            " max_agents=%d claude=%s codex=%s pi=%s",
            self.cfg.repos_dir, self.cfg.work_dir,
            self.cfg.pull_interval, self.cfg.claude_max_turns,
            self.cfg.max_agents,
            self.cfg.claude_path, self.cfg.codex_path, self.cfg.pi_path,
        )

        await self._register()
        assert self.cfg.worker_id, "worker_id must be set after registration"

        await self._fetch_github_token_if_needed()

        logger.info("Connecting to backend WebSocket at %s", self.cfg.ws_url)
        self.ws.on_reconnect = self._on_ws_reconnect
        await self.ws.connect()
        await self._join()
        logger.info(
            "Joined guild %s — worker_id=%s agents=%d",
            self.cfg.guild_id, self.cfg.worker_id, len(self.slots),
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

        listener = asyncio.create_task(self._listen())
        runners  = [asyncio.create_task(self._agent_loop(slot)) for slot in self.slots]
        puller   = asyncio.create_task(self._idle_puller())
        try:
            await asyncio.gather(listener, *runners, puller)
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

            if mtype == "task-assigned":
                if msg.get("workerId") != self.cfg.worker_id:
                    continue
                task_id = msg.get("taskId")
                if not task_id or task_id in self._known_task_ids:
                    continue
                logger.info("Task assigned: %s — %s", task_id, (msg.get("description") or "")[:80])
                self._known_task_ids.add(task_id)
                await self.task_queue.put({
                    "id": task_id,
                    "worker_id": self.cfg.worker_id,
                    "guild_id": self.cfg.guild_id,
                    "description": msg.get("description", ""),
                    "tool": msg.get("tool", "claude"),
                    "issue_number": msg.get("issueNumber"),
                    "issue_repo": msg.get("issueRepo"),
                })

            elif mtype == "worker-message":
                if msg.get("workerId") != self.cfg.worker_id:
                    continue
                text = msg.get("message", "")
                if text:
                    active = next((s for s in self.slots if s.current_claude), None)
                    if active:
                        delivered = await active.current_claude.send_message(text)
                        if delivered:
                            await self._emit(f"[worker] Injected: {text[:80]}")
                        else:
                            logger.warning("Failed to inject message (stdin closed?)")
                    else:
                        logger.debug("worker-message: no claude running; dropping")

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
                    logger.warning("task-followup for %s but no queue (task may have ended)", task_id)

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
                        logger.warning("task-redirect for %s: no active subprocess or followup queue", task_id)

    # ------------------------------------------------------------------ Idle puller
    async def _idle_puller(self) -> None:
        logger.info("Idle puller started (interval=%.1fs)", self.cfg.pull_interval)
        while True:
            await asyncio.sleep(self.cfg.pull_interval)
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
            task_id = task.get("id")
            if task_id in self._cancelled_tasks:
                logger.info("Skipping cancelled task %s", task_id)
                continue
            logger.info(
                "Dequeued task %s (agent=%s queue depth %d): %s",
                task_id, slot.agent_id, self.task_queue.qsize(),
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

    # ------------------------------------------------------------------ Execution
    async def _execute_task(self, task: dict, slot: _AgentSlot) -> None:
        task_id = task["id"]
        desc = task["description"]
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
        primary_wt: Optional[str] = None

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
        resume_session_id: Optional[str] = None

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
            success = False
            stop_reason = "no_events"
            last_msg = ""

            while True:
                if tool == "codex":
                    logger.info("Task %s: launching codex in %s", task_id, primary_wt)
                    success, stop_reason, last_msg = await codex_runner.run_codex_auto(
                        current_desc, primary_wt, emit=emit, codex_path=self.cfg.codex_path,
                    )
                elif tool == "pi":
                    logger.info("Task %s: launching pi in %s", task_id, primary_wt)
                    success, stop_reason, last_msg = await pi_runner.run_pi_auto(
                        current_desc, primary_wt, emit=emit, pi_path=self.cfg.pi_path,
                    )
                else:
                    logger.info(
                        "Task %s: launching claude in %s (max_turns=%d, resume=%s)",
                        task_id, primary_wt, self.cfg.claude_max_turns, resume_session_id,
                    )
                    success, stop_reason, last_msg = await claude_runner.run_claude_auto(
                        current_desc, primary_wt,
                        max_turns=self.cfg.claude_max_turns,
                        emit=emit,
                        on_proc=_on_proc,
                        claude_path=self.cfg.claude_path,
                        resume_session_id=resume_session_id,
                    )

                _capture_session_and_clear()
                logger.info(
                    "Task %s: run done success=%s stop=%s session=%s",
                    task_id, success, stop_reason, resume_session_id,
                )

                if task_id in self._cancelled_tasks:
                    await emit("[worker] Task cancelled.")
                    await self._task_update(task_id, state="cancelled", finishedAt=_now_iso())
                    await self._set_state("idle", slot)
                    return

                try:
                    redirect_instr = redirect_q.get_nowait()
                except asyncio.QueueEmpty:
                    redirect_instr = None

                if redirect_instr is not None:
                    await emit(f"[worker] ↩ Redirected: {redirect_instr[:120]}")
                    current_desc = redirect_instr
                    await self._task_update(task_id, state="working")
                    continue

                break  # normal exit from redirect loop

            logger.info("Task %s: final success=%s stop_reason=%s", task_id, success, stop_reason)

            # Push the branch regardless of outcome so partial work is visible
            # and a follow-up run can build on it.
            push_ok = await github_pr.push_branch(
                branch=branch, worktree_path=primary_wt, emit=emit,
            )
            pr_url: Optional[str] = None

            if success:
                if push_ok:
                    pr_url = await github_pr.open_pr(
                        task=task, branch=branch, worktree_path=primary_wt,
                        token=token, emit=emit,
                    )
                logger.info("Task %s: pushed pr_url=%s", task_id, pr_url)

                await self._task_update(
                    task_id, branch=branch, worktreePath=primary_wt, state="awaiting-review",
                )
                await self._send({
                    "type": "task-complete",
                    "workerId": self.cfg.worker_id,
                    "taskId": task_id,
                    "branch": branch,
                    "description": desc,
                    "prUrl": pr_url or "",
                    "sessionId": resume_session_id or "",
                })
                await self._set_state("awaiting-review", slot)
            else:
                logger.warning("Task %s failed: %s", task_id, stop_reason)
                # Don't mark finishedAt yet — the foreman may send a follow-up
                # that resumes the same worktree/session.
                await self._task_update(
                    task_id, branch=branch, worktreePath=primary_wt, state="failed",
                )
                await self._send({
                    "type": "needs-input",
                    "workerId": self.cfg.worker_id,
                    "taskId": task_id,
                    "description": desc,
                    "branch": branch,
                    "sessionId": resume_session_id or "",
                    "stopReason": stop_reason,
                    "lastMessage": last_msg,
                })
                await self._set_state("error", slot)

            # ── Follow-up loop — runs after both success and failure so the
            # foreman can resume the same worktree (with --resume) for a while
            # before we tear it down. ─────────────────────────────────────────
            followup_q: asyncio.Queue = asyncio.Queue()
            self._followup_queues[task_id] = followup_q
            followup_cancelled = False
            last_success = success
            try:
                while True:
                    try:
                        instructions = await asyncio.wait_for(
                            followup_q.get(), timeout=300.0
                        )
                    except asyncio.TimeoutError:
                        await emit("[worker] Follow-up window expired — finalizing.")
                        await self._send({
                            "type": "task-complete",
                            "workerId": self.cfg.worker_id,
                            "taskId": task_id,
                            "branch": branch,
                            "description": desc,
                            "prUrl": pr_url or "",
                            "sessionId": resume_session_id or "",
                            "finalizedBy": "timeout",
                        })
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

                    # Inner redirect loop for followup runs
                    current_fu_desc = instructions
                    fu_ok = False
                    fu_reason = "no_events"
                    while True:
                        fu_ok, fu_reason, _ = await claude_runner.run_claude_auto(
                            current_fu_desc, primary_wt,
                            max_turns=self.cfg.claude_max_turns,
                            emit=emit,
                            on_proc=_on_proc,
                            claude_path=self.cfg.claude_path,
                            resume_session_id=resume_session_id,
                        )
                        _capture_session_and_clear()
                        logger.info("Task %s follow-up: ok=%s reason=%s session=%s",
                                    task_id, fu_ok, fu_reason, resume_session_id)

                        if task_id in self._cancelled_tasks:
                            followup_cancelled = True
                            break

                        try:
                            redir = redirect_q.get_nowait()
                        except asyncio.QueueEmpty:
                            redir = None

                        if redir is not None:
                            await emit(f"[worker] ↩ Redirected during follow-up: {redir[:120]}")
                            current_fu_desc = redir
                            continue

                        break

                    if followup_cancelled:
                        break

                    last_success = fu_ok

                    if fu_ok:
                        await github_pr.push_branch(
                            branch=branch, worktree_path=primary_wt, emit=emit,
                        )
                        # Originally-failed tasks never opened a PR; do it now
                        # that we have something worth reviewing.
                        if not pr_url:
                            pr_url = await github_pr.open_pr(
                                task=task, branch=branch, worktree_path=primary_wt,
                                token=token, emit=emit,
                            )

                    await self._send({
                        "type": "task-followup-done",
                        "workerId": self.cfg.worker_id,
                        "taskId": task_id,
                        "success": fu_ok,
                        "stopReason": fu_reason,
                        "branch": branch,
                        "sessionId": resume_session_id or "",
                        "prUrl": pr_url or "",
                    })
                    await self._task_update(
                        task_id, state="awaiting-review" if fu_ok else "failed",
                    )
                    await self._set_state(
                        "awaiting-review" if fu_ok else "error", slot,
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
