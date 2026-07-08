"""Configuration for the standalone Pioneer Square foreman.

Reads a TOML file (default: ./pioneer-foreman.toml).
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from backend.foreman.llm import BedrockModelNotConfiguredError, get_foreman_model

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_NAME = "pioneer-foreman.toml"


@dataclass
class Config:
    backend_url: str
    guild_id: str
    # LLM provider settings for the API proxy.
    model: str = "claude-sonnet-4-6"
    # Bedrock uses cross-region inference profiles, not plain model IDs, and those
    # profiles are scoped to a single AWS account, so there is no valid default.
    # Ignored when provider != "bedrock"; required when it is (see effective_model).
    bedrock_model: str | None = None
    api_key: str | None = None
    # Anthropic auth token (OAuth/claude.ai accounts).  Mutually exclusive with
    # api_key; when set it is forwarded as `auth_token` to AsyncAnthropic and
    # ANTHROPIC_API_KEY is ignored.  Reads ANTHROPIC_AUTH_TOKEN env var or
    # [llm] auth_token in the TOML ([claude] still works as a back-compat alias).
    anthropic_auth_token: str | None = None
    # "anthropic" (default), "bedrock" (Amazon Bedrock via AsyncAnthropicBedrock),
    # or "openai" (OpenAI-compatible /v1/chat/completions endpoint).
    provider: str = "anthropic"
    # Base URL for provider="openai"; e.g. http://localhost:11434/v1 for Ollama.
    openai_base_url: str = "http://localhost:11434/v1"
    api_timeout: float = 600.0
    # AWS region for Bedrock; ignored when provider != "bedrock"
    aws_region: str = "us-east-1"
    # Named AWS profile for Bedrock (defaults to AWS_PROFILE env); ignored when
    # provider != "bedrock". None falls back to boto3's default credential chain.
    aws_profile: str | None = None
    # Logging
    log_level: str = "INFO"

    config_path: Path = field(default_factory=Path)

    @property
    def effective_model(self) -> str:
        """Return the model ID appropriate for the configured provider.

        Delegates the provider-branching logic to
        backend.foreman.llm.get_foreman_model(), passing this Config's own
        fields as explicit overrides (they already fold in the TOML/env layering
        done in load()) rather than re-implementing the branch here. The
        Bedrock-not-configured error is caught and re-raised with a
        Config/TOML-specific message, since the standalone foreman's users
        configure this via pioneer-foreman.toml, not guild settings.
        """
        try:
            return get_foreman_model(
                self.provider, model=self.model, bedrock_model=self.bedrock_model
            )
        except BedrockModelNotConfiguredError as exc:
            raise BedrockModelNotConfiguredError(
                "Bedrock provider selected but no model is configured. Set "
                "[llm] bedrock_model in the config TOML or the "
                "FOREMAN_BEDROCK_MODEL environment variable."
            ) from exc

    @property
    def http_url(self) -> str:
        """Backend HTTP URL derived from backend_url."""
        parsed = urlparse(self.backend_url)
        scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme or "http")
        return urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))

    @property
    def ws_url(self) -> str:
        """Backend WebSocket URL for this guild."""
        parsed = urlparse(self.backend_url)
        scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme or "ws")
        base = urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
        return f"{base}/ws/{self.guild_id}"


def _resolve_config_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    return Path.cwd() / DEFAULT_CONFIG_NAME


def load(explicit_path: str | None = None, overrides: dict | None = None) -> Config:
    """Load config from TOML layered with env vars and CLI overrides."""
    overrides = overrides or {}
    cfg_path = _resolve_config_path(explicit_path)

    raw: dict = {}
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh)
    else:
        has_url = (
            overrides.get("backend_url")
            or os.environ.get("PIONEER_BACKEND_URL")
            or os.environ.get("BACKEND_WS_URL")
        )
        has_sid = (
            overrides.get("guild_id")
            or os.environ.get("PIONEER_GUILD_ID")
            or os.environ.get("GUILD_ID")
        )
        if not (has_url and has_sid):
            raise FileNotFoundError(
                f"Foreman config not found at {cfg_path}. "
                "Create one (see pioneer-foreman.toml.example), pass --config, "
                "or supply --backend-url and --guild-id."
            )

    # [claude] is the historical name; [llm] is preferred now that the process
    # can proxy Anthropic, Bedrock, or OpenAI-compatible API calls.
    claude_block = raw.get("claude") or {}
    llm_block = {**claude_block, **(raw.get("llm") or {})}

    backend_url = (
        overrides.get("backend_url")
        or raw.get("backend_url")
        or os.environ.get("PIONEER_BACKEND_URL")
        or os.environ.get("BACKEND_WS_URL")
    )
    guild_id = (
        overrides.get("guild_id")
        or raw.get("guild_id")
        or os.environ.get("PIONEER_GUILD_ID")
        or os.environ.get("GUILD_ID")
    )
    if not backend_url:
        raise ValueError("backend_url is required.")
    if not guild_id:
        raise ValueError("guild_id is required.")

    provider = (
        overrides.get("provider")
        or llm_block.get("provider")
        or os.environ.get("FOREMAN_PROVIDER")
        or "anthropic"
    ).lower()

    api_key = (
        overrides.get("api_key")
        or llm_block.get("api_key")
        or (
            os.environ.get("OPENAI_API_KEY")
            if provider == "openai"
            else os.environ.get("ANTHROPIC_API_KEY")
        )
    ) or None

    anthropic_auth_token = (
        overrides.get("anthropic_auth_token")
        or llm_block.get("auth_token")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ) or None

    model = (
        overrides.get("model")
        or llm_block.get("model")
        or os.environ.get("FOREMAN_MODEL")
        or ("llama3.1" if provider == "openai" else "claude-sonnet-4-6")
    )

    # Trailing `or None` normalises an empty string (e.g. an unset TOML/env
    # value) to None rather than propagating it — an empty string is not a
    # valid ARN/model, and `effective_model` treats None as "not configured"
    # to raise BedrockModelNotConfiguredError instead of failing on empty.
    bedrock_model = (
        overrides.get("bedrock_model")
        or llm_block.get("bedrock_model")
        or os.environ.get("FOREMAN_BEDROCK_MODEL")
    ) or None

    openai_base_url = (
        overrides.get("base_url")
        or overrides.get("openai_base_url")
        or llm_block.get("base_url")
        or llm_block.get("openai_base_url")
        or os.environ.get("FOREMAN_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "http://localhost:11434/v1"
    )

    aws_region = (
        overrides.get("aws_region")
        or llm_block.get("aws_region")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )

    aws_profile = (
        overrides.get("aws_profile")
        or llm_block.get("aws_profile")
        or os.environ.get("AWS_PROFILE")
    ) or None

    log_level = (
        overrides.get("log_level")
        or raw.get("log_level")
        or os.environ.get("FOREMAN_LOG_LEVEL")
        or os.environ.get("LOG_LEVEL")
        or "INFO"
    )

    return Config(
        backend_url=backend_url.rstrip("/"),
        guild_id=guild_id,
        model=model,
        bedrock_model=bedrock_model,
        api_key=api_key,
        anthropic_auth_token=anthropic_auth_token,
        provider=provider,
        openai_base_url=openai_base_url.rstrip("/"),
        api_timeout=float(
            overrides.get(
                "api_timeout",
                llm_block.get("api_timeout", os.environ.get("FOREMAN_API_TIMEOUT", 600.0)),
            )
        ),
        aws_region=aws_region,
        aws_profile=aws_profile,
        log_level=log_level.upper(),
        config_path=cfg_path,
    )
