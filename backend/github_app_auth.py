"""GitHub App authentication — installation-token generation and caching.

Lets Pioneer Square comment on GitHub as a GitHub App identity (e.g.
``pioneer-square-melloy[bot]``) instead of a personal access token, so
automated comments/reviews are attributed to the automation rather than
whichever human's PAT happens to be configured.

Enabled by setting all three of ``GITHUB_APP_ID``, ``GITHUB_APP_PRIVATE_KEY``
(or ``GITHUB_APP_PRIVATE_KEY_PATH``), and ``GITHUB_APP_INSTALLATION_ID``. When
any is missing, :func:`get_github_token` falls back to the ``GITHUB_TOKEN``
env var — existing deployments without a GitHub App configured see no change
in behavior.

``GITHUB_APP_SLUG`` is an optional fourth var giving the bot's attributed
username (e.g. ``pioneer-square-melloy[bot]``); see :func:`get_app_slug`. It
defaults to a generic placeholder when unset.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (read at call time from environment — see module docstring)
# ---------------------------------------------------------------------------

GITHUB_APP_ID = "GITHUB_APP_ID"
GITHUB_APP_PRIVATE_KEY = "GITHUB_APP_PRIVATE_KEY"
GITHUB_APP_PRIVATE_KEY_PATH = "GITHUB_APP_PRIVATE_KEY_PATH"
GITHUB_APP_INSTALLATION_ID = "GITHUB_APP_INSTALLATION_ID"
GITHUB_APP_SLUG = "GITHUB_APP_SLUG"
DEFAULT_APP_SLUG = "github-app[bot]"

# Cached installation token, refreshed once within 5 minutes of expiry.
_token_cache: dict[str, Any] = {"token": None, "expires_at": None}

# Guards the cache check + refresh in get_app_installation_token(). A plain
# threading.Lock (not asyncio.Lock) because this function is synchronous and
# is called from both sync (utils.build_spawn_worker_env) and async
# (foreman/tools._guild_github_token) call sites — an asyncio.Lock can't be
# used outside a running event loop's coroutine context.
_cache_lock = threading.Lock()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_jwt(app_id: str, private_key_pem: str) -> str:
    """Generate a short-lived (10 min) RS256 JWT for GitHub App authentication.

    Signed with the App's private key; ``iss`` is the App ID, per GitHub's
    JWT-based App authentication scheme.
    """
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": now - 60,  # allow for clock drift, as GitHub's docs recommend
        "exp": now + 10 * 60,
        "iss": str(app_id),
    }
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()

    private_key = load_pem_private_key(private_key_pem.encode(), password=None)
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}"


def _request_installation_token(app_id: str, private_key_pem: str, installation_id: str) -> dict:
    """POST to GitHub's installation access-token endpoint and return the parsed JSON body."""
    jwt_token = generate_jwt(app_id, private_key_pem)
    response = httpx.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def get_installation_token(app_id: str, private_key_pem: str, installation_id: str) -> str:
    """Exchange a signed App JWT for a short-lived installation access token."""
    return _request_installation_token(app_id, private_key_pem, installation_id)["token"]


def _app_credentials() -> tuple[str, str, str] | None:
    """Return (app_id, private_key_pem, installation_id) if all App env vars are set, else None."""
    app_id = os.environ.get(GITHUB_APP_ID)
    installation_id = os.environ.get(GITHUB_APP_INSTALLATION_ID)
    private_key_pem = os.environ.get(GITHUB_APP_PRIVATE_KEY)
    if not private_key_pem:
        key_path = os.environ.get(GITHUB_APP_PRIVATE_KEY_PATH)
        if key_path:
            try:
                private_key_pem = Path(key_path).read_text()
            except OSError:
                logger.warning(
                    "Failed to read GITHUB_APP_PRIVATE_KEY_PATH %r; falling back to GITHUB_TOKEN",
                    key_path,
                    exc_info=True,
                )
                return None
    if app_id and installation_id and private_key_pem:
        return app_id, private_key_pem, installation_id
    return None


def get_app_slug() -> str:
    """Return the bot username to attribute App-authenticated actions to.

    Falls back to a generic default when ``GITHUB_APP_SLUG`` isn't set.
    """
    return os.environ.get(GITHUB_APP_SLUG) or DEFAULT_APP_SLUG


def get_app_installation_token() -> str | None:
    """Return a cached GitHub App installation token, or None if the App isn't configured.

    Refreshes when the cached token is within 5 minutes of expiry.
    """
    creds = _app_credentials()
    if creds is None:
        return None
    app_id, private_key_pem, installation_id = creds

    with _cache_lock:
        now = datetime.now(UTC)
        cached_expiry = _token_cache["expires_at"]
        if _token_cache["token"] and cached_expiry and cached_expiry - now > timedelta(minutes=5):
            return _token_cache["token"]

        data = _request_installation_token(app_id, private_key_pem, installation_id)
        _token_cache["token"] = data["token"]
        _token_cache["expires_at"] = datetime.fromisoformat(
            data["expires_at"].replace("Z", "+00:00")
        )
        return _token_cache["token"]


def get_github_token(fallback: str | None = None) -> str | None:
    """Return the GitHub token to use for API/``gh`` CLI calls, or None if unconfigured.

    Prefers a GitHub App installation token when the App env vars are set;
    otherwise returns *fallback* if given, else the ``GITHUB_TOKEN`` env var
    (or None if neither the App nor ``GITHUB_TOKEN`` is configured).
    """
    token = get_app_installation_token()
    if token:
        return token
    if fallback is not None:
        return fallback
    return os.environ.get("GITHUB_TOKEN") or None
