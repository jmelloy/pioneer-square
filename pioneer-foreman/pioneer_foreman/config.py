"""Configuration for the Pioneer Square standalone foreman.

Reads from a TOML file (default: ``./pioneer-foreman.toml``) layered with
environment variables and optional programmatic overrides.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

DEFAULT_CONFIG_NAME = "pioneer-foreman.toml"


@dataclass
class Config:
    backend_url: str
    guild_id: str
    # Bearer token for REST API calls (worker auth_token from registration).
    # Populated by Foreman.run() after _register() when not pre-configured.
    # Pre-configure via PIONEER_FOREMAN_AUTH_TOKEN or the auth_token TOML key
    # to skip re-registration on each restart.
    auth_token: str | None = None
    # Claude model for the foreman AI
    model: str = "claude-sonnet-4-6"
    # Optional GitHub token for direct GitHub API calls.
    # If absent, fetched from the backend at first use via /auth/github/token.
    github_token: str | None = None
    # Seconds between background poll cycles (sent as foreman-poll-status broadcasts)
    poll_interval: float = 60.0
    # Path used for display / resolving relative paths
    config_path: Path = field(default_factory=Path)

    @property
    def http_url(self) -> str:
        """Backend HTTP base URL derived from backend_url."""
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
    """Load config from TOML layered with env vars and optional overrides.

    Priority (highest → lowest):
      1. *overrides* dict (programmatic, e.g. from CLI flags)
      2. Environment variables
      3. TOML file values
      4. Hardcoded defaults

    If the config file is missing but env vars supply ``backend_url`` and
    ``guild_id``, the missing file is treated as an empty config.
    """
    overrides = overrides or {}
    cfg_path = _resolve_config_path(explicit_path)

    raw: dict = {}
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh)
    else:
        has_url = overrides.get("backend_url") or os.environ.get("PIONEER_BACKEND_URL")
        has_sid = overrides.get("guild_id") or os.environ.get("PIONEER_GUILD_ID")
        if not (has_url and has_sid):
            raise FileNotFoundError(
                f"Foreman config not found at {cfg_path}. "
                "Create one (see pioneer-foreman.toml.example), pass --config, "
                "or supply PIONEER_BACKEND_URL and PIONEER_GUILD_ID env vars."
            )

    backend_url = (
        overrides.get("backend_url")
        or raw.get("backend_url")
        or os.environ.get("PIONEER_BACKEND_URL")
    )
    guild_id = (
        overrides.get("guild_id") or raw.get("guild_id") or os.environ.get("PIONEER_GUILD_ID")
    )
    if not backend_url:
        raise ValueError(
            "backend_url is required (in config, --backend-url, or PIONEER_BACKEND_URL)."
        )
    if not guild_id:
        raise ValueError("guild_id is required (in config, --guild-id, or PIONEER_GUILD_ID).")

    anthropic_block = raw.get("anthropic") or {}

    # GitHub token: overrides → PIONEER_GITHUB_TOKEN → GITHUB_TOKEN → TOML
    github_token: str | None = (
        overrides.get("github_token")
        or os.environ.get("PIONEER_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or raw.get("github_token")
    ) or None

    return Config(
        backend_url=backend_url.rstrip("/"),
        guild_id=guild_id,
        auth_token=(
            overrides.get("auth_token")
            or os.environ.get("PIONEER_FOREMAN_AUTH_TOKEN")
            or raw.get("auth_token")
        )
        or None,
        model=(
            overrides.get("model")
            or os.environ.get("FOREMAN_MODEL")
            or anthropic_block.get("model")
            or raw.get("model")
            or "claude-sonnet-4-6"
        ),
        github_token=github_token,
        poll_interval=float(
            overrides.get("poll_interval")
            if overrides.get("poll_interval") is not None
            else raw.get("poll_interval", 60.0)
        ),
        config_path=cfg_path,
    )
