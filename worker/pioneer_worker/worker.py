"""Main worker loop: register, listen for tasks over WebSocket, execute, report."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import re
import signal
import socket
import string
from datetime import UTC, datetime
from typing import cast

import anyio
import httpx

from . import (  # noqa: F401 - test patch compatibility
    claude_runner,
    codex_runner,
    git_ops,
    github_pr,
    pi_runner,
    s3_uploader,
    tool_installer,
)
from . import config as config_mod
from .control_api import ControlServer
from .runner_registry import build as build_runner_registry  # pyright: ignore[reportMissingImports]
from .runner_types import (  # pyright: ignore[reportMissingImports]
    ProcessHandle,
    RunRequest,
    RunResult,
    StopReason,
)
from .sleep_monitor import SystemSleepMonitor
from .ws_client import WSClient

logger = logging.getLogger(__name__)


def _gen_id(prefix: str) -> str:
    return prefix + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _droid_name(id: str) -> str:
    """Generate a droid-style designation like R2-D2 or BB-8, seeded from id."""

    split = 2 + sum(ord(c) for c in id) % 3
    return f"{id[2:][:split]}-{id[2:][split:]}".upper()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# Semantic line "levels" carried on terminal-output frames so the frontend can
# style log lines by type instead of sniffing text prefixes like "[worker]".
# Keep these in sync with the LogLevel union in frontend/src/types.ts.
LEVEL_INFO = "info"  # default — agent/Claude output, rendered as markdown
LEVEL_WORKER = "worker"  # worker-level status / lifecycle line
LEVEL_AUTH = "auth"  # Claude login / credential flow
LEVEL_CLAUDE = "claude"  # Claude runner framing (start / exit / stderr)
LEVEL_THINKING = "thinking"  # extended-thinking text


def _slug(text: str, max_len: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text[:max_len].lower()).strip("-")


_CANCEL_SENTINEL = object()  # placed in redirect queue to signal task cancellation
_SHUTDOWN_SENTINEL = object()  # placed in task queue to wake idle agents during shutdown

_PR_PHASES = frozenset({"execute", "followup"})


# Worktrees are kept around after a task completes so the foreman can send
# follow-ups without paying for a re-clone. They're swept at startup and on a
# steady cadence; anything older than this is fair game to remove.
WORKTREE_TTL_SECONDS = 24 * 60 * 60
WORKTREE_SWEEP_INTERVAL_SECONDS = 60 * 60

# How often to re-query the GitHub API for accessible repos. Keeps the worker's
# list current if new repos are added or permissions change without a restart.
REPO_REFRESH_INTERVAL_SECONDS = 20 * 60

# How often to re-fetch the worker's GitHub token from the backend. The
# backend prefers minting GitHub App installation tokens, which GitHub expires
# after an hour; a worker process is designed to run for days (see
# WORKTREE_TTL_SECONDS), so without a periodic re-fetch the cached token goes
# stale partway through the worker's life and every `gh` CLI call, idle repo
# pull, and repo-list refresh relying on it starts failing. Well under an
# hour to leave margin. Task-scoped push/PR tokens are unaffected — those are
# already re-fetched fresh per task by _task_github_token.
GITHUB_TOKEN_REFRESH_INTERVAL_SECONDS = 45 * 60

# Env vars that mean "claude has a way to authenticate". A direct key, an OAuth
# token, a proxy auth token, or a gateway flag that makes claude authenticate
# through the cloud provider's own credentials instead (this deployment runs
# claude on Bedrock, so the flag alone is a valid configuration). A tool with
# none of these and no logged-in CLI is dropped from the available list rather
# than launched per-task and failed.
_CLAUDE_CREDENTIAL_KEYS = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


class Agent:
    """An agent owned by a Worker.

    Each Worker creates a fixed pool of Agent instances at startup and keeps
    them for its entire lifetime — agents are born and die with the worker
    process.  The agent holds a live subprocess reference (``current_claude``)
    and the ID of the task it is currently executing.  ``current_task_id`` on
    an agent means *this task is running here right now*; it does not imply
    ownership — the task record itself lives in the backend and can be picked
    up by a different agent on the next run.
    """

    def __init__(self, *, id: str | None = None, name: str | None = None) -> None:
        self.id: str = id if id is not None else _gen_id("a-")
        self.name: str = name if name is not None else _droid_name(self.id)
        self.current_claude: ProcessHandle | None = None
        self.current_task_id: str | None = None
        # Last state we told the backend about; resent on WS reconnect so the
        # backend (and frontend) don't show the agent stuck offline.
        self.state: str = "idle"
        # Fine-grained activity within the "working" state (reading/editing/etc.)
        self.activity: str | None = None

    @property
    def agent_id(self) -> str:
        return self.id


class Worker:
    """Registers with the backend, maintains a pool of agents, and executes tasks.

    Two key ownership relationships:

    *Agents are parented to this worker.*  The ``agents`` pool is created at
    startup and torn down at shutdown.  The worker is responsible for their
    entire lifecycle.

    *Tasks are NOT owned by this worker.*  Tasks are external work items
    managed by the backend/foreman.  The worker and its agents are the
    execution environment: the worker receives tasks, runs them on an agent,
    and reports results.  Task-tracking state (``_known_task_ids``,
    ``_cancelled_tasks``, ``_redirect_queues``, ``_task_worktrees``) is
    routing/bookkeeping infrastructure that lives here only because this
    worker is the current execution context — the authoritative task record
    lives in the backend.
    """

    def __init__(self, cfg: config_mod.Config) -> None:
        self.cfg = cfg
        # No-op on non-macOS or when pyobjc isn't installed; see sleep_monitor.py.
        self.sleep_monitor = SystemSleepMonitor(
            on_sleep=self._on_system_sleep, on_wake=self._on_system_wake
        )
        self.ws = WSClient(cfg.ws_url, sleep_monitor=self.sleep_monitor)
        self._shutdown_event = asyncio.Event()
        # Reason for the current shutdown, sent to the backend in worker-disconnect
        # so the foreman can tell an idle-timeout reap from a signal/crash.
        self._shutdown_reason: str | None = None
        self._worker_name: str = ""
        # Captured at run() start so the optional control API's handler thread
        # can schedule coroutines onto the worker's loop. Stays None when no
        # control API is configured.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._control_server: ControlServer | None = None

        # ── Agent pool ──────────────────────────────────────────────────────
        # Agents are owned by this worker.  Created once at startup, destroyed
        # at shutdown.  Each agent runs one task at a time; the pool enables
        # concurrent execution up to cfg.max_agents.
        self.agents: list[Agent] = [Agent() for _ in range(cfg.max_agents)]

        # ── Task execution infrastructure ────────────────────────────────────
        # Tasks are NOT owned by the worker.  The fields below are routing and
        # bookkeeping state that exists only while tasks are executing here.
        # The authoritative task record lives in the backend.

        # Shared queue: all agents drain from here; the listener pushes into it.
        self.task_queue: asyncio.Queue[object] = asyncio.Queue()
        # Guards against re-queueing the same task-id on reconnect or poll.
        self._known_task_ids: set[str] = set()
        # Tasks cancelled by the foreman or a human; checked before/during execution.
        self._cancelled_tasks: set[str] = set()
        # Per-task redirect-instruction queues (SIGTERM + --resume flow).
        self._redirect_queues: dict[str, asyncio.Queue] = {}
        self._interactive_queues: dict[str, asyncio.Queue] = {}
        # Worktrees materialised for each task, kept alive within the TTL window
        # so follow-ups can reuse the existing checkout without a re-clone.
        # Keyed by task_id; each entry is a list of (repo_path, wt_path, last_used_monotonic).
        self._task_worktrees: dict[str, list[tuple[str, str, float]]] = {}

        # ── Repo state ───────────────────────────────────────────────────────
        # Set to True once _join() has been called the first time so that
        # _on_ws_reconnect doesn't prematurely re-join.
        self._joined = False
        # Monotonic timestamp of the last successful GitHub repo-list refresh.
        # 0 means never refreshed; _idle_puller compares against this.
        self._last_repo_refresh: float = 0.0
        # Monotonic timestamp of the last GitHub token fetch/refresh; compared
        # against GITHUB_TOKEN_REFRESH_INTERVAL_SECONDS by _idle_puller.
        self._last_github_token_refresh: float = 0.0
        # True once this worker has fetched its GitHub token from the backend
        # itself (see _fetch_github_token_if_needed). Only a backend-fetched
        # token is safe to periodically overwrite — a statically configured
        # one (config file / PIONEER_GITHUB_TOKEN / GITHUB_TOKEN env var) is
        # left alone since the backend endpoint may return an unrelated token.
        self._github_token_dynamic: bool = False
        # Merged repo list for worker-register broadcasts (static config repos
        # plus API-discovered repos). Never used for task execution — only for
        # telling the backend/UI how many repos this worker can see.
        self._broadcast_repos: list[str] = list(cfg.repos)
        self._runners = build_runner_registry(cfg)
        # Available tool runners detected at startup (e.g. ["claude", "codex", "pi"]).
        self._available_tools: list[str] = []
        # Per-tool live model catalogs detected at startup. Currently populated
        # for pi from `pi --list-models`, which is credential/env dependent.
        self._tool_models: dict[str, list[dict]] = {}
        # Per-tool env vars (claude/pi/codex). Kept OUT of os.environ so one tool's
        # credentials never leak into another's subprocess; merged over os.environ
        # only when spawning that specific tool. See _env_for_tool.
        self._tool_env: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------ HTTP
    def _hostname(self) -> str:
        """Return the host identity reported to the backend.

        Containers normally see their container id as socket.gethostname(). ASG
        workers set PIONEER_HOSTNAME to the EC2 instance id so infrastructure
        lifecycle hooks can map an instance termination back to a worker row.
        """
        return os.environ.get("PIONEER_HOSTNAME") or socket.gethostname()

    async def _http(self, *, authed: bool = False) -> httpx.AsyncClient:
        """Return an httpx client. With ``authed=True`` the worker's bearer
        token is attached so secret-fetching endpoints (claude creds, github
        token) accept the request — those routes reject anonymous callers."""
        headers = {}
        if authed and self.cfg.auth_token:
            headers["Authorization"] = f"Bearer {self.cfg.auth_token}"
        return httpx.AsyncClient(base_url=self.cfg.http_url, timeout=30.0, headers=headers)

    async def _register(self) -> None:
        if self.cfg.worker_id and self.cfg.auth_token:
            # Pre-assigned by the foreman's spawn_worker tool — skip self-registration.
            self._worker_name = self.cfg.worker_name or self.cfg.worker_id
            logger.info(
                "Using pre-assigned worker_id=%s name=%s (skipping self-registration)",
                self.cfg.worker_id,
                self._worker_name,
            )
            return

        async with await self._http() as client:
            resp = await client.post(
                f"/guilds/{self.cfg.guild_id}/workers",
                json={
                    "repos": self.cfg.repos,
                    "org": self.cfg.org,
                    "github_token": None,
                    "hostname": self._hostname(),
                    "user": self.cfg.user,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            wid = payload["id"]
            self.cfg.auth_token = payload.get("auth_token")
            self._worker_name = payload.get("name") or wid
        self.cfg.worker_id = wid
        if not self.cfg.auth_token:
            logger.warning(
                "Registration response did not include auth_token — "
                "secret-fetching endpoints will reject this worker. "
                "Backend may need an upgrade."
            )
        logger.info(
            "Registered as worker %s (name=%s, %d agents) user=%s",
            wid,
            self._worker_name,
            len(self.agents),
            self.cfg.user or "<unattributed>",
        )

    async def _report_usage(
        self,
        *,
        task_id: str,
        tool: str,
        session_id: str | None,
        repo: str | None,
        records: list[dict],
    ) -> None:
        """Best-effort POST of per-API-call usage for a finished run.

        Never raises — usage reporting must not affect task outcome.
        """
        if not records:
            return
        body = {
            "task_id": task_id,
            "worker_id": self.cfg.worker_id,
            "session_id": session_id,
            "tool": tool,
            "repo": repo,
            "reporter": self._worker_name or self.cfg.worker_id,
            "records": records,
        }
        try:
            async with await self._http(authed=True) as client:
                resp = await client.post(f"/guilds/{self.cfg.guild_id}/usage", json=body)
                if resp.status_code >= 400:
                    logger.warning(
                        "Usage report for task %s rejected (status %d): %s",
                        task_id,
                        resp.status_code,
                        resp.text[:200],
                    )
        except Exception as exc:
            logger.warning("Usage report for task %s failed: %s", task_id, exc)

    async def _fetch_github_token_if_needed(self) -> None:
        """Startup entry point: fetch a token from the backend only if none is configured.

        A statically configured token (config file / PIONEER_GITHUB_TOKEN /
        GITHUB_TOKEN env var) already lives in self.cfg.github_token by the
        time this runs (see config.load_config), so the fetch is skipped and
        the token is never periodically refreshed — see _refresh_github_token.
        """
        if not self.cfg.github_token:
            await self._refresh_github_token()
            self._github_token_dynamic = self.cfg.github_token is not None

        if self.cfg.github_token and not os.environ.get("GITHUB_TOKEN"):
            os.environ["GITHUB_TOKEN"] = self.cfg.github_token
            logger.info("GITHUB_TOKEN set in environment for gh CLI and subprocesses")

    async def _refresh_github_token(self) -> None:
        """Fetch the guild's current GitHub token from the backend and cache it.

        Called once at startup by _fetch_github_token_if_needed, and then
        periodically from _idle_puller (gated on _github_token_dynamic) so a
        long-lived worker keeps a valid token — see
        GITHUB_TOKEN_REFRESH_INTERVAL_SECONDS. Unlike the startup path, this
        unconditionally overwrites both self.cfg.github_token and the
        GITHUB_TOKEN env var so `gh` CLI calls, idle repo pulls, and repo-list
        refreshes all pick up the new token immediately.
        """
        self._last_github_token_refresh = asyncio.get_event_loop().time()
        try:
            async with await self._http(authed=True) as client:
                resp = await client.get(
                    "/auth/github/token",
                    params={"guild_id": self.cfg.guild_id},
                )
            if resp.status_code != 200:
                logger.warning("No GitHub token from backend (status %d)", resp.status_code)
                return
            data = resp.json()
            token = data.get("access_token")
            if not token:
                return
            self.cfg.github_token = token
            os.environ["GITHUB_TOKEN"] = token
            logger.info("Refreshed GitHub token for user %s", data.get("username"))
            # When the backend is App-authenticated it also returns the
            # bot's git author identity; set it so commits attribute to
            # the App rather than the push token's default identity.
            name = data.get("git_author_name")
            email = data.get("git_author_email")
            if name and email:
                await git_ops.set_git_identity(name, email)
                logger.info("Git author identity set to %s <%s>", name, email)
        except Exception as exc:
            logger.warning("Could not fetch GitHub token: %s", exc)

    async def _task_github_token(self, task_id: str) -> str | None:
        """Fetch a fresh token to push/PR *task_id* as its triggering user.

        The backend returns that user's OAuth token (so the branch and PR are
        attributed to the human who triggered the task), falling back to the
        App installation token. Fetched per task and used in an explicit push
        URL — never persisted in the shared ``origin`` remote — so concurrent
        agents pushing as different users don't collide, and the token is never
        stale the way a clone-time embedded token was.
        """
        try:
            async with await self._http(authed=True) as client:
                resp = await client.get(
                    "/auth/github/token",
                    params={"guild_id": self.cfg.guild_id, "task_id": task_id},
                )
            if resp.status_code == 200:
                return resp.json().get("access_token")
            logger.warning("No task GitHub token from backend (status %d)", resp.status_code)
        except Exception as exc:
            logger.warning("Could not fetch task GitHub token: %s", exc)
        return self.cfg.github_token

    async def _fetch_guild_env_vars(self) -> None:
        """Fetch guild-level env vars from foreman config and apply to the process environment.

        Only sets vars not already present — docker-provided env vars take precedence
        so production deployments aren't overridden unexpectedly.
        """
        try:
            async with await self._http(authed=True) as client:
                resp = await client.get(f"/guilds/{self.cfg.guild_id}/foreman/env-vars")
            if resp.status_code != 200:
                logger.debug(
                    "Foreman env vars: status %d (no vars or not auth'd)", resp.status_code
                )
                return
            payload = resp.json()
            env_vars = payload.get("env_vars", [])
            applied = 0
            for item in env_vars:
                key = item.get("key", "")
                value = item.get("value")
                if not key or value is None:
                    continue
                if key not in os.environ:
                    os.environ[key] = value
                    applied += 1
                    logger.info("Applied foreman env var: %s (len=%d)", key, len(str(value)))
                else:
                    logger.debug("Skipping foreman env var %s (already set in environment)", key)
            if applied:
                logger.info("Applied %d foreman env var(s) to process environment", applied)

            # Per-tool env vars stay scoped: stored here, never dumped into
            # os.environ, and merged in only when the matching tool is spawned.
            self._tool_env = {}
            for tool, items in (payload.get("tool_env_vars") or {}).items():
                scoped: dict[str, str] = {}
                for item in items or []:
                    key = item.get("key", "")
                    value = item.get("value")
                    if key and value is not None:
                        scoped[key] = value
                if scoped:
                    self._tool_env[tool] = scoped
                    logger.info("Loaded %d scoped env var(s) for tool %r", len(scoped), tool)
        except Exception as exc:
            logger.warning("Could not fetch foreman env vars: %s", exc)

    def _env_for_tool(self, tool: str) -> dict[str, str]:
        """Return the environment a *tool*'s runner subprocess should inherit.

        Base process env (which already carries the shared foreman env_vars)
        overlaid with the tool's own scoped vars. Scoped vars win so a guild can
        override a shared default for one tool without affecting the others.
        """
        env = dict(os.environ)
        env.update(self._tool_env.get(tool, {}))
        return env

    async def _check_gh_auth(self) -> None:
        """Run `gh auth status` and log the result as a startup health check."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh",
                "auth",
                "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            logger.warning("gh CLI not found — PR creation via gh will not work")
            return
        except Exception as exc:
            logger.warning("gh auth status spawn failed: %s", exc)
            return

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except TimeoutError:
            logger.warning("gh auth status timed out after 10s")
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
                await proc.wait()
            return

        output = stdout.decode(errors="replace").strip()
        if proc.returncode == 0:
            logger.info("gh auth status: authenticated\n%s", output)
        else:
            logger.warning("gh auth status: NOT authenticated (rc=%d)\n%s", proc.returncode, output)

    async def _check_codex_doctor(self) -> None:
        """Run `codex doctor` as a non-fatal startup diagnostic."""
        if not self.cfg.codex_doctor:
            logger.info("codex doctor skipped by config")
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cfg.codex_path,
                "doctor",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            logger.warning("codex CLI not found at %r — Codex tasks will fail", self.cfg.codex_path)
            return
        except Exception as exc:
            logger.warning("codex doctor spawn failed: %s", exc)
            return

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        except TimeoutError:
            logger.warning("codex doctor timed out after 20s")
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
                await proc.wait()
            return

        output = stdout.decode(errors="replace").strip()
        if proc.returncode == 0:
            logger.info("codex doctor: ok\n%s", output)
        else:
            logger.warning("codex doctor: failed (rc=%d)\n%s", proc.returncode, output)

    async def _tool_has_credentials(self, name: str) -> bool:
        """Return True if *name* has usable credentials in the environment.

        Credentials belong in the guild env vars now (applied by
        _fetch_guild_env_vars before detection). A tool missing its credentials
        is dropped from the available-tools list with a warning rather than
        launched and failed per-task. This holds for pi too: it needs a provider
        credential like the others, so an unconfigured pi is dropped instead of
        becoming a task-swallowing default.
        """
        runner = self._runners.get(name)
        if runner is None:
            return False
        env = self._env_for_tool(name)
        models = await runner.list_models(env)
        if models:
            self._tool_models[name] = models
        else:
            self._tool_models.pop(name, None)
        return await runner.probe_credentials(env)

    async def _ensure_tools_installed(self) -> None:
        """Install any missing AI coding CLIs before probing PATH for them.

        Worker images no longer bake in claude/codex/pi (see the `worker` stage
        in the root Dockerfile); each is npm-installed here on first use in a
        given container and left alone on every later call, since a tool
        already resolvable via its configured path/PATH entry is skipped.
        """
        targets = (
            self.cfg.install_tools
            if self.cfg.install_tools is not None
            else self.cfg.tools
            if self.cfg.tools is not None
            else list(tool_installer.ALL_TOOLS)
        )
        if not targets:
            return
        tool_paths = {
            "claude": self.cfg.claude_path,
            "codex": self.cfg.codex_path,
            "pi": self.cfg.pi_path,
        }
        await tool_installer.ensure_tools_installed(targets, tool_paths=tool_paths)

    async def _detect_available_tools(self) -> None:
        """Populate self._available_tools from runner binaries on PATH + credentials.

        When cfg.tools is set, only the listed names are probed; when None (default)
        all known tool binaries are probed. A tool is available only if its binary
        is present AND it has usable credentials — otherwise it is dropped with a
        warning telling the operator to set the credential in the guild env vars.
        """
        import shutil

        def _is_executable(path: str) -> bool:
            """Return True if *path* resolves to an executable file.

            For bare names (no path separator) uses shutil.which to search PATH.
            For paths containing a separator, falls back to a direct file check
            when shutil.which returns None (handles relative and absolute paths
            that aren't on PATH).
            """
            if shutil.which(path):
                return True
            if os.sep in path or (os.altsep and os.altsep in path):
                try:
                    return os.path.isfile(path) and os.access(path, os.X_OK)
                except OSError:
                    return False
            return False

        tool_paths = {
            "claude": self.cfg.claude_path,
            "codex": self.cfg.codex_path,
            "pi": self.cfg.pi_path,
        }
        cred_hint = {
            "claude": " or ".join(_CLAUDE_CREDENTIAL_KEYS),
            "codex": "OPENAI_API_KEY",
        }
        candidates = self.cfg.tools if self.cfg.tools is not None else list(self._runners)

        tools: list[str] = []
        for name in candidates:
            binary = tool_paths.get(name)
            if binary is None:
                logger.warning("Unknown tool %r in --tools list; skipping", name)
                continue
            if not _is_executable(binary):
                logger.warning(
                    "Tool %r not found on PATH (checked %r); excluding from available tools",
                    name,
                    binary,
                )
                continue
            if not await self._tool_has_credentials(name):
                logger.warning(
                    "Tool %r has no credentials (set %s in the guild env vars); "
                    "excluding from available tools",
                    name,
                    cred_hint.get(name, "the required credentials"),
                )
                continue
            tools.append(name)
        self._available_tools = tools
        logger.info("Available tools: %s", tools or ["(none)"])

    async def _claude_is_authenticated(self, env: dict[str, str] | None = None) -> bool:
        """Return True if `claude auth status --json` reports loggedIn=true.

        Works on macOS keychain and Linux. The exit code alone is unreliable
        (the CLI returns 0 even when not logged in, just emits ``loggedIn:
        false``), so we parse the JSON. A timeout guards against macOS keychain
        access prompts that can hang the subprocess indefinitely when invoked
        without a controlling TTY.

        *env* is claude's scoped environment (see _env_for_tool). Passing it
        matters: the codex and pi probes already do, and without it a per-tool
        override that configures claude's auth is invisible to the probe, so a
        correctly configured tool reads as unauthenticated and gets dropped.
        """
        import json as _json

        try:
            proc = await asyncio.create_subprocess_exec(
                self.cfg.claude_path,
                "auth",
                "status",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env if env is not None else self._env_for_tool("claude"),
            )
        except FileNotFoundError as exc:
            logger.warning("claude binary not found at %r: %s", self.cfg.claude_path, exc)
            return False
        except Exception as exc:
            logger.warning("claude auth status spawn failed: %s", exc)
            return False

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except TimeoutError:
            logger.warning(
                "claude auth status timed out after 10s — keychain prompt? killing pid=%s",
                proc.pid,
            )
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
                await proc.wait()
            return False

        raw = stdout.decode(errors="replace").strip()
        logger.info("claude auth status rc=%s output=%s", proc.returncode, raw[:200])
        if proc.returncode != 0:
            return False
        try:
            parsed = _json.loads(raw)
        except _json.JSONDecodeError:
            # Older claude versions returned plain text; fall back to rc=0 == authed.
            logger.info("claude auth status output is not JSON; treating rc=0 as authed")
            return True
        logged_in = bool(parsed.get("loggedIn"))
        if logged_in:
            logger.info(
                "Claude auth detected (method=%s provider=%s)",
                parsed.get("authMethod"),
                parsed.get("apiProvider"),
            )
        else:
            logger.info("claude auth status reports loggedIn=false")
        return logged_in

    async def _codex_is_authenticated(self) -> bool:
        """Return True if `codex doctor --json --summary` reports auth configured.

        Mirrors _claude_is_authenticated: the exit code alone doesn't isolate
        auth (doctor returns 0 with unrelated warnings), so we parse the JSON
        and read the auth.credentials check's status. A timeout guards against a
        hung subprocess.
        """
        import json as _json

        try:
            proc = await asyncio.create_subprocess_exec(
                self.cfg.codex_path,
                "doctor",
                "--json",
                "--summary",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._env_for_tool("codex"),
            )
        except FileNotFoundError as exc:
            logger.warning("codex binary not found at %r: %s", self.cfg.codex_path, exc)
            return False
        except Exception as exc:
            logger.warning("codex doctor spawn failed: %s", exc)
            return False

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        except TimeoutError:
            logger.warning("codex doctor timed out after 20s; killing pid=%s", proc.pid)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
                await proc.wait()
            return False

        raw = stdout.decode(errors="replace").strip()
        try:
            parsed = _json.loads(raw)
        except _json.JSONDecodeError:
            logger.warning("codex doctor output is not JSON: %s", raw[:200])
            return False
        status = parsed.get("checks", {}).get("auth.credentials", {}).get("status")
        logger.info("codex doctor auth.credentials status=%s", status)
        return status == "ok"

    async def _pi_has_models(self) -> bool:
        """Load the live pi model catalog and return True if any model is usable."""
        try:
            models = await pi_runner.list_pi_models(
                pi_path=self.cfg.pi_path,
                env=self._env_for_tool("pi"),
                timeout=20.0,
            )
        except FileNotFoundError as exc:
            logger.warning("pi binary not found at %r: %s", self.cfg.pi_path, exc)
            return False
        except Exception as exc:
            logger.warning("pi --list-models spawn failed: %s", exc)
            return False
        if models:
            self._tool_models["pi"] = models
        else:
            self._tool_models.pop("pi", None)
        logger.info("pi --list-models returned %d model rows", len(models))
        return bool(models)

    # ------------------------------------------------------------------ Control API
    def _status_snapshot(self) -> dict:
        """Point-in-time view of the worker, served by the control API."""
        return {
            "workerId": self.cfg.worker_id,
            "workerName": self._worker_name,
            "guildId": self.cfg.guild_id,
            "repos": self._broadcast_repos,
            "joined": self._joined,
            "shuttingDown": self._shutdown_event.is_set(),
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
            "knownTaskIds": sorted(self._known_task_ids),
            "queueDepth": self.task_queue.qsize(),
        }

    def _build_injected_task(self, body: dict) -> dict:
        """Translate a control-API POST body into a task-queue dict."""
        task_id = body.get("id") or body.get("taskId") or _gen_id("t-")
        task: dict = {
            "id": task_id,
            "worker_id": self.cfg.worker_id,
            "guild_id": self.cfg.guild_id,
            "name": body.get("name", ""),
            "description": body.get("description", ""),
            "tool": body.get("tool", "claude"),
            "phase": body.get("phase", "execute"),
            "issue_number": body.get("issueNumber"),
            "issue_repo": body.get("issueRepo"),
            "pr_number": body.get("prNumber"),
            "pr_repo": body.get("prRepo"),
            "pr_head_ref": body.get("branch"),
            "pr_url": body.get("prUrl"),
            "head_sha": body.get("headSha"),
            "repos": body.get("repos") or [],
        }
        if body.get("followupInstructions"):
            task["followup_instructions"] = body["followupInstructions"]
        if body.get("followupBranch"):
            task["followup_branch"] = body["followupBranch"]
        return task

    def _assignment_capacity_reason(self, task: dict) -> str | None:
        """Return None if a pushed assignment can run now, else the rejection reason."""
        task_id = task.get("id")
        target_agent_id = task.get("target_agent_id")
        queued = self.task_queue.qsize()
        active = sum(1 for slot in self.agents if slot.current_task_id is not None)
        if target_agent_id:
            slot = next((s for s in self.agents if s.agent_id == target_agent_id), None)
            if slot is None:
                return f"target agent {target_agent_id} is not part of this worker"
            if slot.current_task_id is not None or slot.state != "idle":
                return f"target agent {target_agent_id} is busy"
            if queued:
                return f"worker already has {queued} queued task(s)"
            return None
        capacity = len(self.agents)
        reserved = active + queued
        if reserved >= capacity:
            return f"all {capacity} agent slot(s) are busy or reserved ({active} active, {queued} queued)"
        if task_id in self._cancelled_tasks:
            return f"task {task_id} is cancelled"
        return None

    async def _reject_assignment(self, task_id: str, reason: str) -> None:
        logger.warning("Rejecting task %s: %s", task_id, reason)
        self._known_task_ids.discard(task_id)
        await self._send(
            {
                "type": "task-rejected",
                "workerId": self.cfg.worker_id,
                "taskId": task_id,
                "reason": reason,
            }
        )

    async def _inject_task(self, body: dict) -> dict:
        """Enqueue a task supplied over the control API (foreman bypass)."""
        if not body.get("description") and not body.get("followupInstructions"):
            return {"error": "description (or followupInstructions) is required"}
        task = self._build_injected_task(body)
        task_id = task["id"]
        if task_id in self._known_task_ids:
            return {"error": f"task {task_id!r} already known to this worker"}
        self._known_task_ids.add(task_id)
        await self.task_queue.put(task)
        logger.info("Control API injected task %s: %s", task_id, task["description"][:80])
        return {"ok": True, "taskId": task_id, "queueDepth": self.task_queue.qsize()}

    def _start_control_api(self) -> None:
        if self.cfg.api_port is None:
            return
        self._control_server = ControlServer(self, host=self.cfg.api_host, port=self.cfg.api_port)
        try:
            self._control_server.start()
        except OSError:
            self._control_server = None

    def _stop_control_api(self) -> None:
        if self._control_server is not None:
            self._control_server.stop()
            self._control_server = None

    # ------------------------------------------------------------------ Sleep/wake
    # These run on SystemSleepMonitor's background thread (see sleep_monitor.py),
    # so they hop onto the worker's event loop rather than touching asyncio state
    # directly.
    def _on_system_sleep(self) -> None:
        if self._loop is not None:
            fut = asyncio.run_coroutine_threadsafe(self.ws.close(), self._loop)
            fut.add_done_callback(
                lambda f: f.exception() and logger.error("sleep-hook error: %s", f.exception())
            )

    def _on_system_wake(self) -> None:
        # SystemSleepMonitor already cleared its own sleeping flag (after
        # waiting out the wake grace period) before firing this callback, so
        # WSClient's reconnect loop — paused on sleep_monitor.is_sleeping —
        # resumes on its own. No explicit reconnect needed here; that would
        # leave the worker stuck disconnected if the one-off attempt failed.
        pass

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

    async def _emit(self, line: str, detail: dict | None = None, level: str = LEVEL_WORKER) -> None:
        """Emit a worker-level log line.

        ``level`` classifies the line semantically (see the ``LEVEL_*``
        constants) so the frontend can style it without parsing text prefixes.
        Worker-level lines default to ``LEVEL_WORKER``.
        """
        msg: dict = {
            "type": "terminal-output",
            "workerId": self.cfg.worker_id,
            "line": line,
            "timestamp": _now_iso(),
        }
        if level and level != LEVEL_INFO:
            msg["level"] = level
        if detail:
            msg["detail"] = detail
        await self._send(msg)

    def _task_emit(self, task_id: str, slot: Agent):
        """Return an emit function scoped to a task and agent slot."""

        async def _emit_task(
            line: str, detail: dict | None = None, level: str = LEVEL_INFO
        ) -> None:
            msg: dict = {
                "type": "terminal-output",
                "workerId": self.cfg.worker_id,
                "agentId": slot.agent_id,
                "taskId": task_id,
                "line": line,
                "timestamp": _now_iso(),
            }
            if level and level != LEVEL_INFO:
                msg["level"] = level
            if detail:
                msg["detail"] = detail
            await self._send(msg)
            # Emit a granular agent-state update when activity changes
            if detail:
                new_activity = detail.get("activity")
                if new_activity and new_activity != slot.activity:
                    slot.activity = new_activity
                    await self._emit_agent_state(slot)

        return _emit_task

    async def _inject_worker_message(self, text: str, task_id: str | None) -> None:
        """Write *text* to the stdin of the agent running *task_id*.

        A worker runs up to ``max_agents`` tasks concurrently, so "the running
        agent" is ambiguous. With a task id we target exactly that agent. Without
        one we deliver only when a single agent is running — picking arbitrarily
        would inject one task's instructions into an unrelated task's session,
        which is worse than not delivering at all.
        """
        if task_id and task_id in self._interactive_queues:
            await self._interactive_queues[task_id].put(text)
            await self._emit(f"Queued for {task_id}: {text[:80]}")
            return

        running = [s for s in self.agents if s.current_claude]
        if task_id:
            target = next((s for s in running if s.current_task_id == task_id), None)
            if target is None:
                logger.info(
                    "worker-message for task %s: no agent running it here (running=%s); dropping",
                    task_id,
                    [s.current_task_id for s in running] or "none",
                )
                return
        elif len(running) == 1:
            target = running[0]
        elif not running:
            logger.debug("worker-message: no agent running; dropping")
            return
        else:
            tasks = ", ".join(str(s.current_task_id) for s in running)
            logger.warning(
                "worker-message without a taskId while %d agents are running (%s); dropping",
                len(running),
                tasks,
            )
            await self._emit(
                f"Message not delivered — {len(running)} tasks are running ({tasks}). "
                "Send it to a specific task."
            )
            return

        handle = target.current_claude
        if handle is None:
            return
        delivered = await handle.send_message(text)
        if delivered:
            await self._emit(f"Injected into {target.current_task_id}: {text[:80]}")
        else:
            logger.warning(
                "Failed to inject message into %s (stdin closed?)", target.current_task_id
            )

    async def _emit_agent_state(self, slot: Agent) -> None:
        """Broadcast the slot's full identity + runtime state.

        Every ``agent-state`` frame carries workerId/agentId/taskId so the
        frontend can map a task row to the slot that owns it without
        falling back to ambiguous worker-level matching.
        """
        await self._send(
            {
                "type": "agent-state",
                "workerId": self.cfg.worker_id,
                "agentId": slot.agent_id,
                "taskId": slot.current_task_id,
                "state": slot.state,
                "activity": slot.activity,
            }
        )

    async def _set_state(self, state: str, agent: Agent) -> None:
        agent.state = state
        if state != "working":
            agent.activity = None
        # Idle/offline slots aren't working anything; drop the task link so the
        # frontend can match `agent.taskId === task.id` without picking up a
        # stale association from the previous run.
        if state in ("idle", "offline"):
            agent.current_task_id = None
        await self._emit_agent_state(agent)

    async def _join(self) -> None:
        for agent in self.agents:
            await self._send(
                {
                    "type": "join",
                    "agentId": agent.id,
                    "agentName": agent.name,
                    "agentType": "worker",
                    "workerId": self.cfg.worker_id,
                }
            )
        # Derive primary tool from config or first detected tool.
        primary_tool = self.cfg.tool or (
            self._available_tools[0] if self._available_tools else None
        )
        msg: dict = {
            "type": "worker-register",
            "workerId": self.cfg.worker_id,
            "repos": self._broadcast_repos,
            "tools": self._available_tools,
            "models": self._tool_models,
            "hostname": self._hostname(),
        }
        if self.cfg.user:
            msg["user"] = self.cfg.user
        if self.cfg.provider:
            msg["provider"] = self.cfg.provider
        if primary_tool:
            msg["tool"] = primary_tool
        await self._send(msg)

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
        for slot in self.agents:
            if slot.state and slot.state != "idle":
                await self._emit_agent_state(slot)
        # Do not pull tasks from REST here. The backend replays assigned
        # pending/working tasks as task-assigned messages during join, and the
        # worker should not maintain its own backlog source that can race WS
        # delivery and enqueue the same task twice.

    async def _task_update(
        self, task_id: str, *, agent: Agent | None = None, **fields: object
    ) -> None:
        payload: dict = {
            "type": "task-update",
            "workerId": self.cfg.worker_id,
            "taskId": task_id,
            **fields,
        }
        # Include the agent identity so the UI can map task→agent unambiguously
        # when a worker runs multiple concurrent agents (workerId is shared).
        if agent is not None:
            payload["agentId"] = agent.agent_id
        await self._send(payload)

    async def _ensure_pr_webhook(self, pr_url: str, emit) -> None:
        """Best-effort: install/refresh a Pioneer Square webhook on the PR's repo.

        Failures are logged via *emit* and swallowed — the PR is already
        open, so missing webhook coverage shouldn't block the task.
        """
        repo = github_pr._repo_from_pr_url(pr_url)
        if not repo:
            return
        try:
            await github_pr.ensure_webhook(
                repo=repo,
                target_url=self.cfg.webhook_target_url,
                http_url=self.cfg.http_url,
                guild_id=self.cfg.guild_id,
                auth_token=self.cfg.auth_token,
                github_token=self.cfg.github_token,
                emit=emit,
            )
        except Exception as exc:
            logger.warning("ensure_webhook raised for %s: %s", repo, exc)

    async def _initiate_shutdown(self, reason: str) -> None:
        """Begin a graceful shutdown.

        Idle agents wake up and exit; busy agents finish their current claude
        run and then return to idle (the post-task follow-up window is gone —
        the foreman now drives follow-ups by re-queueing the task). Safe to
        call multiple times.
        """
        if self._shutdown_event.is_set():
            return
        self._shutdown_event.set()
        self._shutdown_reason = reason
        logger.info("Graceful shutdown initiated: %s", reason)
        try:
            await self._emit(
                f"Shutdown requested ({reason}). Idle agents stopping; "
                "busy agents will finish their current task."
            )
        except Exception as exc:
            logger.debug("emit during shutdown failed (ignored): %s", exc)
        # Wake idle agents waiting on task_queue.get()
        for _ in self.agents:
            self.task_queue.put_nowait(_SHUTDOWN_SENTINEL)

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
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
            os.kill(os.getpid(), sig)
            return
        logger.info(
            "Received %s — initiating graceful shutdown (signal again to force)",
            sig.name,
        )
        loop.create_task(self._initiate_shutdown(f"signal {sig.name}"))

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
                    "reason": self._shutdown_reason,
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
            "Paths: repos_dir=%s work_dir=%s pull_interval=%.1fs max_turns=%s"
            " max_agents=%d claude=%s codex=%s codex_args=%s pi=%s",
            self.cfg.repos_dir,
            self.cfg.work_dir,
            self.cfg.pull_interval,
            self.cfg.claude_max_turns,
            self.cfg.max_agents,
            self.cfg.claude_path,
            self.cfg.codex_path,
            self.cfg.codex_args,
            self.cfg.pi_path,
        )

        self._loop = asyncio.get_running_loop()
        self._install_signal_handlers()
        self._start_control_api()
        self.sleep_monitor.start()

        await self._register()
        assert self.cfg.worker_id, "worker_id must be set after registration"

        # Guarantee PIONEER_BACKEND_URL is in the environment even when
        # backend_url came from the config file rather than the env var —
        # skills (e.g. worker/skills/debug-query) rely on it being set for
        # every spawned claude subprocess.
        os.environ.setdefault("PIONEER_BACKEND_URL", self.cfg.backend_url)

        await self._fetch_github_token_if_needed()
        await self._fetch_guild_env_vars()
        # Log what this worker received, once every source has landed (container
        # env, config, fetched shared vars, per-tool overrides). Keys only —
        # never values — so credentials don't reach the logs.
        logger.info(
            "Worker received: tool=%s provider=%s pi_model=%s tools=%s max_agents=%d"
            " env_keys=%s tool_env_keys=%s",
            self.cfg.tool,
            self.cfg.provider,
            self.cfg.pi_model,
            self.cfg.tools,
            self.cfg.max_agents,
            sorted(os.environ.keys()),
            {tool: sorted(v.keys()) for tool, v in self._tool_env.items()},
        )
        await self._refresh_github_repos()
        await self._check_gh_auth()
        await self._ensure_tools_installed()
        await self._check_codex_doctor()

        logger.info("Connecting to backend WebSocket at %s", self.cfg.ws_url)
        self.ws.on_reconnect = self._on_ws_reconnect
        await self.ws.connect()

        # Detect tools after guild env vars are applied so credential-gated tools
        # (claude, codex) see their keys. A tool missing credentials is dropped
        # with a warning rather than launched per-task and failed.
        listener = asyncio.create_task(self._listen())
        await self._detect_available_tools()

        await self._join()
        self._joined = True
        logger.info(
            "Joined guild %s — worker_id=%s agents=%d",
            self.cfg.guild_id,
            self.cfg.worker_id,
            len(self.agents),
        )

        if self.cfg.repos:
            logger.info("Cloning/fetching %d configured repo(s) at startup", len(self.cfg.repos))
            await asyncio.gather(
                *(
                    git_ops.ensure_repo(self.cfg.repos_dir, r, self.cfg.github_token)
                    for r in self.cfg.repos
                )
            )

        # Reclaim any worktrees the previous incarnation of this worker left
        # behind. Tasks within the TTL window are re-registered so a follow-up
        # arriving for them can reuse the existing checkout.
        # Guard with a timeout: a git lock left by a crashed process can make
        # prune/remove hang indefinitely, blocking the entire startup sequence.
        try:
            await asyncio.wait_for(self._initial_worktree_sweep(), timeout=30.0)
        except TimeoutError:
            logger.warning("Worktree startup sweep timed out after 30s — skipping")

        await self._emit("Online. Watching for tasks.")
        for slot in self.agents:
            await self._set_state("idle", slot)

        # Task delivery is server-push only: the backend replays any already
        # assigned pending/working tasks during join. Avoid an initial REST pull
        # here; it can race the WS replay/assignment and duplicate work across
        # multiple local agent slots.
        runners = [asyncio.create_task(self._agent_loop(slot)) for slot in self.agents]
        puller = asyncio.create_task(self._idle_puller())
        sweeper = asyncio.create_task(self._worktree_sweeper())
        s3_syncer = asyncio.create_task(self._s3_syncer()) if self.cfg.s3_bucket else None
        aux_tasks = [listener, puller, sweeper, *([] if s3_syncer is None else [s3_syncer])]
        try:
            # Wait for either: all runners exit (graceful shutdown), or one of
            # the auxiliary tasks crashes (unexpected).
            done, _pending = await asyncio.wait(
                [*aux_tasks, *runners],
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
            for t in aux_tasks:
                t.cancel()

            await asyncio.gather(*aux_tasks, *runners, return_exceptions=True)

            if first_exc is not None:
                raise first_exc
        finally:
            if self.cfg.s3_bucket:
                with anyio.CancelScope(shield=True):
                    try:
                        logger.info("Running final S3 sync before shutdown")
                        await asyncio.wait_for(
                            s3_uploader.sync_paths(
                                bucket=self.cfg.s3_bucket,
                                prefix=self.cfg.s3_prefix,
                                paths=self.cfg.s3_paths,
                            ),
                            timeout=60.0,
                        )
                    except TimeoutError:
                        logger.warning("Final S3 sync timed out after 60s")
                    except Exception as exc:
                        logger.warning("Final S3 sync failed: %s", exc)
            with anyio.CancelScope(shield=True):
                await self._notify_offline()
            logger.info("Worker shutting down; closing WebSocket")
            await self.ws.close()
            self._stop_control_api()
            self.sleep_monitor.stop()

    # ------------------------------------------------------------------ Listener
    async def _listen(self) -> None:
        logger.info("Listener started")
        # Message types safe to process before _join() completes. Everything else
        # (task assignments, follow-ups, redirects, etc.) must wait until we've
        # actually joined, otherwise we'd start work before the backend sees us
        # online.
        _PRE_JOIN_ALLOWED = {"pong", "worker-message"}
        async for msg in self.ws.messages():
            mtype = msg.get("type")
            logger.debug("WS message: type=%s keys=%s", mtype, list(msg.keys()))

            try:
                if not self._joined and mtype not in _PRE_JOIN_ALLOWED:
                    logger.debug("Dropping %s message received before join", mtype)
                    continue

                if mtype == "pong":
                    # Generic ping reply from the backend; nothing to do.
                    continue

                if mtype == "worker-ping":
                    target = msg.get("workerId")
                    if target and target != self.cfg.worker_id:
                        continue
                    await self._send(
                        {
                            "type": "worker-pong",
                            "workerId": self.cfg.worker_id,
                            "timestamp": _now_iso(),
                        }
                    )
                    continue

                if mtype == "task-assigned":
                    if msg.get("workerId") != self.cfg.worker_id:
                        continue
                    task_id = msg.get("taskId")
                    if not task_id or task_id in self._known_task_ids:
                        continue
                    logger.info(
                        "Task assigned: %s — %s",
                        task_id,
                        (msg.get("description") or "")[:80],
                    )
                    logger.info(
                        "Task %s metadata received: phase=%s issue_repo=%s pr_number=%s "
                        "pr_repo=%s branch=%s pr_url=%s head_sha=%s",
                        task_id,
                        msg.get("phase"),
                        msg.get("issueRepo"),
                        msg.get("prNumber"),
                        msg.get("prRepo"),
                        msg.get("branch"),
                        msg.get("prUrl"),
                        msg.get("headSha"),
                    )
                    task = {
                        "id": task_id,
                        "worker_id": self.cfg.worker_id,
                        "guild_id": self.cfg.guild_id,
                        "name": msg.get("name", ""),
                        "description": msg.get("description", ""),
                        "tool": msg.get("tool", "claude"),
                        "task_type": msg.get("taskType") or msg.get("task_type") or "standard",
                        "target_agent_id": msg.get("targetAgentId"),
                        "model": msg.get("model"),
                        "provider": msg.get("provider"),
                        "phase": msg.get("phase", "execute"),
                        "issue_number": msg.get("issueNumber"),
                        "issue_repo": msg.get("issueRepo"),
                        "pr_number": msg.get("prNumber"),
                        "pr_repo": msg.get("prRepo"),
                        # PR head ref/URL/SHA as known at assignment time — informational
                        # only. Review checkout still re-resolves the head branch live via
                        # pr_repo/pr_number (see _execute_task) rather than trusting these,
                        # since the PR may have moved since the webhook fired.
                        "pr_head_ref": msg.get("branch"),
                        "pr_url": msg.get("prUrl"),
                        "head_sha": msg.get("headSha"),
                        "repos": msg.get("repos") or [],
                    }
                    if reason := self._assignment_capacity_reason(task):
                        await self._reject_assignment(task_id, reason)
                        continue
                    self._known_task_ids.add(task_id)
                    await self.task_queue.put(task)

                elif mtype == "worker-message":
                    if msg.get("workerId") != self.cfg.worker_id:
                        continue
                    text = msg.get("message", "")
                    if text:
                        await self._inject_worker_message(text, msg.get("taskId"))

                elif mtype == "worker-outdated":
                    if msg.get("workerId") not in (None, self.cfg.worker_id):
                        continue
                    # The backend noticed this worker is running an older version.
                    # Informational only — the worker keeps running its current work.
                    logger.info(
                        "Backend reports this worker is out of date (%s); continuing",
                        msg.get("reason", "version mismatch"),
                    )

                elif mtype == "task-followup":
                    if msg.get("workerId") != self.cfg.worker_id:
                        continue
                    task_id = msg.get("taskId")
                    if not task_id:
                        continue
                    instructions = msg.get("instructions", "")
                    # The follow-up window inside _execute_task is gone — workers
                    # return to the idle pool right after task-complete. A
                    # follow-up is now a fresh enqueue: reuse the existing
                    # worktree if it's still on disk, otherwise attach one to the
                    # branch the original worker pushed.
                    logger.info(
                        "Follow-up received for task %s: %s",
                        task_id,
                        instructions[:80],
                    )
                    task = {
                        "id": task_id,
                        "worker_id": self.cfg.worker_id,
                        "guild_id": self.cfg.guild_id,
                        "name": msg.get("name", ""),
                        "description": msg.get("description", "") or instructions,
                        "tool": msg.get("tool", "claude"),
                        "model": msg.get("model"),
                        "provider": msg.get("provider"),
                        "phase": msg.get("phase", "execute"),
                        "issue_number": msg.get("issueNumber"),
                        "issue_repo": msg.get("issueRepo"),
                        "pr_number": msg.get("prNumber"),
                        "pr_repo": msg.get("prRepo"),
                        "repos": msg.get("repos") or [],
                        "followup_instructions": instructions,
                        "followup_branch": msg.get("branch", ""),
                        "session_id": msg.get("sessionId"),
                        "create_pr": bool(msg.get("createPr")),
                    }
                    if reason := self._assignment_capacity_reason(task):
                        await self._reject_assignment(task_id, reason)
                        continue
                    self._known_task_ids.add(task_id)
                    await self.task_queue.put(task)

                elif mtype == "task-finalize":
                    # The worker no longer parks waiting for finalize; foreman
                    # drives lifecycle now. Treat finalize as a hint that the
                    # task is closed and the worktree can be released early.
                    if msg.get("workerId") != self.cfg.worker_id:
                        continue
                    task_id = msg.get("taskId")
                    if not task_id:
                        continue
                    logger.info("Finalize signal for task %s — releasing worktree", task_id)
                    await self._release_task_worktrees(task_id)

                elif mtype == "task-cancel":
                    if msg.get("workerId") != self.cfg.worker_id:
                        continue
                    task_id = msg.get("taskId")
                    if not task_id:
                        continue
                    logger.info("Cancel signal for task %s", task_id)
                    self._cancelled_tasks.add(task_id)
                    # Kill subprocess if this task is currently running
                    active = next((s for s in self.agents if s.current_task_id == task_id), None)
                    if active and active.current_claude:
                        await active.current_claude.terminate()
                        logger.info("Terminated subprocess for cancelled task %s", task_id)
                    # Wake the redirect/cancel-aware inner loop if the task is
                    # mid-run; finalize-time cleanup happens through task-finalize.
                    rq = self._redirect_queues.get(task_id)
                    if rq is not None:
                        await rq.put(_CANCEL_SENTINEL)
                        logger.info("Signalled redirect queue for cancelled task %s", task_id)
                    iq = self._interactive_queues.get(task_id)
                    if iq is not None:
                        await iq.put(_CANCEL_SENTINEL)
                        logger.info("Signalled interactive queue for cancelled task %s", task_id)

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
                    active = next((s for s in self.agents if s.current_task_id == task_id), None)
                    if active and active.current_claude:
                        # Subprocess running — SIGTERM it and queue redirect for the
                        # in-flight redirect loop, which resumes claude with the
                        # full prior session id.
                        await active.current_claude.terminate()
                        logger.info("Terminated subprocess for redirect of task %s", task_id)
                        rq = self._redirect_queues.get(task_id)
                        if rq is not None:
                            await rq.put(instructions)
                    else:
                        rq = self._redirect_queues.get(task_id)
                        if rq is not None:
                            # Subprocess just exited — buffer; the redirect loop
                            # picks the buffered instructions up before returning
                            # the slot to idle.
                            await rq.put(instructions)
                            logger.info(
                                "task-redirect for %s: buffered for in-flight redirect loop",
                                task_id,
                            )
                        else:
                            # Task is no longer running on this worker; treat the
                            # redirect like a follow-up — re-queue the task with
                            # the new instructions. The branch field is whatever
                            # the foreman supplied; an empty string means "use
                            # the worktree we already have".
                            logger.info(
                                "task-redirect for %s outside an active run — re-queueing as follow-up",
                                task_id,
                            )
                            await self.task_queue.put(
                                {
                                    "id": task_id,
                                    "worker_id": self.cfg.worker_id,
                                    "guild_id": self.cfg.guild_id,
                                    "name": msg.get("name", ""),
                                    "description": msg.get("description", "") or instructions,
                                    "tool": msg.get("tool", "claude"),
                                    "phase": msg.get("phase", "execute"),
                                    "issue_number": msg.get("issueNumber"),
                                    "issue_repo": msg.get("issueRepo"),
                                    "pr_number": msg.get("prNumber"),
                                    "pr_repo": msg.get("prRepo"),
                                    "repos": msg.get("repos") or [],
                                    "followup_instructions": instructions,
                                    "followup_branch": msg.get("branch", ""),
                                }
                            )
            except Exception:
                logger.exception("Error handling WS message type=%s", mtype)

    # ------------------------------------------------------------------ Worktree bookkeeping

    def _register_worktrees(self, task_id: str, entries: list[tuple[str, str, str]]) -> None:
        """Record the worktrees we materialised for ``task_id``.

        Stored entries are ``(repo_path, wt_path, last_used_monotonic)``. The
        sweeper consults the timestamp to decide what's stale.
        """
        if not entries:
            return
        ts = asyncio.get_event_loop().time()
        self._task_worktrees[task_id] = [(rp, wt, ts) for _repo_full, rp, wt in entries]

    def _touch_task_worktrees(self, task_id: str) -> None:
        """Refresh the activity timestamp for a task's worktrees."""
        existing = self._task_worktrees.get(task_id)
        if not existing:
            return
        ts = asyncio.get_event_loop().time()
        self._task_worktrees[task_id] = [(rp, wt, ts) for rp, wt, _old in existing]

    async def _release_task_worktrees(self, task_id: str) -> None:
        """Remove all worktrees for a task immediately and forget them."""
        entries = self._task_worktrees.pop(task_id, None)
        if not entries:
            return
        logger.info("Releasing %d worktree(s) for task %s", len(entries), task_id)
        for repo_path, wt_path, _ts in entries:
            try:
                await git_ops.remove_worktree(repo_path, wt_path)
            except Exception as exc:
                logger.warning("remove_worktree failed for %s: %s", wt_path, exc)

    async def _refresh_github_repos(self) -> None:
        """Fetch repos accessible to the GitHub token and update the registered list.

        Static config repos are always included. If cfg.org is set, org repos
        from the API are appended (case-insensitive prefix match on owner).
        Without cfg.org, no API repos are added — the broadcast list stays as
        the static config. If the merged set differs from the current broadcast
        list the backend is notified via worker-register.
        """
        if not self.cfg.github_token:
            return
        if not self.cfg.org:
            # No org configured — nothing to expand; static repos are already broadcast.
            self._last_repo_refresh = asyncio.get_event_loop().time()
            return
        try:
            api_repos = await github_pr.fetch_accessible_repos(self.cfg.github_token)
        except Exception as exc:
            logger.warning("GitHub repo refresh failed for org %s: %s", self.cfg.org, exc)
            if self._joined:
                await self._emit(f"⚠ GitHub repo list refresh failed for {self.cfg.org}: {exc}")
            return
        if not api_repos:
            logger.debug("GitHub repo refresh returned empty list; skipping update")
            self._last_repo_refresh = asyncio.get_event_loop().time()
            return

        org_prefix = self.cfg.org.lower() + "/"
        merged = list(self.cfg.repos)
        for r in api_repos:
            if r not in merged and r.lower().startswith(org_prefix):
                merged.append(r)

        prev_count = len(self._broadcast_repos)
        self._last_repo_refresh = asyncio.get_event_loop().time()

        if set(merged) == set(self._broadcast_repos) and len(merged) == prev_count:
            logger.debug("GitHub repo list unchanged (%d repos)", prev_count)
            return

        self._broadcast_repos = merged
        logger.info("GitHub repos refreshed: %d total (was %d)", len(merged), prev_count)
        if self.cfg.worker_id:
            primary_tool = self.cfg.tool or (
                self._available_tools[0] if self._available_tools else None
            )
            refresh_msg: dict = {
                "type": "worker-register",
                "workerId": self.cfg.worker_id,
                "repos": self._broadcast_repos,
                "tools": self._available_tools,
                "hostname": self._hostname(),
            }
            if self.cfg.user:
                refresh_msg["user"] = self.cfg.user
            if self.cfg.provider:
                refresh_msg["provider"] = self.cfg.provider
            if primary_tool:
                refresh_msg["tool"] = primary_tool
            await self._send(refresh_msg)

    def _known_repos(self) -> list[str]:
        """All repos this worker may have cloned: static list + org repos already on disk."""
        repos = list(self.cfg.repos)
        if self.cfg.org:
            org_dir = os.path.join(self.cfg.repos_dir, self.cfg.org)
            if os.path.isdir(org_dir):
                try:
                    entries = os.listdir(org_dir)
                except OSError:
                    entries = []
                for entry in entries:
                    repo_full = f"{self.cfg.org}/{entry}"
                    if repo_full not in repos and os.path.isdir(
                        os.path.join(org_dir, entry, ".git")
                    ):
                        repos.append(repo_full)
        return repos

    async def _initial_worktree_sweep(self) -> None:
        """At startup, prune any worktrees this worker left behind from a previous run.

        The previous process may have died mid-task; nothing in memory tracks
        those worktrees, so we walk the work directory and remove anything
        attributable to this worker_id that's older than the TTL.
        """
        base = os.path.join(self.cfg.work_dir, self.cfg.guild_id, self.cfg.worker_id or "")
        if not os.path.isdir(base):
            return
        try:
            now = asyncio.get_event_loop().time()
            wall_now = datetime.now(UTC).timestamp()
            known_repos = self._known_repos()
            for entry in os.listdir(base):
                full = os.path.join(base, entry)
                if not os.path.isdir(full):
                    continue
                try:
                    age = wall_now - os.path.getmtime(full)
                except OSError:
                    continue
                if age <= WORKTREE_TTL_SECONDS:
                    # Re-register so the in-memory sweeper takes over.
                    ts = now - age
                    repos_for_task: list[tuple[str, str, float]] = []
                    for repo_full in known_repos:
                        repo_name = repo_full.split("/")[-1]
                        wt_path = os.path.join(full, repo_name)
                        if os.path.isdir(wt_path):
                            repo_path = os.path.join(self.cfg.repos_dir, *repo_full.split("/", 1))
                            repos_for_task.append((repo_path, wt_path, ts))
                    if repos_for_task:
                        self._task_worktrees[entry] = repos_for_task
                    continue
                logger.info("Startup sweep: removing stale task dir %s (age=%.0fs)", full, age)
                # Best-effort: remove each worktree via git, then drop the dir.
                for repo_full in known_repos:
                    repo_name = repo_full.split("/")[-1]
                    wt_path = os.path.join(full, repo_name)
                    if os.path.isdir(wt_path):
                        repo_path = os.path.join(self.cfg.repos_dir, *repo_full.split("/", 1))
                        try:
                            await git_ops.remove_worktree(repo_path, wt_path)
                        except Exception as exc:
                            logger.debug("startup-sweep remove_worktree failed: %s", exc)
                # Whatever git left behind, drop the directory.
                import shutil

                try:
                    shutil.rmtree(full, ignore_errors=True)
                except Exception as exc:
                    logger.debug("startup-sweep rmtree failed for %s: %s", full, exc)
            # Prune dangling worktree references in each repo.
            for repo_full in known_repos:
                repo_path = os.path.join(self.cfg.repos_dir, *repo_full.split("/", 1))
                if os.path.isdir(repo_path):
                    try:
                        await git_ops.prune_worktrees(repo_path)
                    except Exception as exc:
                        logger.debug("prune_worktrees failed for %s: %s", repo_path, exc)
        except Exception as exc:
            logger.warning("Worktree startup sweep failed: %s", exc)

    async def _worktree_sweeper(self) -> None:
        """Periodically retire worktrees older than ``WORKTREE_TTL_SECONDS``."""
        logger.info(
            "Worktree sweeper started (TTL=%ds, interval=%ds)",
            WORKTREE_TTL_SECONDS,
            WORKTREE_SWEEP_INTERVAL_SECONDS,
        )
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=WORKTREE_SWEEP_INTERVAL_SECONDS,
                )
                return  # shutdown
            except TimeoutError as exc:
                logger.debug("worktree sweep interval elapsed: %s", exc)
            now = asyncio.get_event_loop().time()
            stale: list[str] = []
            active_task_ids = {s.current_task_id for s in self.agents if s.current_task_id}
            for task_id, entries in list(self._task_worktrees.items()):
                if task_id in active_task_ids:
                    continue
                if all(now - ts > WORKTREE_TTL_SECONDS for _rp, _wt, ts in entries):
                    stale.append(task_id)
            for tid in stale:
                logger.info("Sweeper: retiring worktrees for task %s", tid)
                await self._release_task_worktrees(tid)

    # ------------------------------------------------------------------ S3 syncer
    async def _s3_syncer(self) -> None:
        """Periodically sync session-log directories to S3."""
        logger.info(
            "S3 syncer started (bucket=%s prefix=%r paths=%s interval=%.0fs)",
            self.cfg.s3_bucket,
            self.cfg.s3_prefix,
            self.cfg.s3_paths,
            self.cfg.s3_sync_interval,
        )
        bucket = self.cfg.s3_bucket
        if not bucket:
            return
        # Sync immediately at startup so the most recent session state is captured.
        await s3_uploader.sync_paths(
            bucket=bucket,
            prefix=self.cfg.s3_prefix,
            paths=self.cfg.s3_paths,
        )
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.cfg.s3_sync_interval,
                )
                break  # shutdown
            except TimeoutError as exc:
                logger.debug("s3 sync interval elapsed: %s", exc)
            await s3_uploader.sync_paths(
                bucket=bucket,
                prefix=self.cfg.s3_prefix,
                paths=self.cfg.s3_paths,
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
            except TimeoutError as exc:
                logger.debug("idle pull interval elapsed: %s", exc)
            # Do not poll REST for tasks. Task assignment/replay is owned by
            # the backend over WebSocket; polling here creates a second queueing
            # path inside the worker and can duplicate assignments.
            now = asyncio.get_event_loop().time()
            if (
                self._github_token_dynamic
                and now - self._last_github_token_refresh > GITHUB_TOKEN_REFRESH_INTERVAL_SECONDS
            ):
                await self._refresh_github_token()
            if (
                self.cfg.github_token
                and now - self._last_repo_refresh > REPO_REFRESH_INTERVAL_SECONDS
            ):
                await self._refresh_github_repos()
            all_idle = all(s.current_claude is None for s in self.agents)
            if self.task_queue.empty() and all_idle:
                known = self._known_repos()
                if known:
                    try:
                        await git_ops.pull_repos(
                            self.cfg.repos_dir,
                            known,
                            self.cfg.github_token,
                            self._emit,
                        )
                    except Exception as exc:
                        logger.warning("Idle repo pull failed: %s", exc)

    # ------------------------------------------------------------------ Agent loop
    async def _agent_loop(self, slot: Agent) -> None:
        logger.info("Agent loop started for %s", slot.agent_id)
        while True:
            task = await self.task_queue.get()
            if task is _SHUTDOWN_SENTINEL:
                logger.info("Agent %s: shutdown sentinel received; exiting", slot.agent_id)
                break
            task = cast(dict, task)
            task_id = task.get("id")
            target_agent_id = task.get("target_agent_id")
            if target_agent_id and target_agent_id != slot.agent_id:
                await self.task_queue.put(task)
                await asyncio.sleep(0.1)
                continue
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
                await self._emit(f"✗ Internal error on task {task_id}: {exc}")
                await self._task_update(
                    task["id"], agent=slot, state="failed", finishedAt=_now_iso()
                )
            finally:
                slot.current_task_id = None
        try:
            await self._set_state("offline", slot)
        except Exception as exc:
            logger.debug("Failed to send offline state for %s: %s", slot.agent_id, exc)
        logger.info("Agent loop exited for %s", slot.agent_id)

    # ------------------------------------------------------------------ Execution
    async def _execute_task(self, task: dict, agent: Agent) -> None:
        task_id = task["id"]
        desc = task.get("description") or ""
        # Clone/fetch as the triggering user's OAuth token, same as push does
        # below. The App installation token (self.cfg.github_token) is scoped to
        # one org, so a remote worker can't clone a repo the App isn't installed
        # on; the user's token can. Falls back to the App token per _task_github_token.
        token = await self._task_github_token(task_id)
        issue_repo = task.get("issue_repo") or ""
        explicit_repos = task.get("repos") or []
        if explicit_repos:
            org_prefix = f"{self.cfg.org}/" if self.cfg.org else None
            repos = [
                r
                for r in explicit_repos
                if r in self.cfg.repos or (org_prefix and r.startswith(org_prefix))
            ] or list(self.cfg.repos)
        else:
            repos = list(self.cfg.repos)
        if issue_repo and issue_repo not in repos:
            if self.cfg.org and issue_repo.startswith(f"{self.cfg.org}/"):
                repos.insert(0, issue_repo)
        logger.info("Task %s: repos=%s", task_id, repos)
        followup_instructions = task.get("followup_instructions") or ""
        # When the idle puller picks up a follow-up task via REST, the task dict
        # contains `branch` (the DB column) but not `followup_branch` (a
        # WS-path key set only by the task-followup message handler).  Fall back
        # to `task.get("branch")` so we never generate a fresh branch name for a
        # task that already has an open PR.
        followup_branch = task.get("followup_branch") or task.get("branch") or ""
        # phase="followup" is the DB marker for tasks dispatched by send_followup.
        # The idle puller path has no followup_instructions (those lived only in
        # the WS message), so we must check phase as well.
        is_followup = bool(followup_instructions) or task.get("phase") == "followup"
        task_type = (task.get("task_type") or task.get("taskType") or "standard").lower()
        is_interactive = task_type == "interactive"
        phase = (task.get("phase") or "execute").lower()
        is_review = phase == "review"
        # Review tasks act on a PR, identified by the dedicated pr_number/pr_repo
        # fields. Fall back to issue_number/issue_repo for back-compat with the
        # deterministic Discord path (which mirrors the PR number into both).
        pr_repo = task.get("pr_repo") or issue_repo
        pr_number = task.get("pr_number") or task.get("issue_number")

        logger.info(
            "Executing task %s (agent=%s, followup=%s): %s",
            task_id,
            agent.agent_id,
            is_followup,
            desc[:120],
        )
        logger.info(
            "Task %s metadata at start: phase=%s issue_repo=%s pr_repo=%s pr_number=%s "
            "pr_head_ref=%s pr_url=%s head_sha=%s",
            task_id,
            phase,
            issue_repo,
            pr_repo,
            pr_number,
            task.get("pr_head_ref"),
            task.get("pr_url"),
            task.get("head_sha"),
        )
        await self._set_state("working", agent)
        await self._task_update(task_id, agent=agent, state="working")

        emit = self._task_emit(task_id, agent)

        # Resolve the tool to dispatch with. Never default to "claude" blindly —
        # fall back to this worker's own detected/configured tools instead, and
        # refuse outright if the foreman asked for a tool we don't actually have
        # (e.g. a worker with tools=["pi"] must never end up invoking claude).
        requested_tool = (task.get("tool") or "").lower()
        if requested_tool:
            tool = requested_tool
        elif self._available_tools:
            tool = self._available_tools[0]
        else:
            tool = (self.cfg.tool or "claude").lower()

        if self._available_tools and tool not in self._available_tools:
            logger.error(
                "Task %s: tool %r is not among this worker's available tools %s — aborting",
                task_id,
                tool,
                self._available_tools,
            )
            await emit(
                f"✗ Tool {tool!r} is not available on this worker "
                f"(available: {', '.join(self._available_tools)}) — aborting.",
                level=LEVEL_WORKER,
            )
            await self._task_update(task_id, agent=agent, state="failed", finishedAt=_now_iso())
            await self._set_state("error", agent)
            await self._set_state("idle", agent)
            return

        name = task.get("name") or desc
        if is_review and not is_followup:
            # Fail fast, before ever touching git, when the metadata handoff
            # from assign_task dropped pr_repo/pr_number — this is the
            # contract review tasks depend on, and a clear error here beats
            # a confusing later failure when checkout has nothing to act on.
            if not pr_repo or not pr_number:
                logger.error(
                    "Task %s: review task missing required metadata (pr_repo=%s pr_number=%s) "
                    "— aborting before checkout",
                    task_id,
                    pr_repo,
                    pr_number,
                )
                await emit(
                    "✗ Review task is missing pr_repo/pr_number — the assigning foreman must "
                    "pass both when calling assign_task(phase='review'). Aborting.",
                    level=LEVEL_WORKER,
                )
                await self._task_update(task_id, agent=agent, state="failed", finishedAt=_now_iso())
                await self._set_state("error", agent)
                await self._set_state("idle", agent)
                return
            # Review tasks must operate on the PR's own branch — never a
            # freshly generated ps/... branch — or the agent won't see
            # the actual PR diff. Never fall back to a generated name here:
            # doing so would silently review the wrong code. Always re-resolve
            # live via the GitHub API rather than trusting task.get("pr_head_ref"):
            # the PR may have been force-pushed since the webhook fired.
            branch = await git_ops.get_pr_head_branch(pr_repo, pr_number)
            if not branch:
                logger.error(
                    "Task %s: could not resolve PR branch for review via GitHub API "
                    "(repo=%s pr=%s) — metadata was present but the API lookup failed",
                    task_id,
                    pr_repo,
                    pr_number,
                )
                await emit(
                    "✗ Could not resolve PR branch for review — aborting.", level=LEVEL_WORKER
                )
                await self._task_update(task_id, agent=agent, state="failed", finishedAt=_now_iso())
                await self._set_state("error", agent)
                await self._set_state("idle", agent)
                return
        else:
            # On follow-ups we must continue on the existing branch — the original
            # worker pushed it and the foreman wants the same PR updated.
            if followup_branch:
                branch = followup_branch
            else:
                # Prefer the linked GitHub issue number as the branch prefix
                # (e.g. "1158/...") so the branch is traceable to its issue at
                # a glance; fall back to the generic "ps/" prefix when the
                # task has no linked issue.
                issue_number = task.get("issue_number")
                branch_prefix = str(issue_number) if issue_number else "ps"
                branch = f"{branch_prefix}/{_slug(name)}-{task_id}"
        work_dir = os.path.join(
            self.cfg.work_dir, self.cfg.guild_id, self.cfg.worker_id or "", task_id
        )
        logger.info(
            "Task %s branch=%s work_dir=%s followup=%s", task_id, branch, work_dir, is_followup
        )
        try:
            os.makedirs(work_dir, exist_ok=True)
        except OSError as exc:
            logger.error("Task %s: failed to create work dir %s: %s", task_id, work_dir, exc)
            await emit(f"✗ Work dir failed: {work_dir}", level=LEVEL_WORKER)
            await self._task_update(task_id, agent=agent, state="failed", finishedAt=_now_iso())
            await self._set_state("error", agent)
            await self._set_state("idle", agent)
            return

        if is_followup:
            await emit(f"Follow-up: {followup_instructions[:120]}", level=LEVEL_WORKER)
            await emit(f"Branch: {branch}", level=LEVEL_WORKER)
        else:
            await emit(f"Task: {desc}", level=LEVEL_WORKER)
            await emit(f"Branch: {branch}", level=LEVEL_WORKER)

        worktree_entries: list[tuple[str, str, str]] = []
        primary_wt: str | None = None

        for repo_full in repos:
            logger.info("Task %s: preparing repo %s", task_id, repo_full)
            await emit(f"Preparing {repo_full}...", level=LEVEL_WORKER)
            # Surface the first-time clone (a slow op) so the UI isn't silent
            # while git fetches the repo; subsequent runs just fast-forward.
            repo_parts = repo_full.split("/", 1)
            already_cloned = len(repo_parts) == 2 and os.path.isdir(
                os.path.join(self.cfg.repos_dir, repo_parts[0], repo_parts[1], ".git")
            )
            if not already_cloned:
                await emit(
                    f"Cloning {repo_full} (first run, may take a moment)…", level=LEVEL_WORKER
                )
            repo_path = await git_ops.ensure_repo(self.cfg.repos_dir, repo_full, token)
            if not repo_path:
                logger.error("Task %s: clone/fetch failed for %s", task_id, repo_full)
                await emit(f"✗ Clone failed: {repo_full}", level=LEVEL_WORKER)
                continue
            repo_name = repo_full.split("/")[-1]
            wt_path = os.path.join(work_dir, repo_name)
            if (
                os.path.isdir(wt_path)
                and os.path.isdir(os.path.join(wt_path, ".git"))
                or (os.path.isdir(wt_path) and os.path.isfile(os.path.join(wt_path, ".git")))
            ):
                # Worktree from a prior run on the same task — reuse it.
                logger.info("Task %s: reusing worktree at %s", task_id, wt_path)
                await emit(f"Reusing worktree {repo_name}", level=LEVEL_WORKER)
                if is_followup or is_review:
                    # Pull latest so we don't clobber commits pushed since the
                    # last follow-up or by other workers.
                    await git_ops.run_git(["fetch", "origin", branch], cwd=wt_path, token=token)
                    await git_ops.run_git(["reset", "--hard", f"origin/{branch}"], cwd=wt_path)
                worktree_entries.append((repo_full, repo_path, wt_path))
                if primary_wt is None:
                    primary_wt = wt_path
                continue
            if is_followup:
                logger.info("Task %s: attaching worktree %s to branch %s", task_id, wt_path, branch)
                # attach_worktree fetches origin/<branch> before checking it out, so the
                # new worktree starts at the latest remote commit — no separate pull needed.
                # Pull latest so we don't clobber commits pushed since the last follow-up
                # or by other workers.
                ok = await git_ops.attach_worktree(repo_path, wt_path, branch, token)
                if not ok:
                    # Branch never reached origin (e.g. original task failed before push).
                    # Fall back to creating a fresh branch so the follow-up can still run.
                    logger.warning(
                        "Task %s: attach failed for %s — branch %s not found on origin; "
                        "falling back to create_worktree",
                        task_id,
                        repo_full,
                        branch,
                    )
                    await emit(
                        f"Branch not found on origin; creating fresh branch {branch[:50]}",
                        level=LEVEL_WORKER,
                    )
                    ok = await git_ops.create_worktree(repo_path, wt_path, branch, token)
            elif is_review and pr_repo and repo_full.lower() == pr_repo.lower():
                # Check out the PR's own branch via `gh pr checkout` instead of
                # `git checkout -b` — review tasks read an existing PR, they
                # never create a new branch. Compared case-insensitively since
                # GitHub repo slugs are case-insensitive.
                logger.info(
                    "Task %s: checking out PR #%s (%s) into worktree %s",
                    task_id,
                    pr_number,
                    repo_full,
                    wt_path,
                )
                assert pr_number is not None
                ok = await git_ops.checkout_pr_worktree(
                    repo_path, wt_path, pr_number, repo_full, token
                )
            else:
                if is_review:
                    logger.warning(
                        "Task %s: repo %s does not match PR repo %s (case-insensitive) — "
                        "falling back to create_worktree instead of gh pr checkout",
                        task_id,
                        repo_full,
                        pr_repo,
                    )
                logger.info("Task %s: creating worktree %s on branch %s", task_id, wt_path, branch)
                ok = await git_ops.create_worktree(repo_path, wt_path, branch, token)
            if ok:
                logger.info("Task %s: worktree ready at %s", task_id, wt_path)
                await emit(f"Worktree ready: {repo_name}", level=LEVEL_WORKER)
                worktree_entries.append((repo_full, repo_path, wt_path))
                if primary_wt is None:
                    primary_wt = wt_path
            else:
                logger.error("Task %s: worktree failed for %s", task_id, repo_full)
                await emit(f"✗ Worktree failed: {repo_full}", level=LEVEL_WORKER)

        if not primary_wt:
            logger.error("Task %s: no worktrees created — aborting", task_id)
            await emit("✗ No worktrees — aborting.", level=LEVEL_WORKER)
            await self._task_update(task_id, agent=agent, state="failed", finishedAt=_now_iso())
            await self._set_state("error", agent)
            # Return the slot to idle like every other abort path. An agent left
            # in "error" is invisible to the backend's follow-up routing (it
            # selects on state == "idle"), and the only thing that would clear
            # the state is another task — which it can no longer be given. One
            # repo that reliably fails to clone would otherwise retire the
            # worker's slots one at a time.
            await self._set_state("idle", agent)
            return

        await self._task_update(task_id, agent=agent, branch=branch, worktreePath=primary_wt)
        # Register worktrees for the deferred sweeper — touched again below in
        # finally so the timestamp reflects the most recent activity.
        self._register_worktrees(task_id, worktree_entries)

        # Tracks the last agent session ID so redirects and same-worker follow-ups
        # can resume with full context. Seeded from the task's saved session_id
        # when this is a follow-up dispatched back to the worker that ran it.
        resume_session_id: str | None = task.get("session_id") if is_followup else None

        # Queue for redirect instructions; listener puts new instructions here after SIGTERM
        redirect_q: asyncio.Queue = asyncio.Queue()
        self._redirect_queues[task_id] = redirect_q

        def _capture_session_and_clear() -> None:
            """Save session_id from the just-finished ClaudeProcess, then clear the slot."""
            nonlocal resume_session_id
            if agent.current_claude is not None:
                if agent.current_claude.session_id:
                    resume_session_id = agent.current_claude.session_id
                agent.current_claude = None

        def _on_proc(proc: ProcessHandle) -> None:
            agent.current_claude = proc

        # Per-API-call usage captured from the claude stream across all runs of
        # this task (the redirect loop may invoke claude more than once). Each
        # api_call gets a task-global call_index; reported to the backend once
        # the run finishes.
        usage_records: list[dict] = []
        _api_call_n = 0

        async def _collect_usage(rec: dict) -> None:
            nonlocal _api_call_n
            if rec.get("kind") == "api_call":
                rec = {**rec, "call_index": _api_call_n}
                _api_call_n += 1
            usage_records.append(rec)

        try:
            # ── Main execution with redirect loop ──────────────────────────
            if is_followup:
                if is_review:
                    current_desc = (
                        f"You are continuing a review of branch `{branch}` for task {task_id}.\n\n"
                        f"Original review task:\n{desc}\n\n"
                        f"Follow-up instructions:\n{followup_instructions}\n\n"
                        "Post updated findings via `gh pr review` — do NOT commit changes, "
                        "push, or open a new PR."
                    )
                else:
                    issue_ref = (
                        f"#{task['issue_number']} in {issue_repo}"
                        if task.get("issue_number") and issue_repo
                        else "the linked GitHub issue, if any"
                    )
                    current_desc = (
                        f"You are continuing existing work on branch `{branch}` for task {task_id}.\n\n"
                        f"Original task:\n{desc}\n\n"
                        f"Follow-up instructions:\n{followup_instructions}\n\n"
                        "Before acting on any reviewer comment above:\n"
                        f"1. Re-read {issue_ref} — it is the source of truth for this work's intent.\n"
                        "2. If a comment contradicts the issue's stated goal, do NOT implement it. "
                        'Instead post a reply on the PR: "Declining this suggestion — it contradicts '
                        "the intent of issue #NNN which requires [quote the relevant part]. The "
                        'current implementation is correct." Resolve/dismiss the review comment via '
                        "the GitHub API if it allows it.\n"
                        "3. A minor nit unrelated to intent (style, naming, whitespace) may be accepted "
                        "at your discretion, but you are not obligated to.\n"
                        "4. Never silently invert this feature's behaviour because a reviewer asked for "
                        "it without checking the issue first. Use assertive language in commit messages "
                        'and PR replies ("Keeping X as required by issue #NNN"), not apologetic '
                        'language ("I\'ve decided not to change this").\n\n'
                        "Make the requested changes, then commit and push:\n"
                        f'  git add -A && git commit -m "<concise commit message>"\n'
                        f"  git push origin {branch}\n"
                        "If a PR already exists for this branch, no new PR is needed."
                    )
            else:
                current_desc = desc
                if is_review:
                    # Review-phase tasks must post findings as GitHub PR review
                    # comments — NEVER by committing files and opening a new PR.
                    if pr_repo and pr_number:
                        _pr_ref = f"https://github.com/{pr_repo}/pull/{pr_number}"
                        _api_ref = f"repos/{pr_repo}/pulls/{pr_number}"
                    else:
                        _pr_ref = "<PR-URL>"
                        _api_ref = "repos/OWNER/REPO/pulls/NUMBER"
                    current_desc = (
                        f"{desc}\n\n"
                        "IMPORTANT — this is a review-phase task.\n"
                        "If you are reviewing a pull request:\n"
                        "  - Post your findings directly as a GitHub PR review using the gh CLI or API.\n"
                        "  - NEVER create a new branch, commit review findings to files, or open a new PR.\n"
                        "  - The review is complete when you post it, for example:\n"
                        f"      gh pr review {_pr_ref} --comment --body '<your findings>'\n"
                        "    or for APPROVE/REQUEST_CHANGES:\n"
                        f"      gh pr review {_pr_ref} --approve --body '<comment>'\n"
                        f"      gh pr review {_pr_ref} --request-changes --body '<what needs fixing>'\n"
                        "    or via the API for inline comments:\n"
                        f"      gh api {_api_ref}/reviews \\\n"
                        "        -f body='...' -f event='COMMENT|APPROVE|REQUEST_CHANGES' \\\n"
                        "        -f 'comments[][path]=file.py' -f 'comments[][line]=42' \\\n"
                        "        -f 'comments[][body]=inline comment'\n"
                        "Do NOT push any commits or open a PR as part of this review.\n"
                    )

                # plan / ephemeral / automation phases: leave current_desc = desc unchanged
            result = RunResult(False, StopReason.NO_EVENTS)
            success = result.success
            stop_reason = result.stop_reason
            last_msg = result.final_message
            runner = self._runners[tool]
            # Scoped environment for this tool: shared vars + this tool's own set,
            # with the other tools' credentials deliberately excluded.
            tool_env = self._env_for_tool(tool)

            async def _run_current_desc() -> RunResult:
                return await runner.run(
                    RunRequest(
                        description=current_desc,
                        cwd=primary_wt,
                        emit=emit,
                        env=tool_env,
                        on_usage=_collect_usage,
                        on_proc=_on_proc,
                        resume_session_id=resume_session_id,
                        model=task.get("model") or None,
                        provider=task.get("provider") or None,
                    )
                )

            def _apply_result(run_result: RunResult) -> None:
                nonlocal result, success, stop_reason, last_msg, resume_session_id
                result = run_result
                success = run_result.success
                stop_reason = run_result.stop_reason
                last_msg = run_result.final_message
                resume_session_id = run_result.session_id

            if is_interactive:
                interactive_q: asyncio.Queue = asyncio.Queue()
                self._interactive_queues[task_id] = interactive_q
                await emit(
                    f"Interactive {tool} session. Send messages in this task window; "
                    "cancel to close.",
                    level=LEVEL_WORKER,
                )
                try:
                    while True:
                        _apply_result(await _run_current_desc())
                        _capture_session_and_clear()

                        await self._task_update(
                            task_id,
                            agent=agent,
                            state="working",
                            workerId=self.cfg.worker_id,
                            agentId=agent.agent_id,
                            stopReason=stop_reason,
                            branch=branch,
                            worktreePath=primary_wt,
                            sessionId=resume_session_id or "",
                            lastText=last_msg,
                        )
                        if task_id in self._cancelled_tasks:
                            break
                        await emit(f"[{tool}] Waiting for your next message…", level=LEVEL_WORKER)
                        next_msg = await interactive_q.get()
                        if next_msg is _CANCEL_SENTINEL:
                            break
                        current_desc = str(next_msg)

                    await emit("Interactive session closed.", level=LEVEL_WORKER)
                    await self._task_update(
                        task_id, agent=agent, state="cancelled", finishedAt=_now_iso()
                    )
                    await self._release_task_worktrees(task_id)
                finally:
                    self._interactive_queues.pop(task_id, None)
                    agent.current_claude = None
                    await self._set_state("idle", agent)
                return

            while True:
                logger.info(
                    "Task %s: launching %s in %s (model=%s provider=%s resume=%s)",
                    task_id,
                    tool,
                    primary_wt,
                    task.get("model") or None,
                    task.get("provider") or None,
                    resume_session_id,
                )
                _apply_result(await _run_current_desc())
                _capture_session_and_clear()
                logger.info(
                    "Task %s: run done success=%s stop=%s session=%s",
                    task_id,
                    success,
                    stop_reason,
                    resume_session_id,
                )

                if task_id in self._cancelled_tasks:
                    await emit("Task cancelled.", level=LEVEL_WORKER)
                    await self._task_update(
                        task_id, agent=agent, state="cancelled", finishedAt=_now_iso()
                    )
                    await self._release_task_worktrees(task_id)
                    await self._set_state("idle", agent)
                    return

                try:
                    redirect_instr = redirect_q.get_nowait()
                except asyncio.QueueEmpty:
                    redirect_instr = None

                if redirect_instr is _CANCEL_SENTINEL:
                    await emit("Task cancelled.", level=LEVEL_WORKER)
                    await self._task_update(
                        task_id, agent=agent, state="cancelled", finishedAt=_now_iso()
                    )
                    await self._release_task_worktrees(task_id)
                    await self._set_state("idle", agent)
                    return

                if redirect_instr is not None and not self._shutdown_event.is_set():
                    await emit(f"↩ Redirected: {redirect_instr[:120]}", level=LEVEL_WORKER)
                    current_desc = redirect_instr
                    await self._task_update(task_id, agent=agent, state="working")
                    continue

                break  # normal exit from redirect loop

            logger.info(
                "Task %s: final success=%s stop_reason=%s",
                task_id,
                success,
                stop_reason,
            )

            # Report captured per-API-call usage. Best-effort.
            if usage_records:
                await self._report_usage(
                    task_id=task_id,
                    tool=tool,
                    session_id=resume_session_id,
                    repo=repos[0] if repos else None,
                    records=usage_records,
                )

            # Push/PR are attributed to the human who triggered the task (their
            # OAuth token), falling back to the App token — see _task_github_token.
            push_token = await self._task_github_token(task_id)
            if is_review:
                # Review tasks only read the PR and post a `gh pr review` —
                # never push commits or auto-commit stray local changes. Preserve
                # the explicit PR metadata assigned by the foreman instead of
                # relying on branch lookup (which can fail for checked-out PR refs).
                await emit("Review phase — skipping push (read-only task).", level=LEVEL_WORKER)
                pr_url = task.get("pr_url") or (
                    f"https://github.com/{pr_repo}/pull/{pr_number}"
                    if pr_repo and pr_number
                    else None
                )
                if not pr_url:
                    pr_url = await github_pr.find_existing_pr(
                        branch=branch, worktree_path=primary_wt, token=push_token
                    )
            else:
                # Push the branch so partial work is visible and a follow-up can
                # build on it. push_branch returns "pushed" | "nothing" | "failed".
                push_result = await github_pr.push_branch(
                    branch=branch,
                    worktree_path=primary_wt,
                    token=push_token,
                    emit=emit,
                )
                if push_result == "failed" and success:
                    # A genuine push failure (e.g. git permission denied) after an
                    # agent "success" must not surface as a reviewable result.
                    logger.warning(
                        "Task %s: agent reported success but push failed — marking error",
                        task_id,
                    )
                    _apply_result(result.with_stop_reason(StopReason.PUSH_FAILED))
                elif push_result == "nothing" and success:
                    # The agent claimed success but left no commits — usually it
                    # was blocked (e.g. every tool call denied) and never did the
                    # work. Don't mask that as a reviewable result.
                    logger.warning(
                        "Task %s: agent reported success but produced no commits — marking error",
                        task_id,
                    )
                    await emit(
                        "Agent reported success but produced no commits — flagging for review.",
                        level=LEVEL_WORKER,
                    )
                    _apply_result(result.with_stop_reason(StopReason.NO_CHANGES))

                pr_url = await github_pr.find_existing_pr(
                    branch=branch, worktree_path=primary_wt, token=push_token
                )
                if not pr_url and push_result == "pushed" and task.get("create_pr"):
                    # PR creation is no longer automatic (#1095) — the branch is
                    # pushed and the task parks in awaiting-foreman-review. A PR
                    # is only opened here when the foreman explicitly requests
                    # one via send_followup(create_pr=true).
                    pr_url = await github_pr.open_pr(
                        task=task,
                        branch=branch,
                        worktree_path=primary_wt,
                        token=push_token,
                        emit=emit,
                    )

            if pr_url:
                label = "Reviewed PR" if is_review else "✓ PR"
                await emit(f"{label}: {pr_url}", level=LEVEL_WORKER)
                await self._ensure_pr_webhook(pr_url, emit)

            logger.info("Task %s: pr_url=%s", task_id, pr_url)
            msg = {
                "workerId": self.cfg.worker_id,
                "agentId": agent.agent_id,
                "taskId": task_id,
                "stopReason": stop_reason,
                "branch": branch,
                "worktreePath": primary_wt,
                "sessionId": resume_session_id or "",
                "prUrl": pr_url or "",
                "lastText": last_msg,
            }

            if success:
                # A PR only exists here if one was already open (find_existing_pr)
                # or the foreman explicitly requested one for this follow-up
                # (create_pr=true) — see the open_pr call above (#1095). With a
                # PR open, GitHub-webhook-driven review takes over; without one,
                # the task parks for the foreman to decide the next step.
                await self._task_update(
                    task_id,
                    success=True,
                    type="task-followup-done" if is_followup else "task-complete",
                    agent=agent,
                    state="awaiting-review" if pr_url else "awaiting-foreman-review",
                    **msg,
                )

            else:
                logger.warning("Task %s failed: %s", task_id, stop_reason)
                if stop_reason == StopReason.NEEDS_INPUT:
                    # Human-escalation path: a dedicated message type so the
                    # backend can surface it distinctly (handle_needs_input).
                    await self._task_update(
                        task_id, agent=agent, type="needs-input", state="error", **msg
                    )
                else:
                    # Plain task-update (default type) so this reaches
                    # handle_task_update, which persists state="error", posts
                    # a Discord notification, and triggers the foreman (#1171)
                    # — a bare type="error" override used to have no backend
                    # handler at all and was silently dropped.
                    await self._task_update(task_id, agent=agent, state="error", **msg)
                await self._set_state("error", agent)

            # The slot returns to idle here. Worktrees stay on disk so the
            # foreman can dispatch a follow-up to this same worker without
            # paying for a re-clone; the deferred sweeper reclaims them
            # after WORKTREE_TTL_SECONDS.
            await self._set_state("idle", agent)

        finally:
            self._redirect_queues.pop(task_id, None)
            # Refresh the worktree-registry timestamp so the sweeper holds
            # off on this task for another full TTL window.
            self._touch_task_worktrees(task_id)
