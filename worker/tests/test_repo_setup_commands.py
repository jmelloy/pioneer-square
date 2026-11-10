"""Tests for UI-managed setup command execution (repo_setup.py)."""

from __future__ import annotations

import asyncio
import os
import time

import httpx
from pioneer_worker import repo_setup
from pioneer_worker.repo_setup import SetupConfig, fetch_setup_config, run_setup_commands


class Collector:
    def __init__(self):
        self.lines: list[str] = []

    async def __call__(self, text, **kwargs):
        self.lines.append(text)


async def test_commands_run_in_cwd_and_output_captured(tmp_path):
    emit = Collector()
    res = await run_setup_commands(
        str(tmp_path), SetupConfig(["pwd", "echo hello", "echo err >&2"], 30), emit
    )
    assert res.ok and res.ran == 3
    assert os.path.realpath(str(tmp_path)) in [os.path.realpath(x) for x in emit.lines if "/" in x]
    assert "hello" in emit.lines and "err" in emit.lines
    assert any("setup complete" in line for line in emit.lines)


async def test_failure_stops_remaining_commands(tmp_path):
    emit = Collector()
    res = await run_setup_commands(
        str(tmp_path), SetupConfig(["echo one", "exit 3", "echo never"], 30), emit
    )
    assert not res.ok and res.ran == 1
    assert any("exit 3" in line for line in emit.lines)
    assert "never" not in emit.lines


async def test_timeout_kills_process_group(tmp_path):
    emit = Collector()
    marker = tmp_path / "survived"
    start = time.monotonic()
    res = await run_setup_commands(
        str(tmp_path), SetupConfig([f"(sleep 3; touch {marker}) & wait"], 1), emit
    )
    assert time.monotonic() - start < 3
    assert not res.ok and not res.cancelled
    assert any("timed out after 1s" in line for line in emit.lines)
    await asyncio.sleep(3.2)
    assert not marker.exists()


async def test_cancellation_kills_command(tmp_path):
    emit = Collector()
    flag = {"c": False}

    async def flip():
        await asyncio.sleep(0.5)
        flag["c"] = True

    t = asyncio.create_task(flip())
    start = time.monotonic()
    res = await run_setup_commands(
        str(tmp_path), SetupConfig(["sleep 30"], 60), emit, is_cancelled=lambda: flag["c"]
    )
    await t
    assert res.cancelled and time.monotonic() - start < 5
    assert any("cancelled" in line for line in emit.lines)


async def test_cancelled_before_start_runs_nothing(tmp_path):
    emit = Collector()
    res = await run_setup_commands(
        str(tmp_path), SetupConfig(["echo x"], 5), emit, is_cancelled=lambda: True
    )
    assert res.cancelled and res.ran == 0 and "x" not in emit.lines


async def test_env_propagates(tmp_path):
    emit = Collector()
    await run_setup_commands(
        str(tmp_path),
        SetupConfig(['echo "$TOOLCHAIN_VAR|$PATH"'], 30),
        emit,
        env={"TOOLCHAIN_VAR": "yes", "PATH": "/custom/bin:" + os.environ["PATH"]},
    )
    assert any(line.startswith("yes|/custom/bin:") for line in emit.lines)


async def test_toolchain_env_empty_without_mise_config(tmp_path):
    assert await repo_setup.toolchain_env(str(tmp_path)) == {}


async def test_toolchain_env_parses_mise_json(tmp_path, monkeypatch):
    (tmp_path / "mise.toml").write_text("")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "mise"
    fake.write_text('#!/bin/sh\necho \'{"PATH": "/x/bin", "FOO": "bar"}\'\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    assert await repo_setup.toolchain_env(str(tmp_path)) == {"PATH": "/x/bin", "FOO": "bar"}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_setup_config_ok():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"setupCommands": ["npm ci", " "], "timeoutSeconds": 90})

    cfg = await fetch_setup_config("http://b/", "g1", "tok", "acme/widget", _client(handler))
    assert cfg.commands == ["npm ci"] and cfg.timeout_seconds == 90
    assert seen["url"] == "http://b/guilds/g1/repos/acme/widget/setup"
    assert seen["auth"] == "Bearer tok"


async def test_fetch_setup_config_errors_yield_empty():
    cfg = await fetch_setup_config(
        "http://b", "g", "t", "a/b", _client(lambda r: httpx.Response(500))
    )
    assert cfg.commands == []

    def boom(request):
        raise httpx.ConnectError("down")

    cfg = await fetch_setup_config("http://b", "g", "t", "a/b", _client(boom))
    assert cfg.commands == []
