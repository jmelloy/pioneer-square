"""run_git/run_gh must not hang forever on a stuck subprocess.

A stalled network fetch/push, or a wait on a lock file left by a crashed
process, previously blocked the caller indefinitely — starving an agent slot
(or the idle puller / startup clone) with no way to recover short of killing
the whole worker process.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from pioneer_worker import git_ops


class _HangingProcess:
    """Fake asyncio subprocess whose communicate() never resolves."""

    def __init__(self) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.killed = False
        self.waited = False

    async def communicate(self):
        await asyncio.Event().wait()  # never set — simulates a stuck process

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        return self.returncode or -9


@pytest.mark.asyncio
async def test_run_git_kills_a_hung_process_after_timeout():
    proc = _HangingProcess()

    async def _fake_exec(*_args, **_kwargs):
        return proc

    with patch("asyncio.create_subprocess_exec", new=_fake_exec):
        rc, out, err = await git_ops.run_git(["fetch", "origin"], cwd="/tmp", timeout=0.05)

    assert rc == -1
    assert "timed out" in err
    assert proc.killed
    assert proc.waited


@pytest.mark.asyncio
async def test_run_gh_kills_a_hung_process_after_timeout():
    proc = _HangingProcess()

    async def _fake_exec(*_args, **_kwargs):
        return proc

    with patch("asyncio.create_subprocess_exec", new=_fake_exec):
        rc, out, err = await git_ops.run_gh(["pr", "view", "1"], timeout=0.05)

    assert rc == -1
    assert "timed out" in err
    assert proc.killed
    assert proc.waited
