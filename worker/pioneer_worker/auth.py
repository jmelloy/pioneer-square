"""Claude Code authentication for the worker.

Detects existing credentials, restores stored credentials from the backend,
and (when neither is available) drives the ``claude setup-token`` TUI to
completion via PTY so a human pasting an auth code in the UI can authenticate
a headless worker.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import io
import json
import logging
import os
import pty
import re
import tarfile
import termios
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

EmitFn = Callable[[str], Awaitable[None]]
SendFn = Callable[[dict], Awaitable[None]]
HttpFactory = Callable[[], Awaitable[httpx.AsyncClient]]


async def is_authenticated(claude_path: str) -> bool:
    """Return True if ``claude auth status --json`` reports ``loggedIn: true``.

    Works on macOS keychain and Linux. The exit code alone is unreliable
    (the CLI returns 0 even when not logged in, just emits ``loggedIn:
    false``), so we parse the JSON. A timeout guards against macOS keychain
    access prompts that can hang the subprocess indefinitely when invoked
    without a controlling TTY.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_path,
            "auth",
            "status",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        logger.warning("claude binary not found at %r: %s", claude_path, exc)
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
        parsed = json.loads(raw)
    except json.JSONDecodeError:
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


def extract_oauth_token(cleaned_output: str) -> str | None:
    """Locate the OAuth token in Ink's success-screen output.

    The Ink TUI renders the token amid lots of surrounding text and ASCII
    art; after stripping ANSI escapes it shows up as a long alphanumeric
    run (40-100 chars from `[A-Za-z0-9_-]`) somewhere near the marker
    "Store this token securely" / "Use this token by setting".

    We anchor on those markers and pick the longest token-like run that
    appears near them.
    """
    # Filter out spaces/newlines that Ink injects between glyphs in its
    # box-layout, which would otherwise split the token character run.
    compact = re.sub(r"\s+", "", cleaned_output)
    marker_idx = compact.find("Storethistokensecurely")
    if marker_idx < 0:
        marker_idx = compact.find("UsethistokenbysettingexportCLAUDE_CODE_OAUTH_TOKEN")
    if marker_idx < 0:
        return None
    window = compact[max(0, marker_idx - 400) : marker_idx]
    candidates = re.findall(r"[A-Za-z0-9_\-]{40,200}", window)
    if not candidates:
        return None
    return candidates[-1]


