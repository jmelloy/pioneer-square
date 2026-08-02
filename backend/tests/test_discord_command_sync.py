"""Unit tests for discord.command_sync.

No real Discord credentials or network access required — all HTTP calls are
mocked at the urllib layer.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from discord.command_sync import COMMANDS, _do_sync, sync_slash_commands_on_startup  # noqa: E402

# ---------------------------------------------------------------------------
# _do_sync (synchronous helper)
# ---------------------------------------------------------------------------


def _fake_response(body: list) -> MagicMock:
    """Return a mock that behaves like the context-manager from urlopen."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_do_sync_global(monkeypatch):
    """PUT is sent to the global endpoint when no guild_id is given."""
    captured = {}

    def fake_urlopen(req):
        captured["url"] = req.full_url
        captured["method"] = req.method
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.data)
        return _fake_response(COMMANDS)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = _do_sync("APP123", "BOT_TOKEN")

    assert captured["method"] == "PUT"
    assert captured["url"].endswith("/applications/APP123/commands")
    assert captured["headers"]["Authorization"] == "Bot BOT_TOKEN"
    assert captured["body"] == COMMANDS
    assert result == COMMANDS


def test_do_sync_guild_scoped(monkeypatch):
    """PUT is sent to the guild-scoped endpoint when guild_id is provided."""
    captured = {}

    def fake_urlopen(req):
        captured["url"] = req.full_url
        return _fake_response(COMMANDS)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    _do_sync("APP123", "BOT_TOKEN", guild_id="GUILD999")

    assert "/guilds/GUILD999/commands" in captured["url"]


def test_do_sync_raises_on_http_error(monkeypatch):
    """HTTPError propagates to the caller."""

    def fake_urlopen(req):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, BytesIO(b"bad token"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        _do_sync("APP123", "BAD_TOKEN")


# ---------------------------------------------------------------------------
# sync_slash_commands_on_startup (async wrapper)
# ---------------------------------------------------------------------------


async def test_startup_sync_skipped_when_no_env(monkeypatch):
    """No HTTP call is made when DISCORD_APPLICATION_ID / BOT_TOKEN are absent."""
    monkeypatch.delenv("DISCORD_APPLICATION_ID", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    with patch("discord.command_sync._do_sync") as mock_sync:
        await sync_slash_commands_on_startup()
        mock_sync.assert_not_called()


async def test_startup_sync_skipped_when_only_app_id(monkeypatch):
    """No HTTP call when BOT_TOKEN is missing even if APPLICATION_ID is set."""
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "APP123")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    with patch("discord.command_sync._do_sync") as mock_sync:
        await sync_slash_commands_on_startup()
        mock_sync.assert_not_called()


async def test_startup_sync_calls_do_sync(monkeypatch):
    """When both env vars are set, _do_sync is called with the correct args."""
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "APP123")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "TOKEN456")
    monkeypatch.delenv("DISCORD_DEV_GUILD_ID", raising=False)

    with patch("discord.command_sync._do_sync", return_value=COMMANDS) as mock_sync:
        await sync_slash_commands_on_startup()
        mock_sync.assert_called_once_with("APP123", "TOKEN456", "")


async def test_startup_sync_uses_guild_id(monkeypatch):
    """DISCORD_DEV_GUILD_ID is forwarded to _do_sync."""
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "APP123")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "TOKEN456")
    monkeypatch.setenv("DISCORD_DEV_GUILD_ID", "GUILD999")

    with patch("discord.command_sync._do_sync", return_value=COMMANDS) as mock_sync:
        await sync_slash_commands_on_startup()
        mock_sync.assert_called_once_with("APP123", "TOKEN456", "GUILD999")


async def test_startup_sync_logs_warning_on_http_error(monkeypatch, caplog):
    """HTTPError is caught and logged as a warning; no exception propagates."""
    import logging

    monkeypatch.setenv("DISCORD_APPLICATION_ID", "APP123")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "TOKEN456")
    monkeypatch.delenv("DISCORD_DEV_GUILD_ID", raising=False)

    err = urllib.error.HTTPError(
        "https://discord.com/...", 401, "Unauthorized", {}, BytesIO(b"bad token")
    )
    with patch("discord.command_sync._do_sync", side_effect=err):
        with caplog.at_level(logging.WARNING, logger="discord.command_sync"):
            await sync_slash_commands_on_startup()  # must not raise

    assert any("401" in r.message for r in caplog.records)


async def test_startup_sync_logs_warning_on_unexpected_error(monkeypatch, caplog):
    """Generic exceptions are caught and logged; no exception propagates."""
    import logging

    monkeypatch.setenv("DISCORD_APPLICATION_ID", "APP123")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "TOKEN456")
    monkeypatch.delenv("DISCORD_DEV_GUILD_ID", raising=False)

    with patch("discord.command_sync._do_sync", side_effect=RuntimeError("network blip")):
        with caplog.at_level(logging.WARNING, logger="discord.command_sync"):
            await sync_slash_commands_on_startup()  # must not raise

    assert any("sync failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# COMMANDS schema sanity-checks
# ---------------------------------------------------------------------------


def test_commands_list_is_non_empty():
    assert len(COMMANDS) > 0


def test_commands_have_required_fields():
    for cmd in COMMANDS:
        assert "name" in cmd, f"missing 'name' in {cmd}"
        assert "description" in cmd, f"missing 'description' in {cmd}"


def test_commands_match_script():
    """COMMANDS defined here must match the standalone registration script."""
    import importlib.util
    import pathlib

    script_path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "scripts"
        / "register_discord_commands.py"
    )
    if not script_path.exists():
        pytest.skip("scripts/register_discord_commands.py not found")

    spec = importlib.util.spec_from_file_location("_reg_script", script_path)
    # The script calls sys.exit on error; patch that away and also avoid the
    # urlopen call that happens at module level.
    with (
        patch("sys.exit"),
        patch("urllib.request.urlopen", side_effect=RuntimeError("skip")),
    ):
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
        except Exception:
            pass  # module-level urlopen raises; COMMANDS is defined before that call

    if not hasattr(mod, "COMMANDS"):
        pytest.skip("COMMANDS not defined in script before exec failed")
    script_commands = mod.COMMANDS

    assert COMMANDS == script_commands, (
        "COMMANDS in backend/discord/command_sync.py and "
        "scripts/register_discord_commands.py are out of sync"
    )
