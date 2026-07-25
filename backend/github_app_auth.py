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
from urllib.parse import quote

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

# Cached installation tokens, keyed by installation_id, each refreshed once
# within 5 minutes of expiry. One App (app_id + private key) can have many
# installations — one per guild/account — so tokens must be cached per id.
_token_cache: dict[str, dict[str, Any]] = {}

# Cached (git_name, git_email) for the App's bot, keyed by slug. The bot's
# numeric user id (needed for the commit-attribution noreply email) is fetched
# once from the GitHub API and never changes for a given App.
_bot_identity_cache: dict[str, tuple[str, str]] = {}

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


def _app_key_credentials() -> tuple[str, str] | None:
    """Return (app_id, private_key_pem) if the App id + private key are set, else None.

    The installation id is resolved separately (per-guild, falling back to the
    ``GITHUB_APP_INSTALLATION_ID`` env var) since one App has many installations.
    """
    app_id = os.environ.get(GITHUB_APP_ID)
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
    if app_id and private_key_pem:
        return app_id, private_key_pem
    return None


def get_app_slug() -> str:
    """Return the bot username to attribute App-authenticated actions to.

    Falls back to a generic default when ``GITHUB_APP_SLUG`` isn't set.
    """
    return os.environ.get(GITHUB_APP_SLUG) or DEFAULT_APP_SLUG


def get_app_installation_token(installation_id: str | None = None) -> str | None:
    """Return a cached GitHub App installation token, or None if the App isn't configured.

    *installation_id* selects which installation (guild/account) to mint for;
    when omitted it falls back to the ``GITHUB_APP_INSTALLATION_ID`` env var so
    single-tenant deploys keep working unchanged. Refreshes when the cached
    token is within 5 minutes of expiry.
    """
    creds = _app_key_credentials()
    if creds is None:
        return None
    app_id, private_key_pem = creds
    installation_id = installation_id or os.environ.get(GITHUB_APP_INSTALLATION_ID)
    if not installation_id:
        return None

    with _cache_lock:
        now = datetime.now(UTC)
        entry = _token_cache.get(installation_id)
        if entry and entry["expires_at"] - now > timedelta(minutes=5):
            return entry["token"]

        data = _request_installation_token(app_id, private_key_pem, installation_id)
        expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        _token_cache[installation_id] = {"token": data["token"], "expires_at": expires_at}
        return data["token"]


def get_app_bot_identity() -> tuple[str, str] | None:
    """Return (git_name, git_email) to author commits as the App's bot, or None.

    GitHub attributes a commit to the bot when the author email is the bot's
    ``<user_id>+<slug>@users.noreply.github.com`` noreply address. The numeric
    user id is fetched once from ``GET /users/<slug>`` and cached. Returns None
    when ``GITHUB_APP_SLUG`` isn't configured or the lookup fails.
    """
    slug = get_app_slug()
    if slug == DEFAULT_APP_SLUG:
        return None
    cached = _bot_identity_cache.get(slug)
    if cached:
        return cached
    try:
        resp = httpx.get(
            f"https://api.github.com/users/{quote(slug, safe='')}",
            headers={"Accept": "application/vnd.github+json"},
            timeout=15,
        )
        resp.raise_for_status()
        user_id = resp.json()["id"]
    except (httpx.HTTPError, KeyError):
        logger.warning("Failed to fetch bot user id for %r", slug, exc_info=True)
        return None
    identity = (slug, f"{user_id}+{slug}@users.noreply.github.com")
    _bot_identity_cache[slug] = identity
    return identity


def get_github_token(fallback: str | None = None, installation_id: str | None = None) -> str | None:
    """Return the GitHub token to use for API/``gh`` CLI calls, or None if unconfigured.

    Prefers a GitHub App installation token when the App env vars are set;
    otherwise returns *fallback* if given, else the ``GITHUB_TOKEN`` env var
    (or None if neither the App nor ``GITHUB_TOKEN`` is configured).
    """
    token = get_app_installation_token(installation_id)
    if token:
        return token
    if fallback is not None:
        return fallback
    return os.environ.get("GITHUB_TOKEN") or None
