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

import httpx

from . import claude_runner, codex_runner, pi_runner, config as config_mod, git_ops, github_pr
from .ws_client import WSClient

logger = logging.getLogger(__name__)


def _gen_task_id() -> str:
    return "t-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text[:40].lower()).strip("-")


class Worker:
    def __init__(self, cfg: config_mod.Config) -> None:
        self.cfg = cfg
        self.ws = WSClient(cfg.ws_url)
        self.task_queue: asyncio.Queue[dict] = asyncio.Queue()
        self.current_claude: Optional[claude_runner.ClaudeProcess] = None
        self._known_task_ids: set[str] = set()

    # ------------------------------------------------------------------ HTTP
    async def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.cfg.http_url, timeout=30.0)

    async def _register_if_needed(self) -> None:
        if self.cfg.worker_id:
            return
        async with await self._http() as client:
            resp = await client.post(
                f"/guilds/{self.cfg.guild_id}/workers",
                json={"repos": self.cfg.repos, "github_token": None},
            )
            resp.raise_for_status()
            wid = resp.json()["id"]
        config_mod.save_worker_id(self.cfg, wid)
        logger.info("Registered new worker id %s", wid)

    async def _fetch_pending_tasks(self) -> list[dict]:
        async with await self._http() as client:
            resp = await client.get(
                f"/guilds/{self.cfg.guild_id}/workers/{self.cfg.worker_id}/tasks"
            )
            resp.raise_for_status()
            tasks = resp.json()
        return [t for t in tasks if t.get("state") in ("pending", "working")]

    # ------------------------------------------------------------------ WS
    async def _send(self, payload: dict) -> None:
        await self.ws.send(payload)

    async def _emit(self, line: str) -> None:
        await self._send({
            "type": "terminal-output",
            "agentId": self.cfg.worker_id,
            "line": line,
            "timestamp": _now_iso(),
        })

    async def _set_state(self, state: str) -> None:
        await self._send({
            "type": "agent-state",
            "agentId": self.cfg.worker_id,
            "state": state,
        })

    async def _join(self) -> None:
        raw = self.cfg.worker_id[2:].upper()
        split = 2 + sum(ord(c) for c in raw) % 3
        default_name = f"{raw[:split]}-{raw[split:]}"
        name = self.cfg.worker_name or default_name
        await self._send({
            "type": "join",
            "agentId": self.cfg.worker_id,
            "agentName": name,
            "agentType": "worker",
        })
        await self._send({
            "type": "worker-register",
            "workerId": self.cfg.worker_id,
            "repos": self.cfg.repos,
        })

    async def _task_update(self, task_id: str, **fields: object) -> None:
        await self._send({
            "type": "task-update",
            "workerId": self.cfg.worker_id,
            "taskId": task_id,
            **fields,
        })

    # ------------------------------------------------------------------ Loop
    async def run(self) -> None:
        logger.info(
            "Worker starting: guild=%s backend=%s repos=%s worker_id=%s",
            self.cfg.guild_id, self.cfg.backend_url, self.cfg.repos,
            self.cfg.worker_id or "<unregistered>",
        )
        logger.info(
            "Paths: repos_dir=%s work_dir=%s pull_interval=%.1fs max_turns=%d"
            " claude=%s codex=%s pi=%s",
            self.cfg.repos_dir, self.cfg.work_dir,
            self.cfg.pull_interval, self.cfg.claude_max_turns,
            self.cfg.claude_path, self.cfg.codex_path, self.cfg.pi_path,
        )

        await self._register_if_needed()
        assert self.cfg.worker_id, "worker_id must be set after registration"

        logger.info("Connecting to backend WebSocket at %s", self.cfg.ws_url)
        await self.ws.connect()
        await self._join()
        logger.info("Joined guild %s as worker %s", self.cfg.guild_id, self.cfg.worker_id)
        await self._emit("[worker] Online. Watching for tasks.")
        await self._set_state("idle")

        initial = await self._fetch_pending_tasks()
        logger.info("Initial pending-task fetch returned %d task(s)", len(initial))
        for task in initial:
            logger.info("Queuing pending task %s: %s", task.get("id"), task.get("description", "")[:80])
            self._known_task_ids.add(task["id"])
            await self.task_queue.put(task)

        logger.info("Spawning listener, task runner, and idle poller; entering main loop")
        listener = asyncio.create_task(self._listen())
        runner = asyncio.create_task(self._task_runner())
        puller = asyncio.create_task(self._idle_puller())
        try:
            await asyncio.gather(listener, runner, puller)
        finally:
            logger.info("Worker shutting down; closing WebSocket")
            await self.ws.close()

    async def _listen(self) -> None:
        logger.info("Listener started; awaiting WebSocket messages")
        async for msg in self.ws.messages():
            mtype = msg.get("type")
            logger.debug("WS message: type=%s keys=%s", mtype, list(msg.keys()))
            if mtype == "task-assigned":
                if msg.get("workerId") != self.cfg.worker_id:
                    logger.debug("Ignoring task-assigned for other worker %s", msg.get("workerId"))
                    continue
                task_id = msg.get("taskId")
                if not task_id or task_id in self._known_task_ids:
                    logger.debug("Skipping already-known task %s", task_id)
                    continue
                logger.info(
                    "Task assigned: %s — %s",
                    task_id, (msg.get("description") or "")[:80],
                )
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
                if self.current_claude and text:
                    logger.info("Injecting message into running claude: %s", text[:80])
                    delivered = await self.current_claude.send_message(text)
                    if delivered:
                        await self._emit(f"[worker] Injected: {text[:80]}")
                    else:
                        logger.warning("Failed to inject message into claude (stdin closed?)")
                elif text:
                    logger.debug("worker-message received but no claude is running; dropping")

    async def _idle_puller(self) -> None:
        logger.info("Idle puller started (interval=%.1fs)", self.cfg.pull_interval)
        while True:
            await asyncio.sleep(self.cfg.pull_interval)
            # Catch tasks that were broadcast while we were briefly disconnected.
            try:
                logger.debug("Polling backend for pending tasks")
                pending = await self._fetch_pending_tasks()
                new_count = 0
                for task in pending:
                    if task["id"] in self._known_task_ids:
                        continue
                    new_count += 1
                    logger.info(
                        "Picked up missed task %s via poll: %s",
                        task["id"], (task.get("description") or "")[:80],
                    )
                    self._known_task_ids.add(task["id"])
                    await self.task_queue.put(task)
                logger.debug("Poll done: %d known, %d new", len(pending), new_count)
            except Exception as exc:  # pragma: no cover
                logger.warning("Pending-task poll failed: %s", exc)
            if self.task_queue.empty() and self.current_claude is None and self.cfg.repos:
                logger.debug("Idle: refreshing %d repo(s)", len(self.cfg.repos))
                await git_ops.pull_repos(
                    self.cfg.repos_dir, self.cfg.repos, self.cfg.github_token, self._emit
                )

    async def _task_runner(self) -> None:
        logger.info("Task runner started; waiting for queued tasks")
        while True:
            task = await self.task_queue.get()
            task_id = task.get("id")
            logger.info(
                "Dequeued task %s (queue depth now %d): %s",
                task_id, self.task_queue.qsize(),
                (task.get("description") or "")[:80],
            )
            try:
                await self._execute_task(task)
                logger.info("Task %s execution returned", task_id)
            except Exception as exc:  # pragma: no cover
                logger.exception("Task %s crashed: %s", task_id, exc)
                await self._emit(f"[worker] ✗ Internal error: {exc}")
                await self._task_update(task["id"], state="failed", finishedAt=_now_iso())
                await self._set_state("error")

    async def _execute_task(self, task: dict) -> None:
        task_id = task["id"]
        desc = task["description"]
        token = self.cfg.github_token
        repos = self.cfg.repos

        logger.info("Executing task %s: %s", task_id, desc[:120])
        await self._set_state("working")
        await self._task_update(task_id, state="working")

        branch = f"claude/{_slug(desc)}-{task_id[:6]}"
        work_dir = os.path.join(self.cfg.work_dir, self.cfg.guild_id, self.cfg.worker_id, task_id)
        logger.info("Task %s branch=%s work_dir=%s", task_id, branch, work_dir)
        os.makedirs(work_dir, exist_ok=True)

        await self._emit(f"[worker] Task: {desc}")
        await self._emit(f"[worker] Branch: {branch}")

        worktree_entries: list[tuple[str, str, str]] = []
        primary_wt: Optional[str] = None

        for repo_full in repos:
            logger.info("Task %s: preparing repo %s", task_id, repo_full)
            await self._emit(f"[worker] Preparing {repo_full}...")
            repo_path = await git_ops.ensure_repo(self.cfg.repos_dir, repo_full, token)
            if not repo_path:
                logger.error("Task %s: clone/fetch failed for %s", task_id, repo_full)
                await self._emit(f"[worker] ✗ Clone failed: {repo_full}")
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
                logger.error("Task %s: worktree creation failed for %s", task_id, repo_full)
                await self._emit(f"[worker] ✗ Worktree failed: {repo_full}")

        if not primary_wt:
            logger.error("Task %s: aborting — no worktrees were created", task_id)
            await self._emit("[worker] ✗ No worktrees — aborting.")
            await self._task_update(task_id, state="failed", finishedAt=_now_iso())
            await self._set_state("error")
            return

        await self._task_update(task_id, branch=branch, worktreePath=primary_wt)

        tool = (task.get("tool") or "claude").lower()

        def _on_proc(proc: claude_runner.ClaudeProcess) -> None:
            self.current_claude = proc

        if tool == "codex":
            logger.info("Task %s: launching codex in %s", task_id, primary_wt)
            success, stop_reason, last_msg = await codex_runner.run_codex_auto(
                desc,
                primary_wt,
                emit=self._emit,
                codex_path=self.cfg.codex_path,
            )
        elif tool == "pi":
            logger.info("Task %s: launching pi in %s", task_id, primary_wt)
            success, stop_reason, last_msg = await pi_runner.run_pi_auto(
                desc,
                primary_wt,
                emit=self._emit,
                pi_path=self.cfg.pi_path,
            )
        else:
            logger.info(
                "Task %s: launching claude in %s (max_turns=%d)",
                task_id, primary_wt, self.cfg.claude_max_turns,
            )
            success, stop_reason, last_msg = await claude_runner.run_claude_auto(
                desc,
                primary_wt,
                max_turns=self.cfg.claude_max_turns,
                emit=self._emit,
                on_proc=_on_proc,
                claude_path=self.cfg.claude_path,
            )
        self.current_claude = None
        finished_at = _now_iso()
        logger.info("Task %s: claude finished success=%s stop_reason=%s", task_id, success, stop_reason)

        if success:
            logger.info("Task %s: pushing branch %s", task_id, branch)
            await github_pr.push_branch(
                branch=branch,
                worktree_path=primary_wt,
                emit=self._emit,
            )
            logger.info("Task %s: done", task_id)
            await self._task_update(
                task_id, state="done", branch=branch, finishedAt=finished_at,
            )
            await self._send({
                "type": "task-complete",
                "workerId": self.cfg.worker_id,
                "taskId": task_id,
                "branch": branch,
                "description": desc,
            })
            await self._set_state("idle")
        else:
            logger.warning("Task %s: failed stop_reason=%s, requesting human input", task_id, stop_reason)
            await self._task_update(task_id, state="failed", finishedAt=finished_at)
            await self._set_state("error")
            await self._send({
                "type": "needs-input",
                "workerId": self.cfg.worker_id,
                "taskId": task_id,
                "description": desc,
                "stopReason": stop_reason,
                "lastMessage": last_msg,
            })

        logger.info("Task %s: cleaning up %d worktree(s)", task_id, len(worktree_entries))
        for _repo_full, repo_path, wt_path in worktree_entries:
            await git_ops.remove_worktree(repo_path, wt_path)
