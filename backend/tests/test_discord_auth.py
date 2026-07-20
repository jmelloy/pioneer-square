"""Unit tests for the foreman-role mention helpers in discord/auth.py."""

from __future__ import annotations

import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from discord import auth  # noqa: E402


def test_mentions_foreman_role_via_mention_roles_array():
    """Guild messages carry a structured mention_roles array — the primary signal."""
    message = {"content": "hey <@&1522965022836396178> got a sec?", "mention_roles": ["1522965022836396178"]}
    assert auth.mentions_foreman_role(message) is True


def test_mentions_foreman_role_via_raw_content_fallback():
    """DMs carry no mention_roles (no guild to resolve a role against) — fall back to content."""
    message = {"content": "<@&1522965022836396178> ping", "mention_roles": []}
    assert auth.mentions_foreman_role(message) is True


def test_mentions_foreman_role_false_when_absent():
    message = {"content": "just chatting", "mention_roles": []}
    assert auth.mentions_foreman_role(message) is False


def test_mentions_foreman_role_ignores_unrelated_role_mentions():
    message = {"content": "<@&999999999999999999> different role", "mention_roles": ["999999999999999999"]}
    assert auth.mentions_foreman_role(message) is False


def test_strip_foreman_role_mention_removes_token():
    content = auth.strip_foreman_role_mention("<@&1522965022836396178> what's the status?")
    assert content == "what's the status?"
    assert "1522965022836396178" not in content


def test_foreman_role_id_overridable_via_env(monkeypatch):
    monkeypatch.setenv("DISCORD_FOREMAN_ROLE_ID", "42")
    reloaded = importlib.reload(auth)
    try:
        assert reloaded.mentions_foreman_role({"content": "<@&42> hi", "mention_roles": []}) is True
        assert reloaded.mentions_foreman_role(
            {"content": "<@&1522965022836396178> hi", "mention_roles": []}
        ) is False
    finally:
        monkeypatch.delenv("DISCORD_FOREMAN_ROLE_ID", raising=False)
        importlib.reload(auth)
