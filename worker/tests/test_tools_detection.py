"""Tests for --tools CLI flag and _detect_available_tools() path validation."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pioneer_worker import cli
from pioneer_worker.config import Config
from pioneer_worker.worker import Worker


def _make_cfg(**kwargs) -> Config:
    defaults = dict(
        backend_url="ws://localhost:8000",
        guild_id="g12345",
        repos=["owner/repo"],
    )
    defaults.update(kwargs)
    return Config(**defaults)


class TestDetectAvailableTools:
    """Unit tests for Worker._detect_available_tools."""

    def test_tool_on_path_is_included(self):
        """A tool whose binary is found on PATH must appear in _available_tools."""
        cfg = _make_cfg(tools=["claude"], claude_path="claude")
        worker = Worker(cfg)
        with patch("shutil.which", side_effect=lambda x: x if x == "claude" else None):
            worker._detect_available_tools()
        assert "claude" in worker._available_tools

    def test_tool_not_on_path_is_excluded(self):
        """A tool whose binary is absent from PATH must be excluded, not raise."""
        cfg = _make_cfg(tools=["codex"], codex_path="codex")
        worker = Worker(cfg)
        with patch("shutil.which", return_value=None):
            worker._detect_available_tools()
        assert "codex" not in worker._available_tools

    def test_tool_not_on_path_logs_warning(self, caplog):
        """Absence from PATH must produce a warning log."""
        import logging

        cfg = _make_cfg(tools=["pi"], pi_path="pi")
        worker = Worker(cfg)
        with caplog.at_level(logging.WARNING, logger="pioneer_worker.worker"):
            with patch("shutil.which", return_value=None):
                worker._detect_available_tools()
        assert any("pi" in r.message and "not found" in r.message for r in caplog.records)

    def test_only_specified_tools_are_checked(self):
        """When cfg.tools is set, unlisted tools are not probed even if on PATH."""
        cfg = _make_cfg(tools=["claude"], claude_path="claude", codex_path="codex")
        worker = Worker(cfg)
        checked: list[str] = []

        def _fake_which(path: str):
            checked.append(path)
            return path  # pretend everything is present

        with patch("shutil.which", side_effect=_fake_which):
            worker._detect_available_tools()

        assert "claude" in worker._available_tools
        assert "codex" not in worker._available_tools
        assert "codex" not in checked

    def test_auto_detect_checks_all_known_tools(self):
        """When cfg.tools is None all known tool binaries must be probed."""
        cfg = _make_cfg(tools=None, claude_path="claude", codex_path="codex", pi_path="pi")
        worker = Worker(cfg)
        checked: list[str] = []

        def _fake_which(path: str):
            checked.append(path)
            return path

        with patch("shutil.which", side_effect=_fake_which):
            worker._detect_available_tools()

        assert "claude" in checked
        assert "codex" in checked
        assert "pi" in checked
        assert worker._available_tools == ["claude", "codex", "pi"]

    def test_unknown_tool_name_logs_warning_and_is_skipped(self, caplog):
        """An unrecognised tool name in cfg.tools must be skipped with a warning."""
        import logging

        cfg = _make_cfg(tools=["magic_tool"])
        worker = Worker(cfg)
        with caplog.at_level(logging.WARNING, logger="pioneer_worker.worker"):
            with patch("shutil.which", return_value="/usr/bin/magic_tool"):
                worker._detect_available_tools()

        assert "magic_tool" not in worker._available_tools
        assert any("magic_tool" in r.message for r in caplog.records)

    def test_mixed_present_and_absent_tools(self):
        """Only tools actually present on PATH are included; absent ones are silently dropped."""
        cfg = _make_cfg(
            tools=["claude", "codex", "pi"],
            claude_path="claude",
            codex_path="codex",
            pi_path="pi",
        )
        worker = Worker(cfg)

        def _fake_which(path: str):
            return path if path in ("claude", "pi") else None

        with patch("shutil.which", side_effect=_fake_which):
            worker._detect_available_tools()

        assert worker._available_tools == ["claude", "pi"]
        assert "codex" not in worker._available_tools

    def test_absolute_path_detected_via_file_check(self, tmp_path):
        """An absolute path to an executable must be detected even when shutil.which returns None."""
        exe = tmp_path / "my-claude"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)

        cfg = _make_cfg(tools=["claude"], claude_path=str(exe))
        worker = Worker(cfg)
        with patch("shutil.which", return_value=None):
            worker._detect_available_tools()

        assert "claude" in worker._available_tools

    def test_absolute_path_nonexistent_excluded(self, tmp_path):
        """An absolute path that doesn't exist must be excluded."""
        cfg = _make_cfg(tools=["claude"], claude_path=str(tmp_path / "no-such-binary"))
        worker = Worker(cfg)
        with patch("shutil.which", return_value=None):
            worker._detect_available_tools()

        assert "claude" not in worker._available_tools

    def test_absolute_path_non_executable_excluded(self, tmp_path):
        """An absolute path to a non-executable file must be excluded."""
        non_exe = tmp_path / "claude-data"
        non_exe.write_text("not a script")
        non_exe.chmod(0o644)

        cfg = _make_cfg(tools=["claude"], claude_path=str(non_exe))
        worker = Worker(cfg)
        with patch("shutil.which", return_value=None):
            worker._detect_available_tools()

        assert "claude" not in worker._available_tools


