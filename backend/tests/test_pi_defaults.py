"""Unit tests for the pi provider/model default fallback (#1040)."""

from foreman.tools import _apply_pi_defaults

GUILD_CFG = {"pi_default_provider": "bedrock", "pi_default_model": "arn:aws:...:profile/x"}


def test_pi_defaults_fill_when_unset():
    provider, model = _apply_pi_defaults("pi", None, None, GUILD_CFG)
    assert provider == "bedrock"
    assert model == "arn:aws:...:profile/x"


def test_explicit_values_win_over_guild_defaults():
    provider, model = _apply_pi_defaults("pi", "anthropic", "claude-sonnet-4-6", GUILD_CFG)
    assert provider == "anthropic"
    assert model == "claude-sonnet-4-6"


def test_non_pi_tool_is_untouched():
    assert _apply_pi_defaults("codex", None, None, GUILD_CFG) == (None, None)


def test_missing_config_leaves_values_none():
    assert _apply_pi_defaults("pi", None, None, {}) == (None, None)
    assert _apply_pi_defaults("pi", None, None, None) == (None, None)
