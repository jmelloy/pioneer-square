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

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_NAME = "pioneer-foreman.toml"


@dataclass
class Config:
    backend_url: str
    guild_id: str
    # JWT auth — shared HS256 secret (PIONEER_FOREMAN_KEY on the backend side).
    # Preferred over auth_token: the foreman mints short-lived tokens automatically.
    backend_key: str | None = None
    # Static token fallback — a member login_token or worker auth_token.
    # Used only when backend_key is not set.
    auth_token: str | None = None
    # Claude settings
    model: str = "claude-sonnet-4-6"
    api_key: str | None = None
    max_rounds: int = 10
    history_limit: int = 40
    # Poll settings
    poll_min_interval: int = 60
    poll_max_interval: int = 3600
    # Logging
    log_level: str = "INFO"

    config_path: Path = field(default_factory=Path)

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

    claude_block = raw.get("claude") or {}
    poll_block = raw.get("poll") or {}

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

    backend_key = (
        overrides.get("backend_key")
        or raw.get("backend_key")
        or os.environ.get("PIONEER_FOREMAN_KEY")
    ) or None

    auth_token = (
        overrides.get("auth_token")
        or raw.get("auth_token")
        or os.environ.get("PIONEER_AUTH_TOKEN")
        or os.environ.get("FOREMAN_AUTH_TOKEN")
    ) or None

    api_key = (
        overrides.get("api_key")
        or claude_block.get("api_key")
        or os.environ.get("ANTHROPIC_API_KEY")
    ) or None

    model = (
        overrides.get("model")
        or claude_block.get("model")
        or os.environ.get("FOREMAN_MODEL")
        or "claude-sonnet-4-6"
    )

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
        backend_key=backend_key,
        auth_token=auth_token,
        model=model,
        api_key=api_key,
        max_rounds=int(overrides.get("max_rounds", claude_block.get("max_rounds", 10))),
        history_limit=int(overrides.get("history_limit", claude_block.get("history_limit", 40))),
        poll_min_interval=int(
            overrides.get("poll_min_interval", poll_block.get("min_interval", 60))
        ),
        poll_max_interval=int(
            overrides.get("poll_max_interval", poll_block.get("max_interval", 3600))
        ),
        log_level=log_level.upper(),
        config_path=cfg_path,
    )
