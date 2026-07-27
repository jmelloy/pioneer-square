"""Tests for pioneer_worker.tool_installer.ensure_tools_installed."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from pioneer_worker import tool_installer


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.killed = False

    async def communicate(self):
        return (b"", b"")

    def kill(self) -> None:
        self.killed = True


def _tool_paths(**overrides) -> dict[str, str]:
    paths = {"claude": "claude", "codex": "codex", "pi": "pi"}
    paths.update(overrides)
    return paths


async def test_already_present_tool_is_not_installed():
    with (
        patch("shutil.which", return_value="/usr/bin/claude"),
        patch("asyncio.create_subprocess_exec", AsyncMock()) as create,
    ):
        await tool_installer.ensure_tools_installed(["claude"], tool_paths=_tool_paths())
    create.assert_not_called()


async def test_missing_tool_is_installed_via_npm():
    with (
        patch("shutil.which", return_value=None),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_FakeProc())) as create,
    ):
        await tool_installer.ensure_tools_installed(["codex"], tool_paths=_tool_paths())
    create.assert_awaited_once_with(
        "npm",
        "install",
        "-g",
        "@openai/codex",
        stdout=-1,
        stderr=-2,
    )


async def test_successful_pi_install_also_installs_subagents():
    with (
        patch("shutil.which", return_value=None),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_FakeProc())) as create,
    ):
        await tool_installer.ensure_tools_installed(["pi"], tool_paths=_tool_paths())
    assert create.await_count == 2
    first_call, second_call = create.await_args_list
    assert first_call.args == ("npm", "install", "-g", "@earendil-works/pi-coding-agent")
    assert second_call.args == ("pi", "install", "npm:pi-subagents")


async def test_failed_pi_install_skips_subagents():
    with (
        patch("shutil.which", return_value=None),
        patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(return_value=_FakeProc(returncode=1)),
        ) as create,
    ):
        await tool_installer.ensure_tools_installed(["pi"], tool_paths=_tool_paths())
    create.assert_awaited_once()


async def test_unknown_tool_name_is_skipped(caplog):
    import logging

    with (
        caplog.at_level(logging.WARNING, logger="pioneer_worker.tool_installer"),
        patch("shutil.which", return_value=None),
        patch("asyncio.create_subprocess_exec", AsyncMock()) as create,
    ):
        await tool_installer.ensure_tools_installed(["magic_tool"], tool_paths=_tool_paths())
    create.assert_not_called()
    assert any("magic_tool" in r.message for r in caplog.records)


async def test_configured_absolute_path_is_respected(tmp_path):
    exe = tmp_path / "my-claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)

    with (
        patch("shutil.which", return_value=None),
        patch("asyncio.create_subprocess_exec", AsyncMock()) as create,
    ):
        await tool_installer.ensure_tools_installed(
            ["claude"], tool_paths=_tool_paths(claude=str(exe))
        )
    create.assert_not_called()


async def test_npm_not_found_returns_gracefully():
    with (
        patch("shutil.which", return_value=None),
        patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError())),
    ):
        # Should not raise even when npm itself is missing.
        await tool_installer.ensure_tools_installed(["claude"], tool_paths=_tool_paths())


async def test_install_timeout_is_handled(monkeypatch):
    import asyncio

    proc = _FakeProc()

    async def _hang(*args, **kwargs):
        await asyncio.sleep(3600)

    proc.communicate = _hang

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise TimeoutError()

    with (
        patch("shutil.which", return_value=None),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        patch("asyncio.wait_for", fake_wait_for),
    ):
        await tool_installer.ensure_tools_installed(["claude"], tool_paths=_tool_paths())
    assert proc.killed is True
