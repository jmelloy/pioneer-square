"""Unit tests for pioneer_foreman.config."""

from __future__ import annotations

import pytest
from pioneer_foreman.config import Config, load

# ── Config properties ─────────────────────────────────────────────────────


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


def test_ws_url_appends_guild():
    cfg = Config(backend_url="ws://localhost:8000", guild_id="myguild")
    assert cfg.ws_url == "ws://localhost:8000/ws/myguild"


def test_ws_url_converts_http_to_ws():
    cfg = Config(backend_url="http://localhost:8000", guild_id="g1")
    assert cfg.ws_url == "ws://localhost:8000/ws/g1"


def test_ws_url_converts_https_to_wss():
    cfg = Config(backend_url="https://example.com", guild_id="g2")
    assert cfg.ws_url == "wss://example.com/ws/g2"


def test_effective_model_anthropic():
    cfg = Config(
        backend_url="ws://x:1", guild_id="g", provider="anthropic", model="claude-opus-4-8"
    )
    assert cfg.effective_model == "claude-opus-4-8"


def test_effective_model_bedrock_returns_bedrock_model():
    cfg = Config(
        backend_url="ws://x:1",
        guild_id="g",
        provider="bedrock",
        bedrock_model="us.anthropic.claude-3-5-sonnet-20241022-v2:0",
    )
    assert cfg.effective_model == "us.anthropic.claude-3-5-sonnet-20241022-v2:0"


def test_effective_model_bedrock_ignores_model_field():
    cfg = Config(
        backend_url="ws://x:1",
        guild_id="g",
        provider="bedrock",
        model="claude-sonnet-4-6",
        bedrock_model="bedrock-profile-id",
    )
    assert cfg.effective_model == "bedrock-profile-id"


# ── load() — error cases ──────────────────────────────────────────────────


def test_load_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("PIONEER_BACKEND_URL", raising=False)
    monkeypatch.delenv("BACKEND_WS_URL", raising=False)
    monkeypatch.delenv("PIONEER_GUILD_ID", raising=False)
    monkeypatch.delenv("GUILD_ID", raising=False)
    with pytest.raises(FileNotFoundError, match="Foreman config not found"):
        load(str(tmp_path / "missing.toml"))


def test_load_raises_without_backend_url(tmp_path, monkeypatch):
    # File exists but omits backend_url; env fallbacks cleared
    monkeypatch.delenv("PIONEER_BACKEND_URL", raising=False)
    monkeypatch.delenv("BACKEND_WS_URL", raising=False)
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text('guild_id = "g"\n')
    with pytest.raises(ValueError, match="backend_url is required"):
        load(str(toml_path))


def test_load_raises_without_guild_id(tmp_path, monkeypatch):
    # File exists but omits guild_id; env fallbacks cleared
    monkeypatch.delenv("PIONEER_GUILD_ID", raising=False)
    monkeypatch.delenv("GUILD_ID", raising=False)
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text('backend_url = "ws://x:1"\n')
    with pytest.raises(ValueError, match="guild_id is required"):
        load(str(toml_path))


# ── load() — overrides bypass missing file ───────────────────────────────


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
    assert cfg.max_rounds == 10
    assert cfg.history_limit == 40
    assert cfg.model == "claude-sonnet-4-6"
    assert cfg.provider == "anthropic"
    # Per-task child contexts default on.
    assert cfg.child_contexts is True


def test_child_contexts_disabled_via_toml(tmp_path):
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text('backend_url = "ws://x:1"\nguild_id = "g"\nchild_contexts = false\n')
    cfg = load(str(toml_path))
    assert cfg.child_contexts is False


def test_child_contexts_disabled_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREMAN_CHILD_CONTEXTS", "0")
    cfg = load(
        str(tmp_path / "missing.toml"),
        overrides={"backend_url": "ws://x:1", "guild_id": "g"},
    )
    assert cfg.child_contexts is False


# ── load() — from TOML file ──────────────────────────────────────────────


def test_load_from_toml(tmp_path):
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text('backend_url = "ws://backend:8000"\nguild_id = "guild1"\n')
    cfg = load(str(toml_path))
    assert cfg.backend_url == "ws://backend:8000"
    assert cfg.guild_id == "guild1"


def test_load_toml_backend_key(tmp_path):
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text('backend_url = "ws://x:1"\nguild_id = "g"\nbackend_key = "s3cr3t"\n')
    cfg = load(str(toml_path))
    assert cfg.backend_key == "s3cr3t"


def test_load_toml_claude_block(tmp_path):
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text(
        'backend_url = "ws://x:1"\nguild_id = "g"\n[claude]\nmodel = "claude-opus-4-8"\nmax_rounds = 20\n'
    )
    cfg = load(str(toml_path))
    assert cfg.model == "claude-opus-4-8"
    assert cfg.max_rounds == 20