class TestToolsCLIFlag:
    """Tests for the --tools CLI flag wiring through cli.main()."""

    def _write_toml(self, tmp_path, body: str):
        p = tmp_path / "pioneer-worker.toml"
        p.write_text(body)
        return p

    def test_tools_flag_passed_to_config(self, tmp_path, monkeypatch):
        """--tools claude,codex must arrive in cfg.tools as a list."""
        toml_path = self._write_toml(
            tmp_path,
            'backend_url = "ws://x:1"\nguild_id = "g"\n[github]\nrepos = ["owner/repo"]\n',
        )
        captured = {}

        class _FakeWorker:
            def __init__(self, cfg):
                captured["tools"] = cfg.tools

            async def run(self):
                return None

        monkeypatch.setattr(cli, "Worker", _FakeWorker)
        rc = cli.main(["--config", str(toml_path), "--tools", "claude,codex"])
        assert rc == 0
        assert captured["tools"] == ["claude", "codex"]

    def test_no_tools_flag_gives_none(self, tmp_path, monkeypatch):
        """Omitting --tools must leave cfg.tools as None (auto-detect mode)."""
        toml_path = self._write_toml(
            tmp_path,
            'backend_url = "ws://x:1"\nguild_id = "g"\n[github]\nrepos = ["owner/repo"]\n',
        )
        captured = {}
        monkeypatch.delenv("PIONEER_TOOLS", raising=False)

        class _FakeWorker:
            def __init__(self, cfg):
                captured["tools"] = cfg.tools

            async def run(self):
                return None

        monkeypatch.setattr(cli, "Worker", _FakeWorker)
        rc = cli.main(["--config", str(toml_path)])
        assert rc == 0
        assert captured["tools"] is None

    def test_tools_flag_strips_whitespace(self, tmp_path, monkeypatch):
        """Spaces around commas in --tools must be stripped."""
        toml_path = self._write_toml(
            tmp_path,
            'backend_url = "ws://x:1"\nguild_id = "g"\n[github]\nrepos = ["owner/repo"]\n',
        )
        captured = {}

        class _FakeWorker:
            def __init__(self, cfg):
                captured["tools"] = cfg.tools

            async def run(self):
                return None

        monkeypatch.setattr(cli, "Worker", _FakeWorker)
        rc = cli.main(["--config", str(toml_path), "--tools", " claude , pi "])
        assert rc == 0
        assert captured["tools"] == ["claude", "pi"]
