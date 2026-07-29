"""Tests for the unified spawn-settings resolver (backend/spawn_config.py)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from spawn_config import ResolvedSpawn, SpawnLayer, merge_layers  # noqa: E402


def test_empty_layers():
    r = merge_layers([None, None])
    assert r == ResolvedSpawn()


def test_guild_baseline_only():
    guild = SpawnLayer(repos=["a/b"], tools=["pi"], agent_count=2, provider="bedrock")
    r = merge_layers([guild, None, None])
    assert r.repos == ["a/b"]
    assert r.tools == ["pi"]
    assert r.agent_count == 2
    assert r.provider == "bedrock"


def test_user_overrides_guild_scalars_and_lists():
    guild = SpawnLayer(repos=["a/b"], tools=["pi"], agent_count=2, model="m1")
    user = SpawnLayer(repos=["c/d"], agent_count=4)  # tools/model unset -> inherit guild
    r = merge_layers([guild, user])
    assert r.repos == ["c/d"]  # user non-empty list wins
    assert r.tools == ["pi"]  # user left it unset -> guild survives
    assert r.agent_count == 4
    assert r.model == "m1"


def test_call_args_win_over_user_and_guild():
    guild = SpawnLayer(tools=["pi"], agent_count=1)
    user = SpawnLayer(tools=["claude"], agent_count=2)
    call = SpawnLayer(agent_count=8)
    r = merge_layers([guild, user, call])
    assert r.tools == ["claude"]  # highest non-empty (user); call left tools unset
    assert r.agent_count == 8  # call wins


def test_empty_list_does_not_clobber():
    guild = SpawnLayer(repos=["a/b"])
    user = SpawnLayer(repos=[])  # empty -> not a real override
    r = merge_layers([guild, user])
    assert r.repos == ["a/b"]


def test_env_vars_overlay_key_by_key():
    guild = SpawnLayer(env_vars={"AWS_REGION": "us-east-1", "TOKEN": "guild-tok"})
    user = SpawnLayer(env_vars={"TOKEN": "user-tok", "EXTRA": "x"})
    r = merge_layers([guild, user])
    assert r.env_vars == {"AWS_REGION": "us-east-1", "TOKEN": "user-tok", "EXTRA": "x"}


def test_empty_env_value_does_not_blank_real_one():
    # The one empty-value guard: a stale "" from a higher layer must not wipe a
    # real credential set by a lower layer.
    guild = SpawnLayer(env_vars={"AWS_BEARER_TOKEN_BEDROCK": "real-token"})
    user = SpawnLayer(env_vars={"AWS_BEARER_TOKEN_BEDROCK": ""})
    r = merge_layers([guild, user])
    assert r.env_vars["AWS_BEARER_TOKEN_BEDROCK"] == "real-token"


def test_empty_env_value_can_set_when_no_lower_value():
    user = SpawnLayer(env_vars={"NEWKEY": ""})
    r = merge_layers([None, user])
    assert r.env_vars["NEWKEY"] == ""


def test_tool_env_vars_merge_per_tool():
    guild = SpawnLayer(tool_env_vars={"pi": {"A": "1"}, "claude": {"C": "3"}})
    user = SpawnLayer(tool_env_vars={"pi": {"B": "2"}})
    r = merge_layers([guild, user])
    assert r.tool_env_vars == {"pi": {"A": "1", "B": "2"}, "claude": {"C": "3"}}
