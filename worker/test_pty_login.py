#!/usr/bin/env python3
"""Standalone test for the claude auth PTY flow.

Runs `claude setup-token` (or whatever command is given) under the same
PTY-with-controlling-tty setup the worker uses, then waits for the OAuth URL,
asks the human operator to paste the code#state value, and writes it to the
PTY. Logs every step so we can see whether claude actually consumes input.

Usage (inside the worker container, where `claude` is on PATH):
    docker exec -it pioneer-worker-nyoyny-debug python3 /work/test_pty_login.py
or copy the file in:
    docker cp test_pty_login.py pioneer-worker-nyoyny-debug:/tmp/
    docker exec -it pioneer-worker-nyoyny-debug python3 /tmp/test_pty_login.py
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import re
import sys
import termios

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pty-test")

ANSI_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]")


def clean(b: bytes) -> str:
    return ANSI_RE.sub(b"", b).decode(errors="replace")


async def run(cmd: list[str], code_input: str | None = None) -> int:
    master_fd, slave_fd = pty.openpty()
    try:
        attrs = termios.tcgetattr(slave_fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
    except termios.error as exc:
        log.warning("could not turn off echo: %s", exc)

    def attach_ctty() -> None:
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=attach_ctty,
    )
    os.close(slave_fd)
    log.info("spawned pid=%s cmd=%s (full-pty, ctty=slave)", proc.pid, " ".join(cmd))

    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    proto = asyncio.StreamReaderProtocol(reader)
    pipe = os.fdopen(master_fd, "rb", buffering=0)
    await loop.connect_read_pipe(lambda: proto, pipe)

    code_sent = False
    saw_url = False
    raw_buf = bytearray()

    async def watchdog() -> None:
        ticks = 0
        while True:
            await asyncio.sleep(5)
            ticks += 1
            if proc.returncode is not None:
                return
            log.info("watchdog: claude still alive %ds (rc=%s)", ticks * 5, proc.returncode)

    wd_task = asyncio.create_task(watchdog())

    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                log.info("EOF on PTY")
                break
            raw_buf.extend(chunk)
            text = clean(chunk)
            sys.stdout.write(text)
            sys.stdout.flush()

            cleaned_all = clean(bytes(raw_buf))
            if not saw_url and "https://" in cleaned_all:
                saw_url = True
                log.info("URL detected in output")

            if not code_sent and saw_url and code_input:
                # Wait briefly for the React UI to mount, then paste
                await asyncio.sleep(2.0)
                payload = code_input.strip() + "\r"
                n = os.write(master_fd, payload.encode())
                log.info("wrote %d bytes (code+CR) to PTY master", n)
                code_sent = True
    finally:
        wd_task.cancel()
        try:
            pipe.close()
        except OSError:
            pass

    rc = await proc.wait()
    log.info("exit rc=%s", rc)
    return rc


async def main() -> None:
    cmd = sys.argv[1:] or ["claude", "setup-token"]
    code = os.environ.get("CLAUDE_CODE_PASTE")
    if code:
        log.info("will auto-paste code from $CLAUDE_CODE_PASTE (len=%d)", len(code))
    else:
        log.info(
            "no $CLAUDE_CODE_PASTE — will not auto-paste; you can also do it "
            "manually by typing into this terminal."
        )
    rc = await run(cmd, code)
    sys.exit(rc)


if __name__ == "__main__":
    asyncio.run(main())