class AuthFlow:
    """Owns the (possibly active) auth-code queue and runs the login dance.

    Worker holds one of these and routes ``worker-auth-response`` /
    ``worker-message`` payloads into ``deliver_code`` so a human pasting a
    code in the UI can complete the headless ``claude setup-token`` flow.
    """

    def __init__(
        self,
        *,
        claude_path: str,
        worker_id_provider: Callable[[], str | None],
        guild_id: str,
        http_factory: HttpFactory,
        send: SendFn,
        emit: EmitFn,
    ) -> None:
        self.claude_path = claude_path
        self._worker_id_provider = worker_id_provider
        self.guild_id = guild_id
        self.http_factory = http_factory
        self.send = send
        self.emit = emit
        self.auth_code_queue: asyncio.Queue[str] | None = None

    @property
    def worker_id(self) -> str | None:
        return self._worker_id_provider()

    async def deliver_code(self, code: str) -> bool:
        """Forward an auth code from the WS listener into the active login flow.

        Returns True if a login is in progress and the code was queued.
        """
        if self.auth_code_queue is None:
            return False
        await self.auth_code_queue.put(code)
        return True

    async def check(self) -> None:
        """Ensure Claude is authenticated. Restore stored creds or run login flow."""
        if os.environ.get("ANTHROPIC_API_KEY"):
            logger.info("ANTHROPIC_API_KEY set — skipping Claude login flow")
            return

        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            logger.info("CLAUDE_CODE_OAUTH_TOKEN already in env — skipping login")
            return

        if await is_authenticated(self.claude_path):
            logger.info("Claude credentials already present")
            return

        if await self._restore_from_backend():
            return

        await self.emit("[auth] No Claude credentials found — starting login...")
        await self._run_login()

    async def _restore_from_backend(self) -> bool:
        """Try to restore credentials stored in the backend.

        Two formats are supported: the new JSON ``{"oauth_token": "..."}`` blob
        produced by ``claude setup-token``, and the legacy base64(tar.gz of
        ~/.claude) blob produced by older versions running ``claude auth login``.

        Returns True if credentials were successfully restored.
        """
        try:
            async with await self.http_factory() as client:
                resp = await client.get(
                    "/auth/claude/credentials",
                    params={"guild_id": self.guild_id},
                )
        except Exception as exc:
            logger.warning("Could not fetch Claude credentials from backend: %s", exc)
            return False

        if resp.status_code != 200:
            return False
        blob = resp.json().get("credentials_blob", "")
        if not blob:
            return False

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
                return True
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass  # fall through to legacy tarball path

        claude_dir = Path.home() / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            tar.extractall(path=Path.home())
        logger.info("Restored Claude credentials tarball from backend (legacy)")
        if await is_authenticated(self.claude_path):
            return True
        logger.warning("Restored credentials blob but auth status still fails")
        return False

    async def _run_login(self) -> None:
        """Drive ``claude setup-token`` to completion and persist the OAuth token.

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
        self.auth_code_queue = asyncio.Queue()
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
            self.claude_path,
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

        # Strip ANSI/CSI escape sequences so we can grep Ink's output.
        ansi_re = re.compile(
            rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b\][^\x07\x1b]*[\x07\x1b]"
        )

        def _clean(b: bytes) -> str:
            return ansi_re.sub(b"", b).decode(errors="replace")

        captured = bytearray()
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
                        await self.emit(f"[auth] {line}")

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
                        await self.send(
                            {
                                "type": "claude-auth-required",
                                "workerId": self.worker_id,
                                "url": url,
                            }
                        )
                        await self.emit(
                            "[auth] Waiting for auth code — paste it into the auth panel in the UI..."
                        )
                        logger.info("Auth login: awaiting code from queue (timeout=300s)")
                        try:
                            code = await asyncio.wait_for(self.auth_code_queue.get(), timeout=300.0)
                        except TimeoutError:
                            logger.warning("Auth login: timed out waiting for code from queue")
                            await self.emit(
                                "[auth] Timed out waiting for auth code — restart the worker to retry"
                            )
                            proc.kill()
                            await proc.wait()
                            return
                        logger.info(
                            "Auth login: code dequeued (len=%d, pid_alive=%s)",
                            len(code),
                            proc.returncode is None,
                        )
                        await self.emit("[auth] Code received — submitting to Claude CLI...")
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
                            await self.emit(f"[auth] Failed to send code to Claude: {exc}")
                            proc.kill()
                            await proc.wait()
                            return
                        logger.info("Auth login: wrote code + CR (separate writes) to PTY")
                        code_sent = True
                        post_submit_watchdog = asyncio.create_task(_login_watchdog(proc))
        finally:
            self.auth_code_queue = None
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

        cleaned_full = _clean(bytes(captured))
        token = extract_oauth_token(cleaned_full)
        if not token:
            await self.emit(
                "[auth] Login finished but could not extract OAuth token from output — please retry"
            )
            logger.warning("Could not locate OAuth token in setup-token output")
            return

        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
        logger.info("Captured CLAUDE_CODE_OAUTH_TOKEN (len=%d) and set in env", len(token))
        await self.emit("[auth] Token captured — saving to backend so future workers can reuse it")

        try:
            blob = base64.b64encode(json.dumps({"oauth_token": token}).encode()).decode()
            async with await self.http_factory() as client:
                await client.post(
                    "/auth/claude/credentials",
                    json={"guild_id": self.guild_id, "credentials_blob": blob},
                )
            await self.emit("[auth] Credentials saved")
            logger.info("Posted OAuth token to backend credentials store")
        except Exception as exc:
            logger.warning("Could not store Claude credentials: %s", exc)
            await self.emit(f"[auth] Warning: could not store credentials: {exc}")


async def _login_watchdog(proc: asyncio.subprocess.Process) -> None:
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
