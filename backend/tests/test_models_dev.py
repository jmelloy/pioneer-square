"""Tests for the models.dev fetch/parse utility."""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from util.models_dev import _FALLBACK_PROVIDERS, _parse_response, fetch_providers


@pytest.fixture(autouse=True)
def reset_cache():
    """Reset module-level cache between tests."""
    import util.models_dev as _mod

    _mod._cached_providers = None
    _mod._cache_fetched_at = 0.0
    yield
    _mod._cached_providers = None
    _mod._cache_fetched_at = 0.0


SAMPLE_RESPONSE = {
    "anthropic": {
        "id": "anthropic",
        "name": "Anthropic",
        "models": {
            "claude-sonnet-4-6": {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
            "claude-opus-4-8": {"id": "claude-opus-4-8", "name": "Claude Opus 4.8"},
        },
    },
    "openai": {
        "id": "openai",
        "name": "OpenAI",
        "models": {
            "gpt-4o": {"id": "gpt-4o", "name": "GPT-4o"},
        },
    },
    "empty-provider": {
        "id": "empty-provider",
        "name": "Empty",
        "models": {},
    },
}


def test_parse_response_basic():
    result = _parse_response(SAMPLE_RESPONSE)
    ids = {p["id"] for p in result}
    assert "anthropic" in ids
    assert "openai" in ids
    # providers with no models are excluded
    assert "empty-provider" not in ids


def test_parse_response_model_fields():
    result = _parse_response(SAMPLE_RESPONSE)
    anthropic = next(p for p in result if p["id"] == "anthropic")
    model_ids = {m["id"] for m in anthropic["models"]}
    assert "claude-sonnet-4-6" in model_ids
    assert "claude-opus-4-8" in model_ids


def test_parse_response_sorted_by_name():
    result = _parse_response(SAMPLE_RESPONSE)
    names = [p["name"] for p in result]
    assert names == sorted(names, key=str.lower)


async def test_fetch_providers_success():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=SAMPLE_RESPONSE)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("util.models_dev.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_providers()

    assert len(result) >= 2
    assert any(p["id"] == "anthropic" for p in result)


async def test_fetch_providers_uses_cache():
    import util.models_dev as _mod

    _mod._cached_providers = [{"id": "cached", "name": "Cached", "models": []}]
    _mod._cache_fetched_at = time.monotonic()

    with patch("util.models_dev.httpx.AsyncClient") as mock_cls:
        result = await fetch_providers()

    # Network should not be called when cache is warm.
    mock_cls.assert_not_called()
    assert result[0]["id"] == "cached"


async def test_fetch_providers_fallback_on_error():
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("network error"))

    with patch("util.models_dev.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_providers()

    assert result is _FALLBACK_PROVIDERS
    assert len(result) > 0
    assert any(p["id"] == "anthropic" for p in result)


async def test_fetch_providers_force_refresh():
    import util.models_dev as _mod

    _mod._cached_providers = [{"id": "stale", "name": "Stale", "models": []}]
    _mod._cache_fetched_at = time.monotonic()

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=SAMPLE_RESPONSE)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("util.models_dev.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_providers(force=True)

    assert any(p["id"] == "anthropic" for p in result)
    assert not any(p["id"] == "stale" for p in result)
