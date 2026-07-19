"""Tests for the Bedrock model ID enricher."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from util.bedrock_enricher import _extract_short_id, build_bedrock_model_map

# ---------------------------------------------------------------------------
# _extract_short_id
# ---------------------------------------------------------------------------


def test_extract_short_id_foundation_model():
    assert _extract_short_id("anthropic.claude-sonnet-4-6-20251001-v1:0") == "claude-sonnet-4-6"


def test_extract_short_id_us_inference_profile():
    assert _extract_short_id("us.anthropic.claude-sonnet-4-6-20251001-v1:0") == "claude-sonnet-4-6"


def test_extract_short_id_eu_inference_profile():
    assert _extract_short_id("eu.anthropic.claude-opus-4-8-20250514-v1:0") == "claude-opus-4-8"


def test_extract_short_id_ap_inference_profile():
    # The date suffix (-20251001) is stripped; the short slug is claude-haiku-4-5
    assert _extract_short_id("ap.anthropic.claude-haiku-4-5-20251001-v1:0") == "claude-haiku-4-5"


def test_extract_short_id_no_match_returns_none():
    # Unrecognised format — no date+version suffix
    assert _extract_short_id("not-a-real-id") is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(foundation_models=None, inference_profiles=None):
    client = MagicMock()
    client.list_foundation_models.return_value = {"modelSummaries": foundation_models or []}
    client.list_inference_profiles.return_value = {
        "inferenceProfileSummaries": inference_profiles or []
    }
    return client


# ---------------------------------------------------------------------------
# build_bedrock_model_map — success path
# ---------------------------------------------------------------------------


def test_build_bedrock_model_map_foundation_models_only():
    client = _make_client(
        foundation_models=[
            {"modelId": "anthropic.claude-sonnet-4-6-20251001-v1:0"},
            {"modelId": "anthropic.claude-opus-4-8-20250514-v1:0"},
        ]
    )
    # boto3 is imported inside the function; patch the cached module attribute.
    with patch("boto3.client", return_value=client):
        result = build_bedrock_model_map()

    assert result["claude-sonnet-4-6"] == "anthropic.claude-sonnet-4-6-20251001-v1:0"
    assert result["claude-opus-4-8"] == "anthropic.claude-opus-4-8-20250514-v1:0"


def test_build_bedrock_model_map_inference_profiles_override_foundation():
    """Cross-region inference profiles (us.*) must override foundation model IDs."""
    client = _make_client(
        foundation_models=[
            {"modelId": "anthropic.claude-sonnet-4-6-20251001-v1:0"},
        ],
        inference_profiles=[
            {"inferenceProfileId": "us.anthropic.claude-sonnet-4-6-20251001-v1:0"},
        ],
    )
    with patch("boto3.client", return_value=client):
        result = build_bedrock_model_map()

    assert result["claude-sonnet-4-6"] == "us.anthropic.claude-sonnet-4-6-20251001-v1:0"


def test_build_bedrock_model_map_non_us_profiles_not_overridden():
    """eu.* profiles are not indexed; only us.* profiles are stored."""
    client = _make_client(
        foundation_models=[
            {"modelId": "anthropic.claude-sonnet-4-6-20251001-v1:0"},
        ],
        inference_profiles=[
            # eu.* should be ignored; us.* should be indexed
            {"inferenceProfileId": "eu.anthropic.claude-sonnet-4-6-20251001-v1:0"},
            {"inferenceProfileId": "us.anthropic.claude-sonnet-4-6-20251001-v1:0"},
        ],
    )
    with patch("boto3.client", return_value=client):
        result = build_bedrock_model_map()

    assert result["claude-sonnet-4-6"] == "us.anthropic.claude-sonnet-4-6-20251001-v1:0"


def test_build_bedrock_model_map_empty_responses():
    client = _make_client()
    with patch("boto3.client", return_value=client):
        result = build_bedrock_model_map()

    assert result == {}


# ---------------------------------------------------------------------------
# build_bedrock_model_map — graceful degradation
# ---------------------------------------------------------------------------


def test_build_bedrock_model_map_no_credentials():
    """Missing AWS credentials must return an empty dict without raising."""
    from botocore.exceptions import NoCredentialsError

    client = MagicMock()
    client.list_foundation_models.side_effect = NoCredentialsError()

    with patch("boto3.client", return_value=client):
        result = build_bedrock_model_map()

    assert result == {}


def test_build_bedrock_model_map_client_creation_fails():
    """Failure to create the boto3 client must return an empty dict."""
    with patch("boto3.client", side_effect=Exception("region not configured")):
        result = build_bedrock_model_map()

    assert result == {}


def test_build_bedrock_model_map_inference_profiles_fail_gracefully():
    """list_inference_profiles error must not prevent returning foundation model IDs."""
    client = _make_client(
        foundation_models=[
            {"modelId": "anthropic.claude-sonnet-4-6-20251001-v1:0"},
        ]
    )
    client.list_inference_profiles.side_effect = Exception("access denied")

    with patch("boto3.client", return_value=client):
        result = build_bedrock_model_map()

    # Falls back to foundation model IDs when inference profiles can't be fetched
    assert result["claude-sonnet-4-6"] == "anthropic.claude-sonnet-4-6-20251001-v1:0"


# ---------------------------------------------------------------------------
# build_bedrock_model_map — AWS_BEARER_TOKEN_BEDROCK (issue #943)
# ---------------------------------------------------------------------------


def test_build_bedrock_model_map_uses_bearer_token(monkeypatch):
    """When AWS_BEARER_TOKEN_BEDROCK is set, the client must be built with the
    bearer signature version and the token injected directly — a plain
    boto3.client("bedrock") does not auto-negotiate bearer auth and would
    otherwise raise NoCredentialsError and log the "no AWS credentials
    configured" warning even though a valid token is configured.
    """
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "tok-abc")

    client = _make_client(
        foundation_models=[
            {"modelId": "anthropic.claude-sonnet-4-6-20251001-v1:0"},
        ]
    )

    with patch("boto3.client", return_value=client) as mock_client_ctor:
        result = build_bedrock_model_map()

    assert result["claude-sonnet-4-6"] == "anthropic.claude-sonnet-4-6-20251001-v1:0"

    _, kwargs = mock_client_ctor.call_args
    assert kwargs["config"].signature_version == "bearer"
    assert client._request_signer._auth_token.token == "tok-abc"


def test_build_bedrock_model_map_boto3_import_error():
    """ImportError for boto3 must return an empty dict."""
    import builtins

    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):
        result = build_bedrock_model_map()

    assert result == {}
