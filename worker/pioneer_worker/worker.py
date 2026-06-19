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

from . import claude_runner, codex_runner, git_ops, github_pr, pi_runner, s3_uploader
from . import config as config_mod
from .control_api import ControlServer
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
        self.current_claude: claude_runner.ClaudeProcess | None = None
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
        self.ws = WSClient(cfg.ws_url)
        self._shutdown_event = asyncio.Event()
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
        self.task_queue: asyncio.Queue[dict] = asyncio.Queue()
        # Guards against re-queueing the same task-id on reconnect or poll.
        self._known_task_ids: set[str] = set()
        # Tasks cancelled by the foreman or a human; checked before/during execution.
        self._cancelled_tasks: set[str] = set()
        # Per-task redirect-instruction queues (SIGTERM + --resume flow).
        self._redirect_queues: dict[str, asyncio.Queue] = {}
        # Worktrees materialised for each task, kept alive within the TTL window
        # so follow-ups can reuse the existing checkout without a re-clone.
        # Keyed by task_id; each entry is a list of (repo_path, wt_path, last_used_monotonic).
        self._task_worktrees: dict[str, list[tuple[str, str, float]]] = {}

        # ── Auth / repo state ────────────────────────────────────────────────
        # Queue for auth codes received from the UI during claude auth login.
        self._auth_code_queue: asyncio.Queue[str] | None = None
        # Set to True once _join() has been called the first time so that
        # _on_ws_reconnect doesn't prematurely join before auth completes.
        self._joined = False
        # Monotonic timestamp of the last successful GitHub repo-list refresh.
        # 0 means never refreshed; _idle_puller compares against this.
        self._last_repo_refresh: float = 0.0
        # Merged repo list for worker-register broadcasts (static config repos
        # plus API-discovered repos). Never used for task execution — only for
        # telling the backend/UI how many repos this worker can see.
        self._broadcast_repos: list[str] = list(cfg.repos)
        # Available tool runners detected at startup (e.g. ["claude", "codex", "pi"]).
        self._available_tools: list[str] = []

    # ------------------------------------------------------------------ HTTP
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
                    "hostname": socket.gethostname(),
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
        if not self.cfg.github_token:
            try:
                async with await self._http(authed=True) as client:
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

        if self.cfg.github_token and not os.environ.get("GITHUB_TOKEN"):
            os.environ["GITHUB_TOKEN"] = self.cfg.github_token
            logger.info("GITHUB_TOKEN set in environment for gh CLI and subprocesses")

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
            env_vars = resp.json().get("env_vars", [])
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
        except Exception as exc:
            logger.warning("Could not fetch foreman env vars: %s", exc)

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
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
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
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return

        output = stdout.decode(errors="replace").strip()
        if proc.returncode == 0:
            logger.info("codex doctor: ok\n%s", output)
        else:
            logger.warning("codex doctor: failed (rc=%d)\n%s", proc.returncode, output)

    async def _ensure_codex_api_key(self) -> None:
        """Warn early if no OpenAI API key is available for Codex tasks.

        The key is passed explicitly to each run_codex_auto call (via the
        openai_api_key parameter) rather than being injected into os.environ,
        so this method only checks and logs — it does not mutate global state.
        """
        if self.cfg.openai_api_key is not None:
            logger.info("OPENAI_API_KEY configured — Codex tasks will use it")
        else:
            logger.warning(
                "OPENAI_API_KEY not configured — Codex tasks will fail. "
                "Set openai_api_key in the [codex] config block or the OPENAI_API_KEY env var."
            )

    def _detect_available_tools(self) -> None:
        """Populate self._available_tools based on which runner binaries are on PATH.

        When cfg.tools is set, only the listed names are probed; any that aren't found
        on PATH are excluded with a warning.  When cfg.tools is None (default), all
        known tool binaries are probed automatically.
        """
        import os
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
                return os.path.isfile(path) and os.access(path, os.X_OK)
            return False

        tool_paths = {
            "claude": self.cfg.claude_path,
            "codex": self.cfg.codex_path,
            "pi": self.cfg.pi_path,
        }
        candidates = self.cfg.tools if self.cfg.tools is not None else list(tool_paths)

        tools: list[str] = []
        for name in candidates:
            binary = tool_paths.get(name)
            if binary is None:
                logger.warning("Unknown tool %r in --tools list; skipping", name)
                continue
            if _is_executable(binary):
                tools.append(name)
            else:
                logger.warning(
                    "Tool %r not found on PATH (checked %r); excluding from available tools",
                    name,
                    binary,
                )
        self._available_tools = tools
        logger.info("Available tools: %s", tools or ["(none)"])

    async def _claude_is_authenticated(self) -> bool:
        """Return True if `claude auth status --json` reports loggedIn=true.

        Works on macOS keychain and Linux. The exit code alone is unreliable
        (the CLI returns 0 even when not logged in, just emits ``loggedIn:
        false``), so we parse the JSON. A timeout guards against macOS keychain
        access prompts that can hang the subprocess indefinitely when invoked
        without a controlling TTY.
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
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
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

    async def _check_claude_auth(self) -> None:
        """Ensure Claude is authenticated. Restores stored credentials or runs login flow."""
        import base64
        import io
        import json
        import tarfile
        from pathlib import Path

        if os.environ.get("ANTHROPIC_API_KEY"):
            logger.info("ANTHROPIC_API_KEY set — skipping Claude login flow")
            return

        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            logger.info("CLAUDE_CODE_OAUTH_TOKEN already in env — skipping login")
            return

        if await self._claude_is_authenticated():
            logger.info("Claude credentials already present")
            return

        # Try restoring credentials stored in the backend. Two formats are
        # supported: the new JSON {"oauth_token": "..."} blob produced by
        # `claude setup-token`, and the legacy base64(tar.gz of ~/.claude)
        # blob produced by older versions running `claude auth login`.
        try:
            async with await self._http(authed=True) as client:
                resp = await client.get(
                    "/auth/claude/credentials",
                    params={"guild_id": self.cfg.guild_id},
                )
            if resp.status_code == 200:
                blob = resp.json().get("credentials_blob", "")
                if blob:
                    raw = base64.b64decode(blob)
                    try:
                        payload = json.loads(raw)
                        token = payload.get("oauth_token")
                        if token:
                            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
                            logger.info(
                                "Restored CLAUDE_CODE_OAUTH_TOKEN from backend (len=%d)",
                                len(token),
                            )
                            # The env var is inherited by every spawned claude;
                            # no need to re-verify with `claude auth status`,
                            # which can spuriously fail on macOS keychain hosts.
                            return
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass  # fall through to legacy tarball path
                    claude_dir = Path.home() / ".claude"
                    claude_dir.mkdir(parents=True, exist_ok=True)
                    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
                        tar.extractall(path=Path.home())
                    logger.info("Restored Claude credentials tarball from backend (legacy)")
                    if await self._claude_is_authenticated():
                        return
                    logger.warning("Restored credentials blob but auth status still fails")
        except Exception as exc:
            logger.warning("Could not fetch Claude credentials from backend: %s", exc)

        await self._emit("No Claude credentials found — starting login...", level=LEVEL_AUTH)
        await self._run_claude_login()

    async def _run_claude_login(self) -> None:
        """Drive `claude setup-token` to completion and persist the resulting OAuth token.

        Why ``setup-token`` and not ``claude auth login``: the CLI ``auth login``
        command has no manual paste path — its only way to receive the auth
        code is via a localhost HTTP callback fired by the auto-opened browser,
        which fails in headless containers. ``setup-token`` runs an Ink
        (React-for-CLI) TUI that *does* prompt with "Paste code here if
        prompted >" and accepts the code over stdin. The trade-off is scope:
        the resulting token is ``user:inference`` only (sufficient for running
        Claude Code tasks, but not for Remote Control / file_upload / mcp).

        Mechanics: Ink mounts only when stdout is a real TTY and reads input
        only when it has a controlling terminal, so we allocate a PTY pair,
        plumb it into all three standard streams of the child, and use
        setsid + TIOCSCTTY in a preexec to make the slave PTY the child's
        ctty. The auth code must be written in two writes (paste, ~150ms
        delay, CR alone) — a single ``code\\r`` write doesn't fire onSubmit
        because Ink batches the CR into the paste event.
        """
        import base64
        import fcntl
        import json
        import os
        import pty
        import re
        import termios

        self._auth_code_queue = asyncio.Queue()
        logger.info("Starting claude setup-token — auth_code_queue is now open")

        master_fd, slave_fd = pty.openpty()
        # Disable input echo so the auth code we write isn't reflected onto
        # stdout (which would put the secret into our logs).
        try:
            attrs = termios.tcgetattr(slave_fd)
            attrs[3] &= ~termios.ECHO  # lflags
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
        except termios.error as exc:
            logger.debug("Could not disable PTY echo: %s", exc)

        def _attach_controlling_tty() -> None:
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        proc = await asyncio.create_subprocess_exec(
            self.cfg.claude_path,
            "setup-token",
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=_attach_controlling_tty,
        )
        os.close(slave_fd)
        logger.info(
            "claude setup-token started pid=%s (PTY mode, master_fd=%d, ctty=slave)",
            proc.pid,
            master_fd,
        )

        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        reader_protocol = asyncio.StreamReaderProtocol(reader)
        master_pipe = os.fdopen(master_fd, "rb", buffering=0)
        await loop.connect_read_pipe(lambda: reader_protocol, master_pipe)

        # Strip ANSI/CSI escape sequences so we can grep Ink's output. Match
        # the common cursor/SGR/erase forms claude emits.
        ansi_re = re.compile(
            rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b\][^\x07\x1b]*[\x07\x1b]"
        )

        def _clean(b: bytes) -> str:
            return ansi_re.sub(b"", b).decode(errors="replace")

        captured = bytearray()  # full raw output for token extraction at end
        url_seen = False
        code_sent = False
        post_submit_watchdog: asyncio.Task | None = None
        try:
            while True:
                try:
                    chunk = await reader.read(4096)
                except Exception as exc:
                    logger.warning("PTY read error: %s", exc)
                    break
                if not chunk:
                    break
                captured.extend(chunk)
                cleaned = _clean(chunk)
                for line in cleaned.splitlines():
                    line = line.rstrip()
                    if line:
                        logger.info("[claude setup-token] %s", line)
                        await self._emit(line, level=LEVEL_AUTH)

                # Detect the OAuth URL once it's been emitted. Ink word-wraps
                # the URL across multiple lines (often after each ~80 chars),
                # so we strip whitespace from the running buffer first and
                # then look for the URL ending at the OAuth state parameter.
                if not url_seen:
                    full_no_ws = re.sub(r"\s+", "", _clean(bytes(captured)))
                    m = re.search(
                        r"https://claude\.com/cai/oauth/authorize\?[A-Za-z0-9=&%_.\-]+state=[A-Za-z0-9_\-]+",
                        full_no_ws,
                    )
                    if m:
                        url = m.group(0)
                        url_seen = True
                        await self._send(
                            {
                                "type": "claude-auth-required",
                                "workerId": self.cfg.worker_id,
                                "url": url,
                            }
                        )
                        await self._emit(
                            "Waiting for auth code — paste it into the auth panel in the UI...",
                            level=LEVEL_AUTH,
                        )
                        logger.info("Auth login: awaiting code from queue (timeout=300s)")
                        try:
                            code = await asyncio.wait_for(
                                self._auth_code_queue.get(), timeout=300.0
                            )
                        except TimeoutError:
                            logger.warning("Auth login: timed out waiting for code from queue")
                            await self._emit(
                                "Timed out waiting for auth code — restart the worker to retry",
                                level=LEVEL_AUTH,
                            )
                            proc.kill()
                            await proc.wait()
                            return
                        logger.info(
                            "Auth login: code dequeued (len=%d, pid_alive=%s)",
                            len(code),
                            proc.returncode is None,
                        )
                        await self._emit(
                            "Code received — submitting to Claude CLI...", level=LEVEL_AUTH
                        )
                        try:
                            os.write(master_fd, code.strip().encode())
                            # Ink batches consecutive bytes into one paste
                            # event; a CR included in the same write is
                            # swallowed and never fires onSubmit. Sending CR
                            # in a separate write after a short sleep makes
                            # Ink treat it as a distinct keypress.
                            await asyncio.sleep(0.2)
                            os.write(master_fd, b"\r")
                        except OSError as exc:
                            logger.warning("Auth login: PTY write failed: %s", exc)
                            await self._emit(
                                f"Failed to send code to Claude: {exc}", level=LEVEL_AUTH
                            )
                            proc.kill()
                            await proc.wait()
                            return
                        logger.info("Auth login: wrote code + CR (separate writes) to PTY")
                        code_sent = True
                        post_submit_watchdog = asyncio.create_task(self._auth_login_watchdog(proc))
        finally:
            self._auth_code_queue = None
            if post_submit_watchdog is not None and not post_submit_watchdog.done():
                post_submit_watchdog.cancel()
            try:
                master_pipe.close()
            except OSError:
                pass

        logger.info(
            "setup-token: PTY EOF reached (code_sent=%s); waiting for claude to exit",
            code_sent,
        )
        await proc.wait()
        logger.info("claude setup-token exited with rc=%s", proc.returncode)

        # Extract the token from the captured Ink output. After Ink renders
        # the success view there's a contiguous run of the token characters
        # near "Store this token securely" / "export CLAUDE_CODE_OAUTH_TOKEN".
        cleaned_full = _clean(bytes(captured))
        token = self._extract_oauth_token(cleaned_full)
        if not token:
            await self._emit(
                "Login finished but could not extract OAuth token from output — please retry",
                level=LEVEL_AUTH,
            )
            logger.warning("Could not locate OAuth token in setup-token output")
            return

        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
        logger.info("Captured CLAUDE_CODE_OAUTH_TOKEN (len=%d) and set in env", len(token))
        await self._emit(
            "Token captured — saving to backend so future workers can reuse it",
            level=LEVEL_AUTH,
        )

        try:
            blob = base64.b64encode(json.dumps({"oauth_token": token}).encode()).decode()
            async with await self._http(authed=True) as client:
                await client.post(
                    "/auth/claude/credentials",
                    json={"guild_id": self.cfg.guild_id, "credentials_blob": blob},
                )
            await self._emit("Credentials saved", level=LEVEL_AUTH)
            logger.info("Posted OAuth token to backend credentials store")
        except Exception as exc:
            logger.warning("Could not store Claude credentials: %s", exc)
            await self._emit(f"Warning: could not store credentials: {exc}", level=LEVEL_AUTH)

    @staticmethod
    def _extract_oauth_token(cleaned_output: str) -> str | None:
        """Locate the OAuth token in Ink's success-screen output.

        The Ink TUI renders the token amid lots of surrounding text and ASCII
        art; after stripping ANSI escapes it shows up as a long alphanumeric
        run (40-100 chars from `[A-Za-z0-9_-]`) somewhere near the marker
        "Store this token securely" / "Use this token by setting".

        We anchor on those markers and pick the longest token-like run that
        appears near them.
        """
        import re

        text = cleaned_output
        # Filter out spaces/newlines that Ink injects between glyphs in its
        # box-layout, which would otherwise split the token character run.
        compact = re.sub(r"[\s]+", "", text)
        # The marker text gets compacted too.
        marker_idx = compact.find("Storethistokensecurely")
        if marker_idx < 0:
            marker_idx = compact.find("UsethistokenbysettingexportCLAUDE_CODE_OAUTH_TOKEN")
        if marker_idx < 0:
            return None
        # Search the 400 chars before the marker for the longest token-shaped run.
        window = compact[max(0, marker_idx - 400) : marker_idx]
        candidates = re.findall(r"[A-Za-z0-9_\-]{40,200}", window)
        if not candidates:
            return None
        # Prefer the run closest to the marker (latest in the window).
        return candidates[-1]

    async def _auth_login_watchdog(self, proc: asyncio.subprocess.Process) -> None:
        """Periodically log that we're still waiting on claude after the code was submitted.

        Helps diagnose hangs where the async-for loop is blocked because claude
        produced no further output (no newline, no exit) after stdin was sent.
        """
        ticks = 0
        try:
            while True:
                await asyncio.sleep(15.0)
                ticks += 1
                if proc.returncode is not None:
                    logger.info(
                        "Auth login watchdog: claude exited rc=%s (after %ds)",
                        proc.returncode,
                        ticks * 15,
                    )
                    return
                logger.warning(
                    "Auth login watchdog: still waiting on claude stdout %ds after code submission (pid=%s, rc=%s)",
                    ticks * 15,
                    proc.pid,
                    proc.returncode,
                )
        except asyncio.CancelledError:
            logger.debug("Auth login watchdog cancelled")
            raise

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
            "repos": body.get("repos") or [],
        }
        if body.get("followupInstructions"):
            task["followup_instructions"] = body["followupInstructions"]
        if body.get("followupBranch"):
            task["followup_branch"] = body["followupBranch"]
        return task

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
        await self._send(
            {
                "type": "worker-register",
                "workerId": self.cfg.worker_id,
                "repos": self._broadcast_repos,
                "tools": self._available_tools,
                **({"user": self.cfg.user} if self.cfg.user else {}),
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
        for slot in self.agents:
            if slot.state and slot.state != "idle":
                await self._emit_agent_state(slot)
        # Re-fetch any tasks that were assigned while the WS was down; without
        # this they'd be missed until the idle puller fires (up to 300s later).
        try:
            missed = await self._fetch_pending_tasks()
            for task in missed:
                if task["id"] not in self._known_task_ids:
                    logger.info("Reconnect: queuing missed task %s", task["id"])
                    self._known_task_ids.add(task["id"])
                    await self.task_queue.put(task)
        except Exception as exc:
            logger.warning("Reconnect: pending-task fetch failed: %s", exc)

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
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass
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

        await self._register()
        assert self.cfg.worker_id, "worker_id must be set after registration"

        await self._fetch_github_token_if_needed()
        await self._fetch_guild_env_vars()
        await self._refresh_github_repos()
        await self._check_gh_auth()
        await self._check_codex_doctor()
        await self._ensure_codex_api_key()

        logger.info("Connecting to backend WebSocket at %s", self.cfg.ws_url)
        self.ws.on_reconnect = self._on_ws_reconnect
        await self.ws.connect()

        # Start listener before auth so it can relay auth codes from the UI.
        # _join() is intentionally delayed until after auth — agents must not
        # be visible to the foreman until Claude is ready to accept tasks.
        listener = asyncio.create_task(self._listen())
        await self._check_claude_auth()
        self._detect_available_tools()

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

        initial = await self._fetch_pending_tasks()
        logger.info("Initial pending-task fetch: %d task(s)", len(initial))
        for task in initial:
            logger.info("Queuing task %s: %s", task.get("id"), task.get("description", "")[:80])
            self._known_task_ids.add(task["id"])
            await self.task_queue.put(task)

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
            with anyio.CancelScope(shield=True):
                await self._notify_offline()
            logger.info("Worker shutting down; closing WebSocket")
            await self.ws.close()
            self._stop_control_api()

    # ------------------------------------------------------------------ Listener
    async def _listen(self) -> None:
        logger.info("Listener started")
        # Message types safe to process before _join() completes — auth-related
        # messages flow during the pre-join window (claude setup-token), and
        # Everything else (task assignments, follow-ups, redirects, etc.) must
        # wait until we've actually joined, otherwise we'd start work before
        # the backend sees us online.
        _PRE_JOIN_ALLOWED = {"pong", "worker-message", "worker-auth-response"}
        async for msg in self.ws.messages():
            mtype = msg.get("type")
            logger.debug("WS message: type=%s keys=%s", mtype, list(msg.keys()))

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
                self._known_task_ids.add(task_id)
                await self.task_queue.put(
                    {
                        "id": task_id,
                        "worker_id": self.cfg.worker_id,
                        "guild_id": self.cfg.guild_id,
                        "name": msg.get("name", ""),
                        "description": msg.get("description", ""),
                        "tool": msg.get("tool", "claude"),
                        "model": msg.get("model"),
                        "provider": msg.get("provider"),
                        "phase": msg.get("phase", "execute"),
                        "issue_number": msg.get("issueNumber"),
                        "issue_repo": msg.get("issueRepo"),
                        "repos": msg.get("repos") or [],
                    }
                )

            elif mtype == "worker-message":
                if msg.get("workerId") != self.cfg.worker_id:
                    continue
                text = msg.get("message", "")
                if text:
                    # During Claude login the auth queue is open; treat the
                    # message as the auth code so the foreman can relay it.
                    if self._auth_code_queue is not None:
                        await self._auth_code_queue.put(text)
                        logger.info(
                            "Auth code received via worker-message (login flow) len=%d",
                            len(text),
                        )
                        await self._emit("Auth code received and forwarded to login flow")
                    else:
                        active = next((s for s in self.agents if s.current_claude), None)
                        if active:
                            delivered = await active.current_claude.send_message(text)
                            if delivered:
                                await self._emit(f"Injected: {text[:80]}")
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
                    self._auth_code_queue is not None,
                )
                if msg_worker_id != self.cfg.worker_id:
                    logger.warning("worker-auth-response workerId mismatch — ignoring")
                    continue
                code = msg.get("code", "")
                if not code:
                    logger.warning("worker-auth-response has empty code — ignoring")
                    continue
                if self._auth_code_queue is not None:
                    await self._auth_code_queue.put(code)
                    logger.info("Auth code queued (len=%d)", len(code))
                else:
                    logger.warning(
                        "worker-auth-response received but no auth in progress (queue is None)"
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
                # Drop the cached known-id so the puller doesn't reject the
                # re-queued task as a duplicate after a worker restart.
                self._known_task_ids.add(task_id)
                await self.task_queue.put(
                    {
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
                        "repos": msg.get("repos") or [],
                        "followup_instructions": instructions,
                        "followup_branch": msg.get("branch", ""),
                    }
                )

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
                                "repos": msg.get("repos") or [],
                                "followup_instructions": instructions,
                                "followup_branch": msg.get("branch", ""),
                            }
                        )

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
            logger.warning("GitHub repo refresh failed: %s", exc)
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
            await self._send(
                {
                    "type": "worker-register",
                    "workerId": self.cfg.worker_id,
                    "repos": self._broadcast_repos,
                    "tools": self._available_tools,
                    **({"user": self.cfg.user} if self.cfg.user else {}),
                }
            )

    def _known_repos(self) -> list[str]:
        """All repos this worker may have cloned: static list + org repos already on disk."""
        repos = list(self.cfg.repos)
        if self.cfg.org:
            org_dir = os.path.join(self.cfg.repos_dir, self.cfg.org)
            if os.path.isdir(org_dir):
                for entry in os.listdir(org_dir):
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
        base = os.path.join(self.cfg.work_dir, self.cfg.guild_id, self.cfg.worker_id)
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
            except TimeoutError:
                pass
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
        # Sync immediately at startup so the most recent session state is captured.
        await s3_uploader.sync_paths(
            bucket=self.cfg.s3_bucket,
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
            except TimeoutError:
                pass
            await s3_uploader.sync_paths(
                bucket=self.cfg.s3_bucket,
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
            now = asyncio.get_event_loop().time()
            if (
                self.cfg.github_token
                and now - self._last_repo_refresh > REPO_REFRESH_INTERVAL_SECONDS
            ):
                await self._refresh_github_repos()
            all_idle = all(s.current_claude is None for s in self.agents)
            if self.task_queue.empty() and all_idle:
                known = self._known_repos()
                if known:
                    await git_ops.pull_repos(
                        self.cfg.repos_dir,
                        known,
                        self.cfg.github_token,
                        self._emit,
                    )

    # ------------------------------------------------------------------ Agent loop
    async def _agent_loop(self, slot: Agent) -> None:
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
        token = self.cfg.github_token
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
        followup_branch = task.get("followup_branch") or ""
        is_followup = bool(followup_instructions)

        logger.info(
            "Executing task %s (agent=%s, followup=%s): %s",
            task_id,
            agent.agent_id,
            is_followup,
            desc[:120],
        )
        await self._set_state("working", agent)
        await self._task_update(task_id, agent=agent, state="working")

        name = task.get("name") or desc
        # On follow-ups we must continue on the existing branch — the original
        # worker pushed it and the foreman wants the same PR updated.
        branch = followup_branch or f"claude/{_slug(name)}-{task_id[:6]}"
        work_dir = os.path.join(self.cfg.work_dir, self.cfg.guild_id, self.cfg.worker_id, task_id)
        logger.info(
            "Task %s branch=%s work_dir=%s followup=%s", task_id, branch, work_dir, is_followup
        )
        os.makedirs(work_dir, exist_ok=True)

        emit = self._task_emit(task_id, agent)
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
                # Worktree from a prior run on the same task — reuse it. Pull
                # the latest origin state for the branch so a different worker
                # that pushed changes is reflected here.
                logger.info("Task %s: reusing worktree at %s", task_id, wt_path)
                await emit(f"Reusing worktree {repo_name}", level=LEVEL_WORKER)
                if is_followup:
                    await git_ops.run_git(["fetch", "origin", branch], cwd=wt_path)
                worktree_entries.append((repo_full, repo_path, wt_path))
                if primary_wt is None:
                    primary_wt = wt_path
                continue
            if is_followup:
                logger.info("Task %s: attaching worktree %s to branch %s", task_id, wt_path, branch)
                ok = await git_ops.attach_worktree(repo_path, wt_path, branch)
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
                    ok = await git_ops.create_worktree(repo_path, wt_path, branch)
            else:
                logger.info("Task %s: creating worktree %s on branch %s", task_id, wt_path, branch)
                ok = await git_ops.create_worktree(repo_path, wt_path, branch)
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
            return

        await self._task_update(task_id, agent=agent, branch=branch, worktreePath=primary_wt)
        # Register worktrees for the deferred sweeper — touched again below in
        # finally so the timestamp reflects the most recent activity.
        self._register_worktrees(task_id, worktree_entries)

        tool = (task.get("tool") or "claude").lower()

        # Tracks the last claude session ID so redirects can --resume with full context
        resume_session_id: str | None = None

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

        def _on_proc(proc: claude_runner.ClaudeProcess) -> None:
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
                current_desc = (
                    f"You are continuing existing work on branch `{branch}` for task {task_id}.\n\n"
                    f"Original task:\n{desc}\n\n"
                    f"Follow-up instructions:\n{followup_instructions}\n\n"
                    "Make the requested changes, then commit and push:\n"
                    f'  git add -A && git commit -m "<concise commit message>"\n'
                    f"  git push origin {branch}\n"
                    "If a PR already exists for this branch, no new PR is needed."
                )
            else:
                current_desc = desc
                if tool == "claude":
                    pr_title = (task.get("name") or desc)[:72].replace('"', "'")
                    closes = f" Closes #{task['issue_number']}" if task.get("issue_number") else ""
                    _phase = (task.get("phase") or "execute").lower()
                    if _phase == "review":
                        # Review-phase tasks must post findings as GitHub PR review
                        # comments — NEVER by committing files and opening a new PR.
                        _pr_repo = task.get("issue_repo") or ""
                        _pr_num = task.get("issue_number")
                        if _pr_repo and _pr_num:
                            _pr_ref = f"https://github.com/{_pr_repo}/pull/{_pr_num}"
                            _api_ref = f"repos/{_pr_repo}/pulls/{_pr_num}"
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
                    elif _phase in _PR_PHASES:
                        current_desc = (
                            f"{desc}\n\n"
                            f"After completing your changes, commit, push, and open a PR:\n"
                            f'  git add -A && git commit -m "<concise commit message>"\n'
                            f"  git push origin {branch}\n"
                            f'  gh pr create --title "{pr_title}" --body "<summary of changes>{closes}"\n'
                        )
                    # plan / ephemeral / automation phases: leave current_desc = desc unchanged
            success = False
            stop_reason = "no_events"
            last_msg = ""

            while True:
                if tool == "codex":
                    logger.info("Task %s: launching codex in %s", task_id, primary_wt)
                    success, stop_reason, last_msg = await codex_runner.run_codex_auto(
                        current_desc,
                        primary_wt,
                        emit=emit,
                        codex_path=self.cfg.codex_path,
                        codex_args=self.cfg.codex_args,
                        openai_api_key=self.cfg.openai_api_key,
                    )
                elif tool == "pi":
                    _pi_model = task.get("model") or self.cfg.pi_model
                    _pi_provider = task.get("provider") or self.cfg.pi_provider
                    logger.info(
                        "Task %s: launching pi in %s (model=%s provider=%s)",
                        task_id,
                        primary_wt,
                        _pi_model,
                        _pi_provider,
                    )
                    success, stop_reason, last_msg, _pi_session_id = await pi_runner.run_pi_auto(
                        current_desc,
                        primary_wt,
                        emit=emit,
                        pi_path=self.cfg.pi_path,
                        model=_pi_model,
                        provider=_pi_provider,
                        on_usage=_collect_usage,
                    )
                    resume_session_id = _pi_session_id
                else:
                    logger.info(
                        "Task %s: launching claude in %s (max_turns=%d, resume=%s)",
                        task_id,
                        primary_wt,
                        self.cfg.claude_max_turns,
                        resume_session_id,
                    )
                    (
                        success,
                        stop_reason,
                        last_msg,
                        resume_session_id,
                    ) = await claude_runner.run_claude_auto(
                        current_desc,
                        primary_wt,
                        max_turns=self.cfg.claude_max_turns,
                        emit=emit,
                        on_proc=_on_proc,
                        on_usage=_collect_usage,
                        claude_path=self.cfg.claude_path,
                        resume_session_id=resume_session_id,
                    )

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

            # Report captured per-API-call usage (claude tasks only). Best-effort.
            if usage_records:
                await self._report_usage(
                    task_id=task_id,
                    session_id=resume_session_id,
                    repo=repos[0] if repos else None,
                    records=usage_records,
                )

            # Push the branch regardless of outcome so partial work is visible
            # and a follow-up run can build on it.
            push_ok = await github_pr.push_branch(
                branch=branch,
                worktree_path=primary_wt,
                emit=emit,
            )
            if push_ok:
                await emit(f"Branch pushed: {branch}", level=LEVEL_WORKER)

            pr_url = await github_pr.find_existing_pr(
                branch=branch,
                worktree_path=primary_wt,
                token=token,
            )
            if pr_url:
                await emit(f"✓ Claude-authored PR: {pr_url}", level=LEVEL_WORKER)
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
                await self._task_update(
                    task_id,
                    success=True,
                    type="task-followup-done" if is_followup else "task-complete",
                    agent=agent,
                    state="awaiting-review",
                    **msg,
                )

            else:
                logger.warning("Task %s failed: %s", task_id, stop_reason)
                await self._task_update(
                    task_id,
                    agent=agent,
                    type="needs-input" if stop_reason == "needs_input" else "error",
                    state="error",
                    **msg,
                )
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
