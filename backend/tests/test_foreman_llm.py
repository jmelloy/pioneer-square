"""Tests for foreman/llm.py — provider selection and client factory.

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
        from foreman.llm import get_foreman_model

        assert get_foreman_model() == "claude-sonnet-4-6"

    def test_bedrock_provider_env_without_model_raises(self, monkeypatch):
        """Regression for #817: Bedrock inference profiles are AWS-account-scoped, so
        silently falling back to a hardcoded ARN sends requests to the wrong account.
        Must raise a clear, actionable error instead."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        monkeypatch.delenv("FOREMAN_BEDROCK_MODEL", raising=False)
        from foreman.llm import BedrockModelNotConfiguredError, get_foreman_model

        with pytest.raises(BedrockModelNotConfiguredError):
            get_foreman_model()

    def test_explicit_provider_without_model_raises(self, monkeypatch):
        monkeypatch.setenv("FOREMAN_PROVIDER", "anthropic")
        monkeypatch.delenv("FOREMAN_BEDROCK_MODEL", raising=False)
        from foreman.llm import BedrockModelNotConfiguredError, get_foreman_model

        with pytest.raises(BedrockModelNotConfiguredError):
            get_foreman_model(provider="bedrock")

    def test_custom_bedrock_model_env(self, monkeypatch):
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        monkeypatch.setenv("FOREMAN_BEDROCK_MODEL", "arn:aws:bedrock:us-west-2::my-profile")
        from foreman.llm import get_foreman_model

        assert get_foreman_model() == "arn:aws:bedrock:us-west-2::my-profile"

    def test_custom_anthropic_model_env(self, monkeypatch):
        monkeypatch.setenv("FOREMAN_PROVIDER", "anthropic")
        monkeypatch.setenv("FOREMAN_MODEL", "claude-opus-4-8")
        from foreman.llm import get_foreman_model

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
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client()
        mock_mod.AsyncAnthropic.assert_called_once()
        mock_mod.AsyncAnthropicBedrock.assert_not_called()

    def test_bedrock_env_creates_bedrock_client(self, monkeypatch):
        """FOREMAN_PROVIDER=bedrock must produce an AsyncAnthropicBedrock client."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(model="test-bedrock-model")
        mock_mod.AsyncAnthropicBedrock.assert_called_once()
        mock_mod.AsyncAnthropic.assert_not_called()

    def test_explicit_bedrock_provider_arg_overrides_env(self, monkeypatch):
        """Passing provider='bedrock' explicitly must use Bedrock even if env says anthropic."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "anthropic")
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(provider="bedrock", model="test-bedrock-model")
        mock_mod.AsyncAnthropicBedrock.assert_called_once()
        mock_mod.AsyncAnthropic.assert_not_called()

    def test_bedrock_no_model_raises_before_client_construction(self, monkeypatch):
        """Regression for PR #818 review: a missing Bedrock model must raise
        BedrockModelNotConfiguredError immediately, before AsyncAnthropicBedrock is
        ever constructed — not fail later inside the first API call."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        monkeypatch.delenv("FOREMAN_BEDROCK_MODEL", raising=False)
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        with pytest.raises(llm_mod.BedrockModelNotConfiguredError):
            llm_mod.make_anthropic_client()
        mock_mod.AsyncAnthropicBedrock.assert_not_called()

    def test_bedrock_model_falls_back_to_env(self, monkeypatch):
        """When no model arg is passed, FOREMAN_BEDROCK_MODEL satisfies the check."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        monkeypatch.setenv("FOREMAN_BEDROCK_MODEL", "arn:aws:bedrock:us-west-2::my-profile")
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client()
        mock_mod.AsyncAnthropicBedrock.assert_called_once()

    def test_explicit_anthropic_provider_arg_overrides_bedrock_env(self, monkeypatch):
        """Passing provider='anthropic' must use AsyncAnthropic even if env says bedrock."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(provider="anthropic", api_key="test-key")
        mock_mod.AsyncAnthropic.assert_called_once()
        mock_mod.AsyncAnthropicBedrock.assert_not_called()

    def test_bedrock_passes_region(self, monkeypatch):
        """The aws_region argument must be forwarded to AsyncAnthropicBedrock."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        # Patch the module-level constant directly rather than relying on env + reload.
        monkeypatch.setattr(llm_mod, "_BEDROCK_REGION", "eu-west-1")
        llm_mod.make_anthropic_client(model="test-bedrock-model")
        mock_mod.AsyncAnthropicBedrock.assert_called_once_with(aws_region="eu-west-1")

    def test_bedrock_region_explicit_arg_overrides_env(self, monkeypatch):
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        monkeypatch.setattr(llm_mod, "_BEDROCK_REGION", "us-east-1")
        llm_mod.make_anthropic_client(
            provider="bedrock", region="ap-southeast-1", model="test-bedrock-model"
        )
        mock_mod.AsyncAnthropicBedrock.assert_called_once_with(aws_region="ap-southeast-1")

    def test_bedrock_aws_env_explicit_keys_forwarded(self, monkeypatch):
        """Guild env_vars (settings dialogue) carrying AWS keys must reach the SDK.

        Regression: the settings dialogue stores AWS creds in foreman_config
        env_vars, which only ever reached spawned workers — not the foreman's
        own Bedrock client — so boto3 raised 'could not resolve credentials'.
        """
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(
            provider="bedrock",
            model="test-bedrock-model",
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
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        monkeypatch.setattr(llm_mod, "_BEDROCK_REGION", "us-east-1")
        llm_mod.make_anthropic_client(
            provider="bedrock",
            model="test-bedrock-model",
            extra_env={"AWS_BEARER_TOKEN_BEDROCK": "bedrock-token-xyz"},
        )
        mock_mod.AsyncAnthropicBedrock.assert_called_once_with(
            aws_region="us-east-1", api_key="bedrock-token-xyz"
        )

    def test_bedrock_aws_env_profile_forwarded(self, monkeypatch):
        """An AWS_PROFILE in guild env_vars must be passed as aws_profile."""
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        monkeypatch.setattr(llm_mod, "_BEDROCK_REGION", "us-east-1")
        # Patch boto3 session probe so a missing real profile doesn't error the test.
        with patch.object(llm_mod, "_log_bedrock_credentials"):
            llm_mod.make_anthropic_client(
                provider="bedrock",
                model="test-bedrock-model",
                extra_env={"AWS_PROFILE": "my-sso-profile"},
            )
        mock_mod.AsyncAnthropicBedrock.assert_called_once_with(
            aws_region="us-east-1", aws_profile="my-sso-profile"
        )

    def test_api_key_forwarded_to_anthropic_client(self, monkeypatch):
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(api_key="sk-test-123")
        mock_mod.AsyncAnthropic.assert_called_once_with(api_key="sk-test-123")

    def test_no_api_key_omitted_from_kwargs(self, monkeypatch):
        """When api_key is None, it must not be passed to AsyncAnthropic (lets SDK read env)."""
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman.llm as llm_mod

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
        import foreman.llm as llm_mod

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
        import foreman.llm as llm_mod

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
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(
            api_key="sk-explicit", extra_env={"ANTHROPIC_API_KEY": "sk-from-env"}
        )
        mock_mod.AsyncAnthropic.assert_called_once_with(api_key="sk-explicit")

    @pytest.mark.parametrize("placeholder", ["placeholder", "Placeholder", " placeholder ", ""])
    def test_placeholder_env_api_key_omitted_from_kwargs(self, monkeypatch, placeholder):
        """ANTHROPIC_API_KEY=placeholder (any case/whitespace) must not reach the SDK."""
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(extra_env={"ANTHROPIC_API_KEY": placeholder})
        mock_mod.AsyncAnthropic.assert_called_once_with()

    def test_placeholder_explicit_api_key_omitted_from_kwargs(self, monkeypatch):
        """An explicit api_key of "placeholder" must not reach the SDK either."""
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(api_key="placeholder")
        mock_mod.AsyncAnthropic.assert_called_once_with()

    def test_placeholder_env_falls_back_to_real_explicit_api_key(self, monkeypatch):
        """A real explicit api_key must still be used when ANTHROPIC_API_KEY is a placeholder."""
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(
            api_key="sk-explicit", extra_env={"ANTHROPIC_API_KEY": "placeholder"}
        )
        mock_mod.AsyncAnthropic.assert_called_once_with(api_key="sk-explicit")

    def test_auth_token_wins_over_process_api_key(self, monkeypatch):
        """Regression for #660: ANTHROPIC_AUTH_TOKEN in guild env_vars must win over a
        process-level ANTHROPIC_API_KEY so the SDK never receives both (it raises ValueError
        when api_key and auth_token are both supplied)."""
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        # Simulate a server environment where ANTHROPIC_API_KEY is set in the process env
        # while the guild config supplies ANTHROPIC_AUTH_TOKEN via extra_env.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-process-level")
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(
            provider="anthropic", extra_env={"ANTHROPIC_AUTH_TOKEN": "tok-guild-configured"}
        )
        # Must pass ONLY auth_token — not api_key — to avoid the SDK ValueError.
        mock_mod.AsyncAnthropic.assert_called_once_with(auth_token="tok-guild-configured")

    def test_auth_token_wins_over_explicit_api_key_arg(self, monkeypatch):
        """ANTHROPIC_AUTH_TOKEN in extra_env wins even when api_key is passed explicitly."""
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(
            provider="anthropic",
            api_key="sk-explicit",
            extra_env={"ANTHROPIC_AUTH_TOKEN": "tok-guild-configured"},
        )
        mock_mod.AsyncAnthropic.assert_called_once_with(auth_token="tok-guild-configured")

    def test_missing_anthropic_package_raises(self, monkeypatch):
        """If anthropic is not installed, make_anthropic_client must raise ImportError."""
        monkeypatch.delenv("FOREMAN_PROVIDER", raising=False)
        import foreman.llm as llm_mod

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
        import foreman.llm as llm_mod

        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        mock_mod = _make_mock_anthropic()
        with patch.object(llm_mod, "_anthropic_mod", mock_mod):
            llm_mod.HAS_ANTHROPIC = True
            llm_mod.make_anthropic_client(model="test-bedrock-model")
            mock_mod.AsyncAnthropicBedrock.assert_called_once()
            mock_mod.AsyncAnthropic.assert_not_called()

    def test_anthropic_env_set_after_bedrock_reload(self, monkeypatch):
        """Switching from bedrock back to anthropic must produce an AsyncAnthropic client."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        import foreman.llm as llm_mod

        monkeypatch.setenv("FOREMAN_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        mock_mod = _make_mock_anthropic()
        with patch.object(llm_mod, "_anthropic_mod", mock_mod):
            llm_mod.HAS_ANTHROPIC = True
            llm_mod.make_anthropic_client()
            mock_mod.AsyncAnthropic.assert_called_once()
            mock_mod.AsyncAnthropicBedrock.assert_not_called()


class TestMakeAnthropicClientNativeBedrockModel:
    """Amazon Nova / Kimi K2 have their own wire format and must dispatch to
    BedrockNativeClient (Converse API), not AsyncAnthropicBedrock (Messages API)."""

    def test_nova_model_returns_bedrock_native_client(self, monkeypatch):
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        import foreman.llm as llm_mod
        from foreman.providers.bedrock import BedrockNativeClient

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        client = llm_mod.make_anthropic_client(model="amazon.nova-pro-v1:0")
        assert isinstance(client, BedrockNativeClient)
        mock_mod.AsyncAnthropicBedrock.assert_not_called()

    def test_claude_on_bedrock_model_still_uses_anthropic_sdk(self, monkeypatch):
        """Sanity check: a Claude model ID on Bedrock must still take the
        existing AsyncAnthropicBedrock path, not the native Converse path."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        import foreman.llm as llm_mod

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        llm_mod.make_anthropic_client(model="anthropic.claude-sonnet-4-6-20251001-v1:0")
        mock_mod.AsyncAnthropicBedrock.assert_called_once()


class TestMakeAnthropicClientResponsesApiModel:
    """OpenAI gpt-oss-120b/20b speak the Responses API (bedrock-mantle), not
    the Converse API or AsyncAnthropicBedrock, and must dispatch to
    BedrockResponsesClient."""

    def test_gpt_oss_model_returns_bedrock_responses_client(self, monkeypatch):
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        import foreman.llm as llm_mod
        from foreman.providers.bedrock import BedrockResponsesClient

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        client = llm_mod.make_anthropic_client(model="openai.gpt-oss-120b")
        assert isinstance(client, BedrockResponsesClient)
        mock_mod.AsyncAnthropicBedrock.assert_not_called()

    def test_gpt_oss_safeguard_model_is_not_routed_to_responses_client(self, monkeypatch):
        """The Safeguard variants support Chat Completions but not the
        Responses API (per AWS's per-model compatibility table) — they must
        not be routed to BedrockResponsesClient. Not otherwise a native
        Bedrock model either, so this exercises the same generic
        AsyncAnthropicBedrock fallback as any unrecognized model ID."""
        monkeypatch.setenv("FOREMAN_PROVIDER", "bedrock")
        import foreman.llm as llm_mod
        from foreman.providers.bedrock import BedrockNativeClient, BedrockResponsesClient

        mock_mod = _make_mock_anthropic()
        monkeypatch.setattr(llm_mod, "_anthropic_mod", mock_mod)
        monkeypatch.setattr(llm_mod, "HAS_ANTHROPIC", True)
        client = llm_mod.make_anthropic_client(model="openai.gpt-oss-safeguard-120b")
        assert not isinstance(client, BedrockResponsesClient)
        assert not isinstance(client, BedrockNativeClient)
        mock_mod.AsyncAnthropicBedrock.assert_called_once()


# ---------------------------------------------------------------------------
# OpenAI-compatible translation (issue #826: shared with the standalone proxy,
# which has no translation logic of its own — see foreman/tests/test_runner.py)
# ---------------------------------------------------------------------------


class TestOpenAiTranslation:
    def test_anthropic_tools_to_openai_wrap_schema(self):
        from foreman.llm import anthropic_tools_to_openai

        tools = [
            {
                "name": "assign_task",
                "description": "Assign a task.",
                "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}},
            }
        ]

        assert anthropic_tools_to_openai(tools) == [
            {
                "type": "function",
                "function": {
                    "name": "assign_task",
                    "description": "Assign a task.",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "string"}},
                    },
                },
            }
        ]

    def test_anthropic_messages_to_openai_convert_tool_exchange(self):
        from foreman.llm import anthropic_messages_to_openai

        messages = [
            {"role": "user", "content": [{"type": "text", "text": "Create it"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll do that."},
                    {
                        "type": "tool_use",
                        "id": "toolu-1",
                        "name": "create_task",
                        "input": {"name": "Build"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu-1", "content": "created"}
                ],
            },
        ]

        converted = anthropic_messages_to_openai([{"type": "text", "text": "System"}], messages)

        assert converted == [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Create it"},
            {
                "role": "assistant",
                "content": "I'll do that.",
                "tool_calls": [
                    {
                        "id": "toolu-1",
                        "type": "function",
                        "function": {"name": "create_task", "arguments": '{"name": "Build"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "toolu-1", "content": "created"},
        ]

    def test_openai_response_to_anthropic_maps_tool_calls(self):
        from foreman.llm import openai_response_to_anthropic

        raw = {
            "id": "chatcmpl-1",
            "model": "llama3.1",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "Checking.",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "get_task_status",
                                    "arguments": '{"task_id": "t-1"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }

        normalised = openai_response_to_anthropic(raw, "llama3.1")

        assert normalised["stop_reason"] == "tool_use"
        assert normalised["usage"]["input_tokens"] == 12
        assert normalised["usage"]["output_tokens"] == 3
        assert normalised["content"] == [
            {"type": "text", "text": "Checking."},
            {
                "type": "tool_use",
                "id": "call-1",
                "name": "get_task_status",
                "input": {"task_id": "t-1"},
            },
        ]

    async def test_call_openai_compatible_posts_chat_completion(self):
        import json

        import httpx
        from foreman.llm import call_openai_compatible

        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["json"] = json.loads(request.read())
            return httpx.Response(
                200,
                headers={"x-request-id": "req-openai"},
                json={
                    "id": "chatcmpl-2",
                    "model": "llama3.1",
                    "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            response, request_id = await call_openai_compatible(
                client,
                model="llama3.1",
                max_tokens=32,
                system=[{"type": "text", "text": "System"}],
                messages=[{"role": "user", "content": "Hi"}],
                base_url="http://ollama.test/v1",
                api_key="key",
            )
        finally:
            await client.aclose()

        assert captured["url"] == "http://ollama.test/v1/chat/completions"
        assert captured["json"]["model"] == "llama3.1"
        assert "tools" not in captured["json"]
        assert captured["headers"]["authorization"] == "Bearer key"
        assert request_id == "req-openai"
        assert response["content"] == [{"type": "text", "text": "done"}]

    async def test_call_openai_compatible_omits_tools_when_none_given(self):
        """No tools passed → no `tools` key in the POSTed body, matching
        call_anthropic — some OpenAI-compatible endpoints reject an empty list."""
        import httpx
        from foreman.llm import call_openai_compatible

        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["json"] = json.loads(request.read())
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-3",
                    "model": "llama3.1",
                    "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await call_openai_compatible(
                client,
                model="llama3.1",
                max_tokens=32,
                messages=[{"role": "user", "content": "Hi"}],
                base_url="http://ollama.test/v1",
            )
        finally:
            await client.aclose()

        assert "tools" not in captured["json"]

    async def test_call_openai_compatible_omits_empty_tool_choice(self):
        """Regression for #850: an empty `tool_choice={}` must not be forwarded —
        OpenAI (and compatible) APIs reject an empty tool_choice object."""
        import httpx
        from foreman.llm import call_openai_compatible

        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["json"] = json.loads(request.read())
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-4",
                    "model": "llama3.1",
                    "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await call_openai_compatible(
                client,
                model="llama3.1",
                max_tokens=32,
                messages=[{"role": "user", "content": "Hi"}],
                base_url="http://ollama.test/v1",
                tool_choice={},
            )
        finally:
            await client.aclose()

        assert "tool_choice" not in captured["json"]

    async def test_call_openai_compatible_includes_tool_choice_when_given(self):
        """Complements test_call_openai_compatible_omits_empty_tool_choice: a
        non-empty `tool_choice` is forwarded through to the POSTed body."""
        import json

        import httpx
        from foreman.llm import call_openai_compatible

        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["json"] = json.loads(request.read())
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-5",
                    "model": "llama3.1",
                    "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await call_openai_compatible(
                client,
                model="llama3.1",
                max_tokens=32,
                messages=[{"role": "user", "content": "Hi"}],
                base_url="http://ollama.test/v1",
                tool_choice={"type": "auto"},
            )
        finally:
            await client.aclose()

        assert captured["json"]["tool_choice"] == {"type": "auto"}


# ---------------------------------------------------------------------------
# call_anthropic — shared Anthropic Messages API call, used locally by the
# embedded foreman and by the standalone proxy against its own client.
# ---------------------------------------------------------------------------


class TestCallAnthropic:
    async def test_call_anthropic_returns_parsed_response_and_request_id(self):
        """call_anthropic returns the SDK's own parsed response object (not a
        pre-dumped dict) so local callers keep full attribute access; callers
        that need JSON (the proxy) call .model_dump() themselves."""
        from foreman.llm import call_anthropic

        response_payload = {"id": "msg_1", "type": "message", "content": []}
        parsed_response = SimpleNamespace(model_dump=lambda: response_payload)

        class _RawResponse:
            headers = {"request-id": "req-1"}

            def parse(self):
                return parsed_response

        async def create(**kwargs):
            create.kwargs = kwargs
            return _RawResponse()

        client = SimpleNamespace(
            messages=SimpleNamespace(with_raw_response=SimpleNamespace(create=create))
        )

        response, request_id = await call_anthropic(
            client,
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=[{"type": "text", "text": "sys"}],
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            tool_choice={"type": "auto"},
        )

        assert response is parsed_response
        assert response.model_dump() == response_payload
        assert request_id == "req-1"
        assert create.kwargs["tool_choice"] == {"type": "auto"}
        assert "tools" not in create.kwargs

    async def test_call_anthropic_omits_tools_when_none_given(self):
        """Regression for PR #849 review: an empty `tools` array is not sent to
        the Anthropic API — some endpoints reject it — and an explicit
        `tools=None` from the caller isn't silently coerced into `[]` either."""
        from foreman.llm import call_anthropic

        async def create(**kwargs):
            create.kwargs = kwargs

            class _RawResponse:
                headers = {}

                def parse(self):
                    return SimpleNamespace(model_dump=lambda: {})

            return _RawResponse()

        client = SimpleNamespace(
            messages=SimpleNamespace(with_raw_response=SimpleNamespace(create=create))
        )

        await call_anthropic(
            client,
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=[{"type": "text", "text": "sys"}],
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
        )

        assert "tools" not in create.kwargs

    async def test_call_anthropic_includes_tools_when_given(self):
        from foreman.llm import call_anthropic

        async def create(**kwargs):
            create.kwargs = kwargs

            class _RawResponse:
                headers = {}

                def parse(self):
                    return SimpleNamespace(model_dump=lambda: {})

            return _RawResponse()

        client = SimpleNamespace(
            messages=SimpleNamespace(with_raw_response=SimpleNamespace(create=create))
        )

        tools = [{"name": "assign_task", "input_schema": {}}]
        await call_anthropic(
            client,
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
        )

        assert create.kwargs["tools"] == tools

    async def test_call_anthropic_omits_empty_tool_choice(self):
        """Regression for #850: an empty `tool_choice={}` must not be forwarded —
        some endpoints reject an empty tool_choice object just like empty tools."""
        import httpx
        from foreman.llm import call_anthropic

        async def create(**kwargs):
            create.kwargs = kwargs

            class _RawResponse:
                headers = httpx.Headers({})

                def parse(self):
                    return SimpleNamespace(model_dump=lambda: {})

            return _RawResponse()

        client = SimpleNamespace(
            messages=SimpleNamespace(with_raw_response=SimpleNamespace(create=create))
        )

        await call_anthropic(
            client,
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": "hi"}],
            tool_choice={},
        )

        assert "tool_choice" not in create.kwargs

    async def test_call_anthropic_includes_tool_choice_when_given(self):
        """Complements test_call_anthropic_omits_empty_tool_choice: a non-empty
        `tool_choice` is forwarded to the API as-is."""
        import httpx
        from foreman.llm import call_anthropic

        async def create(**kwargs):
            create.kwargs = kwargs

            class _RawResponse:
                headers = httpx.Headers({})

                def parse(self):
                    return SimpleNamespace(model_dump=lambda: {})

            return _RawResponse()

        client = SimpleNamespace(
            messages=SimpleNamespace(with_raw_response=SimpleNamespace(create=create))
        )

        await call_anthropic(
            client,
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": "hi"}],
            tool_choice={"type": "auto"},
        )

        assert create.kwargs["tool_choice"] == {"type": "auto"}
