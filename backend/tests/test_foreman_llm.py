"""Tests for foreman_core/llm.py — provider selection and client factory.

Verifies that make_anthropic_client() and get_foreman_model() correctly handle
both the default Anthropic provider and alternate providers (e.g. AWS Bedrock).
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_anthropic(anthropic_cls=None, bedrock_cls=None):
    """Build a minimal mock of the anthropic module."""
    mock_mod = MagicMock()
    mock_mod.AsyncAnthropic = anthropic_cls or MagicMock(return_value=MagicMock())
    mock_mod.AsyncAnthropicBedrock = bedrock_cls or MagicMock(return_value=MagicMock())
    return mock_mod


# ---------------------------------------------------------------------------
# get_foreman_model
# ---------------------------------------------------------------------------


class TestGetForemanModel:
    def test_default_returns_anthropic_model(self, monkeypatch):
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        monkeypatch.delenv("FOREMAN_MODEL", raising=False)
        from foreman_core.llm import get_foreman_model

        assert get_foreman_model() == "claude-sonnet-4-6"

    def test_bedrock_provider_env_returns_bedrock_model(self, monkeypatch):
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        monkeypatch.delenv("FOREMAN_BEDROCK_MODEL", raising=False)
        from foreman_core.llm import _DEFAULT_BEDROCK_MODEL, get_foreman_model

        assert get_foreman_model() == _DEFAULT_BEDROCK_MODEL

    def test_explicit_provider_overrides_env(self, monkeypatch):
        monkeypatch.setenv("FOREMAN_PROVIDER", "anthropic")
        from foreman_core.llm import _DEFAULT_BEDROCK_MODEL, get_foreman_model

        assert get_foreman_model(provider="bedrock") == _DEFAULT_BEDROCK_MODEL

    def test_custom_bedrock_model_env(self, monkeypatch):
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        monkeypatch.setenv("FOREMAN_BEDROCK_MODEL", "arn:aws:bedrock:us-west-2::my-profile")
        from foreman_core.llm import get_foreman_model

        assert get_foreman_model() == "arn:aws:bedrock:us-west-2::my-profile"

    def test_custom_anthropic_model_env(self, monkeypatch):
        monkeypatch.setenv("FOREMAN_PROVIDER", "anthropic")
        monkeypatch.setenv("FOREMAN_MODEL", "claude-opus-4-8")
        from foreman_core.llm import get_foreman_model

        assert get_foreman_model() == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# make_anthropic_client — provider dispatch
# ---------------------------------------------------------------------------


class TestMakeAnthropicClient:
    def test_default_creates_anthropic_client(self, monkeypatch):
        """When no provider is specified and FOREMAN_PROVIDER is unset, use AsyncAnthropic."""
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman_core.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client()
        mock_mod.AsyncAnthropic.assert_called_once()
        mock_mod.AsyncAnthropicBedrock.assert_not_called()

    def test_bedrock_env_creates_bedrock_client(self, monkeypatch):
        """FOREMAN_PROVIDER=bedrock must produce an AsyncAnthropicBedrock client."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        import foreman_core.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client()
        mock_mod.AsyncAnthropicBedrock.assert_called_once()
        mock_mod.AsyncAnthropic.assert_not_called()

    def test_explicit_bedrock_provider_arg_overrides_env(self, monkeypatch):
        """Passing provider='bedrock' explicitly must use Bedrock even if env says anthropic."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "anthropic")
        import foreman_core.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(provider="bedrock")
        mock_mod.AsyncAnthropicBedrock.assert_called_once()
        mock_mod.AsyncAnthropic.assert_not_called()

    def test_explicit_anthropic_provider_arg_overrides_bedrock_env(self, monkeypatch):
        """Passing provider='anthropic' must use AsyncAnthropic even if env says bedrock."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        import foreman_core.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(provider="anthropic", api_key="test-key")
        mock_mod.AsyncAnthropic.assert_called_once()
        mock_mod.AsyncAnthropicBedrock.assert_not_called()

    def test_bedrock_passes_region(self, monkeypatch):
        """The aws_region argument must be forwarded to AsyncAnthropicBedrock."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        import foreman_core.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        # Patch the module-level constant directly rather than relying on env + reload.
        monkeypatch.setattr(llm_mod, "_BEDROCK_REGION", "eu-west-1")
        llm_mod.make_anthropic_client()
        mock_mod.AsyncAnthropicBedrock.assert_called_once_with(aws_region="eu-west-1")

    def test_bedrock_region_explicit_arg_overrides_env(self, monkeypatch):
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        import foreman_core.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        monkeypatch.setattr(llm_mod, "_BEDROCK_REGION", "us-east-1")
        llm_mod.make_anthropic_client(provider="bedrock", region="ap-southeast-1")
        mock_mod.AsyncAnthropicBedrock.assert_called_once_with(aws_region="ap-southeast-1")

    def test_api_key_forwarded_to_anthropic_client(self, monkeypatch):
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman_core.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(api_key="sk-test-123")
        mock_mod.AsyncAnthropic.assert_called_once_with(api_key="sk-test-123")

    def test_no_api_key_omitted_from_kwargs(self, monkeypatch):
        """When api_key is None, it must not be passed to AsyncAnthropic (lets SDK read env)."""
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman_core.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client()
        mock_mod.AsyncAnthropic.assert_called_once_with()

    def test_missing_anthropic_package_raises(self, monkeypatch):
        """If anthropic is not installed, make_anthropic_client must raise ImportError."""
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman_core.llm as llm_mod

        # Explicitly force the "no anthropic" path regardless of CI environment so the
        # test exercises the ImportError branch even when the package is installed.
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", False)
        monkeypatch.setattr(llm_mod, "_anthropic_mod", None)
        with pytest.raises(ImportError):
            llm_mod.make_anthropic_client()


# ---------------------------------------------------------------------------
# Regression: make_anthropic_client reads FOREMAN_PROVIDER dynamically
# ---------------------------------------------------------------------------


class TestMakeAnthropicClientDynamicEnv:
    """Regression for issue #608: make_anthropic_client() must read FOREMAN_PROVIDER from
    os.environ on every call, not from a module-level constant captured at import time.

    This ensures that tests and environments that set the env var after module import
    get the correct provider, and matches the behaviour of get_foreman_model().
    """

    def test_bedrock_env_set_after_import_is_respected(self, monkeypatch):
        """Patching FOREMAN_PROVIDER after module import must still produce a Bedrock client."""
        # Start with anthropic, import module, then switch to bedrock — the call must still
        # create AsyncAnthropicBedrock because we read os.environ dynamically.
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman_core.llm as llm_mod

        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        mock_mod = _make_mock_anthropic()
        with patch.object(llm_mod, "_anthropic_mod", mock_mod):
            llm_mod.HAS_ANTHROPIC = True
            llm_mod.make_anthropic_client()
            mock_mod.AsyncAnthropicBedrock.assert_called_once()
            mock_mod.AsyncAnthropic.assert_not_called()

    def test_anthropic_env_set_after_bedrock_reload(self, monkeypatch):
        """Switching from bedrock back to anthropic must produce an AsyncAnthropic client."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        import foreman_core.llm as llm_mod

        monkeypatch.setenv("FOREMAN_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        mock_mod = _make_mock_anthropic()
        with patch.object(llm_mod, "_anthropic_mod", mock_mod):
            llm_mod.HAS_ANTHROPIC = True
            llm_mod.make_anthropic_client()
            mock_mod.AsyncAnthropic.assert_called_once()
            mock_mod.AsyncAnthropicBedrock.assert_not_called()
