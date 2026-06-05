"""Fetch and cache the models.dev provider/model catalog.

Fetched once at startup (or on first request) and refreshed every TTL seconds.
Falls back to a minimal static list if the fetch fails.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
_CACHE_TTL = 3600  # 1 hour

# Minimal fallback so the UI always has something to show.
_FALLBACK_PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "anthropic",
        "name": "Anthropic",
        "models": [
            {"id": "claude-opus-4-8", "name": "Claude Opus 4.8"},
            {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
            {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5"},
        ],
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "models": [
            {"id": "gpt-4o", "name": "GPT-4o"},
            {"id": "o4-mini", "name": "o4-mini"},
            {"id": "gpt-4.1", "name": "GPT-4.1"},
        ],
    },
    {
        "id": "google",
        "name": "Google",
        "models": [
            {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
            {"id": "gemini-2.0-flash-lite", "name": "Gemini 2.0 Flash Lite"},
        ],
    },
    {
        "id": "bedrock",
        "name": "AWS Bedrock",
        "models": [
            {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Bedrock)"},
        ],
    },
]

_cached_providers: list[dict[str, Any]] | None = None
_cache_fetched_at: float = 0.0


def _parse_response(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert the models.dev API response into a flat list of provider dicts."""
    providers: list[dict[str, Any]] = []
    for provider_id, entry in data.items():
        if not isinstance(entry, dict):
            continue
        raw_models = entry.get("models", {})
        if not isinstance(raw_models, dict):
            continue
        models = [
            {"id": m.get("id", mid), "name": m.get("name", mid)}
            for mid, m in raw_models.items()
            if isinstance(m, dict)
        ]
        if not models:
            continue
        providers.append(
            {
                "id": provider_id,
                "name": entry.get("name", provider_id),
                "models": models,
            }
        )
    providers.sort(key=lambda p: p["name"].lower())
    return providers


async def fetch_providers(*, force: bool = False) -> list[dict[str, Any]]:
    """Return cached providers, refreshing from models.dev when the TTL has expired.

    On network/parse failure the previous cache (or the static fallback) is returned.
    """
    global _cached_providers, _cache_fetched_at

    now = time.monotonic()
    if not force and _cached_providers is not None and (now - _cache_fetched_at) < _CACHE_TTL:
        return _cached_providers

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(MODELS_DEV_URL)
            resp.raise_for_status()
            data = resp.json()
        providers = _parse_response(data)
        if providers:
            _cached_providers = providers
            _cache_fetched_at = now
            logger.info("models.dev: loaded %d providers", len(providers))
        else:
            logger.warning("models.dev: parsed 0 providers — keeping previous cache")
    except Exception as exc:
        logger.warning("models.dev: fetch failed (%s) — using fallback", exc)

    if _cached_providers is None:
        _cached_providers = _FALLBACK_PROVIDERS

    return _cached_providers
