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


_PROVIDER_ENV_VARS = (
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_REGION",
    "AWS_REGION",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)


@pytest.fixture(autouse=True)
def _clear_ambient_provider_env(monkeypatch):
    """Strip ambient provider credentials so tests assert on a clean slate.

    Without this, a host/CI env that exports e.g. AWS_BEARER_TOKEN_BEDROCK or
    ANTHROPIC_API_KEY would make make_anthropic_client() forward extra kwargs
    (api_key / auth_token / aws_*) and break the exact-kwargs assertions below.
    """
    for var in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


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

    def test_bedrock_aws_env_explicit_keys_forwarded(self, monkeypatch):
        """Guild env_vars (settings dialogue) carrying AWS keys must reach the SDK.

        Regression: the settings dialogue stores AWS creds in foreman_config
        env_vars, which only ever reached spawned workers — not the foreman's
        own Bedrock client — so boto3 raised 'could not resolve credentials'.
        """
        import foreman_core.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(
            provider="bedrock",
            extra_env={
                "AWS_ACCESS_KEY_ID": "AKIATEST",
                "AWS_SECRET_ACCESS_KEY": "secret",
                "AWS_SESSION_TOKEN": "token",
                "AWS_DEFAULT_REGION": "eu-west-1",
            },
        )
        mock_mod.AsyncAnthropicBedrock.assert_called_once_with(
            aws_region="eu-west-1",
            aws_access_key="AKIATEST",
            aws_secret_key="secret",
            aws_session_token="token",
        )

    def test_bedrock_aws_env_bearer_token_forwarded(self, monkeypatch):
        """A bearer token in guild env_vars must be passed as api_key, not SigV4."""
        import foreman_core.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        monkeypatch.setattr(llm_mod, "_BEDROCK_REGION", "us-east-1")
        llm_mod.make_anthropic_client(
            provider="bedrock",
            extra_env={"AWS_BEARER_TOKEN_BEDROCK": "bedrock-token-xyz"},
        )
        mock_mod.AsyncAnthropicBedrock.assert_called_once_with(
            aws_region="us-east-1", api_key="bedrock-token-xyz"
        )

    def test_bedrock_aws_env_profile_forwarded(self, monkeypatch):
        """An AWS_PROFILE in guild env_vars must be passed as aws_profile."""
        import foreman_core.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        monkeypatch.setattr(llm_mod, "_BEDROCK_REGION", "us-east-1")
        # Patch boto3 session probe so a missing real profile doesn't error the test.
        with patch.object(llm_mod, "_log_bedrock_credentials"):
            llm_mod.make_anthropic_client(
                provider="bedrock", extra_env={"AWS_PROFILE": "my-sso-profile"}
            )
        mock_mod.AsyncAnthropicBedrock.assert_called_once_with(
            aws_region="us-east-1", aws_profile="my-sso-profile"
        )

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

    def test_anthropic_extra_env_api_key_and_base_url_forwarded(self, monkeypatch):
        """Guild env_vars carrying ANTHROPIC_* must reach the direct API client.

        Like the Bedrock case, the settings dialogue's env_vars never reach the
        foreman process, so they must be forwarded explicitly.
        """
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman_core.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(
            provider="anthropic",
            extra_env={
                "ANTHROPIC_API_KEY": "sk-ant-guild",
                "ANTHROPIC_BASE_URL": "https://proxy.example.com",
            },
        )
        mock_mod.AsyncAnthropic.assert_called_once_with(
            api_key="sk-ant-guild", base_url="https://proxy.example.com"
        )

    def test_anthropic_extra_env_auth_token_forwarded(self, monkeypatch):
        """ANTHROPIC_AUTH_TOKEN in guild env_vars must be passed as auth_token."""
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman_core.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(
            provider="anthropic", extra_env={"ANTHROPIC_AUTH_TOKEN": "tok-abc"}
        )
        mock_mod.AsyncAnthropic.assert_called_once_with(auth_token="tok-abc")

    def test_explicit_api_key_wins_over_extra_env(self, monkeypatch):
        """An explicit api_key arg takes precedence over extra_env ANTHROPIC_API_KEY."""
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman_core.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(
            api_key="sk-explicit", extra_env={"ANTHROPIC_API_KEY": "sk-from-env"}
        )
        mock_mod.AsyncAnthropic.assert_called_once_with(api_key="sk-explicit")

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