def test_load_toml_bedrock_provider(tmp_path):
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text(
        'backend_url = "ws://x:1"\nguild_id = "g"\n[claude]\nprovider = "bedrock"\naws_region = "us-west-2"\n'
    )
    cfg = load(str(toml_path))
    assert cfg.provider == "bedrock"
    assert cfg.aws_region == "us-west-2"


# ── load() — env var overrides ────────────────────────────────────────────


def test_load_env_vars_for_url_and_guild(tmp_path, monkeypatch):
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://envhost:9000")
    monkeypatch.setenv("PIONEER_GUILD_ID", "envguild")
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.backend_url == "ws://envhost:9000"
    assert cfg.guild_id == "envguild"


def test_load_env_legacy_backend_url(tmp_path, monkeypatch):
    monkeypatch.delenv("PIONEER_BACKEND_URL", raising=False)
    monkeypatch.setenv("BACKEND_WS_URL", "ws://legacy:8000")
    monkeypatch.setenv("PIONEER_GUILD_ID", "g")
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.backend_url == "ws://legacy:8000"


def test_load_env_backend_key_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://x:1")
    monkeypatch.setenv("PIONEER_GUILD_ID", "g")
    monkeypatch.setenv("PIONEER_FOREMAN_KEY", "envkey123")
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.backend_key == "envkey123"


def test_load_env_model(tmp_path, monkeypatch):
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://x:1")
    monkeypatch.setenv("PIONEER_GUILD_ID", "g")
    monkeypatch.setenv("FOREMAN_MODEL", "claude-haiku-4-5-20251001")
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.model == "claude-haiku-4-5-20251001"


def test_load_env_guild_id_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("PIONEER_GUILD_ID", raising=False)
    monkeypatch.setenv("GUILD_ID", "legacyguild")
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://x:1")
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.guild_id == "legacyguild"


# ── load() — precedence: overrides > TOML > env ──────────────────────────


def test_overrides_beat_toml(tmp_path):
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text('backend_url = "ws://toml:8000"\nguild_id = "tomlguild"\n')
    cfg = load(str(toml_path), overrides={"backend_url": "ws://override:1111"})
    assert cfg.backend_url == "ws://override:1111"
    assert cfg.guild_id == "tomlguild"


def test_overrides_beat_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://env:1")
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text('backend_url = "ws://toml:1"\nguild_id = "g"\n')
    cfg = load(str(toml_path), overrides={"backend_url": "ws://override:2"})
    assert cfg.backend_url == "ws://override:2"


def test_toml_beats_env_for_backend_key(tmp_path, monkeypatch):
    monkeypatch.setenv("PIONEER_FOREMAN_KEY", "envkey")
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text('backend_url = "ws://x:1"\nguild_id = "g"\nbackend_key = "tomlkey"\n')
    cfg = load(str(toml_path))
    assert cfg.backend_key == "tomlkey"


def test_log_level_uppercased(tmp_path):
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text('backend_url = "ws://x:1"\nguild_id = "g"\nlog_level = "debug"\n')
    cfg = load(str(toml_path))
    assert cfg.log_level == "DEBUG"


def test_backend_url_trailing_slash_stripped(tmp_path):
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text('backend_url = "ws://x:1/"\nguild_id = "g"\n')
    cfg = load(str(toml_path))
    assert not cfg.backend_url.endswith("/")


# ── Anthropic auth_token ──────────────────────────────────────────────────


def test_load_toml_claude_auth_token(tmp_path):
    """[claude] auth_token in TOML is loaded as anthropic_auth_token."""
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text(
        'backend_url = "ws://x:1"\nguild_id = "g"\n[claude]\nauth_token = "tok-toml"\n'
    )
    cfg = load(str(toml_path))
    assert cfg.anthropic_auth_token == "tok-toml"


def test_load_env_anthropic_auth_token(tmp_path, monkeypatch):
    """ANTHROPIC_AUTH_TOKEN env var is loaded as anthropic_auth_token."""
    monkeypatch.setenv("PIONEER_BACKEND_URL", "ws://x:1")
    monkeypatch.setenv("PIONEER_GUILD_ID", "g")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-env")
    cfg = load(str(tmp_path / "missing.toml"))
    assert cfg.anthropic_auth_token == "tok-env"


def test_load_toml_auth_token_beats_env(tmp_path, monkeypatch):
    """TOML [claude] auth_token takes precedence over ANTHROPIC_AUTH_TOKEN env var."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-env")
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text(
        'backend_url = "ws://x:1"\nguild_id = "g"\n[claude]\nauth_token = "tok-toml"\n'
    )
    cfg = load(str(toml_path))
    assert cfg.anthropic_auth_token == "tok-toml"


def test_load_anthropic_auth_token_defaults_to_none(tmp_path):
    """anthropic_auth_token is None when not configured anywhere."""
    toml_path = tmp_path / "pioneer-foreman.toml"
    toml_path.write_text('backend_url = "ws://x:1"\nguild_id = "g"\n')
    cfg = load(str(toml_path))
    assert cfg.anthropic_auth_token is None
