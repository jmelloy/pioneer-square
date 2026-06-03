"""Unit tests for pioneer_worker.config."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pioneer_worker.config import Config, load

# ---------------------------------------------------------------------------
# Config.http_url property
# ---------------------------------------------------------------------------


def test_http_url_from_ws():
    cfg = Config(backend_url="ws://localhost:8000", guild_id="abc")
    assert cfg.http_url == "http://localhost:8000"


def test_http_url_from_wss():
    cfg = Config(backend_url="wss://example.com", guild_id="abc")
    assert cfg.http_url == "https://example.com"


def test_http_url_already_http():
    cfg = Config(backend_url="http://localhost:8000", guild_id="abc")
    assert cfg.http_url == "http://localhost:8000"


def test_http_url_strips_trailing_slash():
    cfg = Config(backend_url="ws://localhost:8000/", guild_id="abc")
    assert not cfg.http_url.endswith("/")


# ---------------------------------------------------------------------------
# Config.ws_url property
# ---------------------------------------------------------------------------


def test_ws_url_appends_guild():
    cfg = Config(backend_url="ws://localhost:8000", guild_id="myguild")
    assert cfg.ws_url == "ws://localhost:8000/ws/myguild"


def test_ws_url_converts_http_to_ws():
    cfg = Config(backend_url="http://localhost:8000", guild_id="g1")
    assert cfg.ws_url == "ws://localhost:8000/ws/g1"


def test_ws_url_converts_https_to_wss():
    cfg = Config(backend_url="https://example.com", guild_id="g2")
    assert cfg.ws_url == "wss://example.com/ws/g2"


# ---------------------------------------------------------------------------
# load() — error cases
# ---------------------------------------------------------------------------


def test_load_missing_file_raises(tmp_path):
    env = {"PIONEER_BACKEND_URL": "", "PIONEER_GUILD_ID": ""}
    with patch.dict("os.environ", env, clear=False), pytest.raises(FileNotFoundError):
        load(str(tmp_path / "missing.toml"))


def test_load_missing_file_no_overrides_raises():
    env = {"PIONEER_BACKEND_URL": "", "PIONEER_GUILD_ID": ""}
    with patch.dict("os.environ", env, clear=False), pytest.raises(FileNotFoundError):
        load("/nonexistent/pioneer-worker.toml")


# ---------------------------------------------------------------------------
# load() — overrides bypass missing file
# ---------------------------------------------------------------------------


def test_load_overrides_no_file(tmp_path):
    cfg = load(
        str(tmp_path / "missing.toml"),
        overrides={"backend_url": "ws://test:8000", "guild_id": "testguild"},
    )
    assert cfg.backend_url == "ws://test:8000"
    assert cfg.guild_id == "testguild"


def test_load_overrides_defaults_preserved(tmp_path):
    cfg = load(
        str(tmp_path / "missing.toml"),
        overrides={"backend_url": "ws://x:1", "guild_id": "g"},
    )
    assert cfg.pull_interval == 300.0
    assert cfg.claude_max_turns == 50
    assert cfg.max_agents == 4


# ---------------------------------------------------------------------------
# load() — from TOML file
# ---------------------------------------------------------------------------


def test_load_from_toml(tmp_path):
    toml_path = tmp_path / "pioneer-worker.toml"
    toml_path.write_text(
        'backend_url = "ws://backend:8000"\nguild_id = "guild1"\n[github]\nrepos = ["owner/repo"]\n'
    )
    cfg = load(str(toml_path))
    assert cfg.backend_url == "ws://backend:8000"
    assert cfg.guild_id == "guild1"
    assert "owner/repo" in cfg.repos


def test_load_toml_custom_pull_interval(tmp_path):
    toml_path = tmp_path / "pioneer-worker.toml"
    toml_path.write_text('backend_url = "ws://x:1"\nguild_id = "g"\npull_interval = 60.0\n')
    cfg = load(str(toml_path))
    assert cfg.pull_interval == 60.0


def test_load_toml_github_token_literal(tmp_path):
    toml_path = tmp_path / "pioneer-worker.toml"
    toml_path.write_text(
        'backend_url = "ws://x:1"\nguild_id = "g"\n[github]\ntoken = "ghp_literal"\n'
    )
    cfg = load(str(toml_path))
    assert cfg.github_token == "ghp_literal"


def test_load_toml_github_token_env_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_GH_TOKEN", "ghp_from_env")
    toml_path = tmp_path / "pioneer-worker.toml"
    toml_path.write_text(
        'backend_url = "ws://x:1"\nguild_id = "g"\n[github]\ntoken = "env:MY_GH_TOKEN"\n'
    )
    cfg = load(str(toml_path))
    assert cfg.github_token == "ghp_from_env"


def test_load_codex_args_from_toml(tmp_path):
    toml_path = tmp_path / "pioneer-worker.toml"
    toml_path.write_text(
        'backend_url = "ws://x:1"\nguild_id = "g"\n'
        '[codex]\nargs = ["--sandbox", "workspace-write", "--ask-for-approval", "never"]\n'
    )
    cfg = load(str(toml_path))
    assert cfg.codex_args == ["--sandbox", "workspace-write", "--ask-for-approval", "never"]


def test_load_codex_args_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://x:1")
    monkeypatch.setenv("PIONEER_GUILD_ID", "g")
    monkeypatch.setenv("PIONEER_CODEX_ARGS", '--sandbox workspace-write -m "gpt-5.4"')
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.codex_args == ["--sandbox", "workspace-write", "-m", "gpt-5.4"]


def test_load_codex_doctor_can_be_disabled(tmp_path):
    toml_path = tmp_path / "pioneer-worker.toml"
    toml_path.write_text('backend_url = "ws://x:1"\nguild_id = "g"\n[codex]\ndoctor = false\n')
    cfg = load(str(toml_path))
    assert cfg.codex_doctor is False


# ---------------------------------------------------------------------------
# load() — environment variable overrides
# ---------------------------------------------------------------------------


def test_load_env_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://envhost:9000")
    monkeypatch.setenv("PIONEER_GUILD_ID", "envguild")
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.backend_url == "ws://envhost:9000"
    assert cfg.guild_id == "envguild"


def test_load_overrides_beat_toml(tmp_path):
    toml_path = tmp_path / "pioneer-worker.toml"
    toml_path.write_text('backend_url = "ws://toml:8000"\nguild_id = "tomlguild"\n')
    cfg = load(str(toml_path), overrides={"backend_url": "ws://override:1111"})
    assert cfg.backend_url == "ws://override:1111"
    assert cfg.guild_id == "tomlguild"


def test_load_overrides_beat_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://env:1")
    toml_path = tmp_path / "pioneer-worker.toml"
    toml_path.write_text('backend_url = "ws://toml:1"\nguild_id = "g"\n')
    cfg = load(str(toml_path), overrides={"backend_url": "ws://override:2"})
    assert cfg.backend_url == "ws://override:2"


# ---------------------------------------------------------------------------
# load() — github token env var fallbacks (used by spawn-worker docker path)
# ---------------------------------------------------------------------------


def test_load_github_token_from_pioneer_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://x:1")
    monkeypatch.setenv("PIONEER_GUILD_ID", "g")
    monkeypatch.setenv("PIONEER_GITHUB_TOKEN", "ghp_pioneer")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.github_token == "ghp_pioneer"


def test_load_github_token_from_github_token_env(tmp_path, monkeypatch):
    """Backend-spawned containers set GITHUB_TOKEN (no TOML mounted)."""
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://x:1")
    monkeypatch.setenv("PIONEER_GUILD_ID", "g")
    monkeypatch.delenv("PIONEER_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_github")
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.github_token == "ghp_github"


def test_load_github_token_pioneer_beats_github(tmp_path, monkeypatch):
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://x:1")
    monkeypatch.setenv("PIONEER_GUILD_ID", "g")
    monkeypatch.setenv("PIONEER_GITHUB_TOKEN", "ghp_pioneer")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_github")
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.github_token == "ghp_pioneer"


def test_load_github_token_empty_pioneer_falls_through(tmp_path, monkeypatch):
    """Empty PIONEER_GITHUB_TOKEN should not mask GITHUB_TOKEN."""
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://x:1")
    monkeypatch.setenv("PIONEER_GUILD_ID", "g")
    monkeypatch.setenv("PIONEER_GITHUB_TOKEN", "")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_github")
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.github_token == "ghp_github"


# ---------------------------------------------------------------------------
# load() — org field
# ---------------------------------------------------------------------------


def test_org_defaults_to_none():
    cfg = Config(backend_url="ws://x:1", guild_id="g")
    assert cfg.org is None


def test_load_org_from_toml(tmp_path):
    toml_path = tmp_path / "pioneer-worker.toml"
    toml_path.write_text('backend_url = "ws://x:1"\nguild_id = "g"\n[github]\norg = "myorg"\n')
    cfg = load(str(toml_path))
    assert cfg.org == "myorg"


def test_load_org_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://x:1")
    monkeypatch.setenv("PIONEER_GUILD_ID", "g")
    monkeypatch.setenv("PIONEER_ORG", "envorg")
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.org == "envorg"


def test_load_org_override_beats_toml(tmp_path):
    toml_path = tmp_path / "pioneer-worker.toml"
    toml_path.write_text('backend_url = "ws://x:1"\nguild_id = "g"\n[github]\norg = "tomlorg"\n')
    cfg = load(str(toml_path), overrides={"org": "overrideorg"})
    assert cfg.org == "overrideorg"


def test_load_org_and_repos_can_coexist(tmp_path):
    toml_path = tmp_path / "pioneer-worker.toml"
    toml_path.write_text(
        'backend_url = "ws://x:1"\nguild_id = "g"\n'
        '[github]\norg = "myorg"\nrepos = ["myorg/extra"]\n'
    )
    cfg = load(str(toml_path))
    assert cfg.org == "myorg"
    assert "myorg/extra" in cfg.repos


def test_load_repos_only_org_none(tmp_path):
    toml_path = tmp_path / "pioneer-worker.toml"
    toml_path.write_text(
        'backend_url = "ws://x:1"\nguild_id = "g"\n[github]\nrepos = ["owner/repo"]\n'
    )
    cfg = load(str(toml_path))
    assert cfg.org is None
    assert "owner/repo" in cfg.repos


# ---------------------------------------------------------------------------
# load() — pre-assigned worker_id / auth_token (foreman spawn_worker path)
# ---------------------------------------------------------------------------


def test_load_worker_id_from_env(tmp_path, monkeypatch):
    """PIONEER_WORKER_ID is picked up so the worker can skip self-registration."""
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://x:1")
    monkeypatch.setenv("PIONEER_GUILD_ID", "g")
    monkeypatch.setenv("PIONEER_WORKER_ID", "w-abc123")
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.worker_id == "w-abc123"


def test_load_auth_token_from_env(tmp_path, monkeypatch):
    """PIONEER_AUTH_TOKEN lets a pre-registered worker skip self-registration."""
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://x:1")
    monkeypatch.setenv("PIONEER_GUILD_ID", "g")
    monkeypatch.setenv("PIONEER_AUTH_TOKEN", "tok-secret")
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.auth_token == "tok-secret"


def test_load_worker_id_none_by_default(tmp_path):
    cfg = load(
        str(tmp_path / "missing.toml"),
        overrides={"backend_url": "ws://x:1", "guild_id": "g"},
    )
    assert cfg.worker_id is None
    assert cfg.auth_token is None


def test_load_max_agents_from_env(tmp_path, monkeypatch):
    """PIONEER_MAX_AGENTS overrides the default of 4."""
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://x:1")
    monkeypatch.setenv("PIONEER_GUILD_ID", "g")
    monkeypatch.setenv("PIONEER_MAX_AGENTS", "2")
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.max_agents == 2


def test_load_max_agents_toml_beats_env(tmp_path, monkeypatch):
    """max_agents in TOML takes priority over PIONEER_MAX_AGENTS."""
    monkeypatch.setenv("PIONEER_MAX_AGENTS", "2")
    toml_path = tmp_path / "pioneer-worker.toml"
    toml_path.write_text('backend_url = "ws://x:1"\nguild_id = "g"\nmax_agents = 8\n')
    cfg = load(str(toml_path))
    assert cfg.max_agents == 8
