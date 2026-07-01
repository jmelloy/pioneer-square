"""Resolve models.dev short model IDs to AWS Bedrock inference-profile ARNs.

Investigation finding: the Claude CLI on Bedrock does NOT accept the short
model ID from models.dev (e.g. ``claude-sonnet-4-6``).  AWS Bedrock's
InvokeModel API requires either:
  - A foundation model ID:  ``anthropic.claude-sonnet-4-6-20251001-v1:0``
  - A cross-region inference profile ARN: ``us.anthropic.claude-sonnet-4-6-20251001-v1:0``

Cross-region inference profiles (``us.*`` prefix) are preferred because they
route to the nearest available region within the geographic area, giving
higher throughput and availability.

This module fetches both lists from the AWS API and returns a mapping so the
catalog persistence layer can store the resolved ID alongside the short ID.
When AWS credentials are absent the module logs a warning and returns an
empty mapping — startup is never blocked, and workers will see the short ID
as a fallback (which may fail at inference time).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def _extract_short_id(model_id: str) -> str | None:
    """Extract a models.dev-style short ID from a Bedrock model/profile ID.

    Examples::

        "anthropic.claude-sonnet-4-6-20251001-v1:0"    -> "claude-sonnet-4-6"
        "us.anthropic.claude-sonnet-4-6-20251001-v1:0" -> "claude-sonnet-4-6"
        "eu.anthropic.claude-opus-4-8-20250514-v1:0"   -> "claude-opus-4-8"
    """
    stripped = model_id
    # Strip geographic prefix (us., eu., ap.)
    stripped = re.sub(r"^(?:us|eu|ap)\.", "", stripped)
    # Strip provider prefix (e.g. "anthropic.")
    stripped = re.sub(r"^[a-z]+\.", "", stripped)
    # Match <model-slug>-YYYYMMDD-vN:M and extract just the slug.
    m = re.match(r"^(claude[-\w]+?)-\d{8}-v\d+:\d+$", stripped)
    if m:
        return m.group(1)
    return None


def build_bedrock_model_map() -> dict[str, str]:
    """Return ``{short_id: bedrock_arn}`` using live AWS Bedrock API calls.

    Calls ``list_foundation_models`` and ``list_inference_profiles``
    synchronously (boto3 is a sync library).  Cross-region inference profiles
    (``us.*`` prefix) override plain foundation model IDs when both exist for
    the same short ID.

    Returns an empty dict when:
    - boto3 is not installed (should not happen — it's a declared dependency)
    - AWS credentials are absent or insufficient
    - any unexpected error occurs

    All failures are logged as warnings rather than raised.
    """
    try:
        import boto3
        from botocore.exceptions import NoCredentialsError
    except ImportError:
        logger.warning(
            "bedrock_enricher: boto3 not installed — Bedrock model IDs will not be resolved"
        )
        return {}

    try:
        client = boto3.client("bedrock")
    except Exception as exc:
        logger.warning("bedrock_enricher: failed to create Bedrock client (%s) — skipping", exc)
        return {}

    mapping: dict[str, str] = {}

    # Step 1: foundation model IDs (e.g. "anthropic.claude-sonnet-4-6-20251001-v1:0").
    try:
        response = client.list_foundation_models(byProvider="Anthropic")
        for entry in response.get("modelSummaries", []):
            mid = entry.get("modelId", "")
            short = _extract_short_id(mid)
            if short:
                mapping[short] = mid
    except NoCredentialsError:
        logger.warning(
            "bedrock_enricher: no AWS credentials configured — "
            "Bedrock model IDs will not be resolved; workers using the bedrock "
            "provider will fall back to the short model ID and may fail at runtime"
        )
        return {}
    except Exception as exc:
        logger.warning("bedrock_enricher: list_foundation_models failed (%s) — skipping", exc)
        return {}

    # Step 2: cross-region inference profiles override foundation model IDs.
    # These are preferred because they route across regions for higher availability.
    try:
        response = client.list_inference_profiles()
        for profile in response.get("inferenceProfileSummaries", []):
            pid = profile.get("inferenceProfileId", "")
            if pid.startswith("us."):
                short = _extract_short_id(pid)
                if short:
                    mapping[short] = pid
    except Exception as exc:
        logger.warning(
            "bedrock_enricher: list_inference_profiles failed (%s) — "
            "falling back to foundation model IDs",
            exc,
        )

    logger.info("bedrock_enricher: resolved %d model ID(s)", len(mapping))
    return mapping
