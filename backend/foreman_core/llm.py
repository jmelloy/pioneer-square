"""Anthropic client factory supporting both the direct API and Amazon Bedrock.

Both the embedded foreman (backend) and standalone foreman import this to build
their own module-level singleton — the factory is shared, the instance is not.
"""

from __future__ import annotations

import logging
import os

try:
    import anthropic as _anthropic_mod

    HAS_ANTHROPIC = True
except ImportError:
    _anthropic_mod = None  # type: ignore[assignment]
    HAS_ANTHROPIC = False

logger = logging.getLogger(__name__)

FOREMAN_MODEL = os.environ.get("FOREMAN_MODEL", "claude-sonnet-4-6")

# Set FOREMAN_PROVIDER=bedrock to use Amazon Bedrock instead of the Anthropic API.
# Requires: pip install "anthropic[bedrock]"  +  AWS credentials in env / IAM role.
# On Bedrock, model IDs use the "anthropic." prefix, e.g.:
#   anthropic.claude-sonnet-4-5   (Sonnet 4.x on Bedrock)
#   anthropic.claude-opus-4-5     (Opus 4.x on Bedrock)
# Check your Bedrock console for exact IDs available in your region.
FOREMAN_PROVIDER = os.environ.get("FOREMAN_PROVIDER", "anthropic").lower()
_BEDROCK_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def make_anthropic_client(
    provider: str | None = None,
    api_key: str | None = None,
    region: str | None = None,
):
    """Create and return an Anthropic async client.

    provider: 'anthropic' (default, reads FOREMAN_PROVIDER env) or 'bedrock'.
    api_key:  API key for direct Anthropic API (ignored for Bedrock).
    region:   AWS region for Bedrock (defaults to AWS_DEFAULT_REGION or 'us-east-1').
    """
    if not HAS_ANTHROPIC or _anthropic_mod is None:
        raise ImportError("anthropic package is not installed")

    resolved_provider = (provider or FOREMAN_PROVIDER).lower()
    resolved_region = region or _BEDROCK_REGION

    if resolved_provider == "bedrock":
        client = _anthropic_mod.AsyncAnthropicBedrock(aws_region=resolved_region)
        logger.info(
            "Foreman using Amazon Bedrock (region=%s, model=%s)",
            resolved_region,
            FOREMAN_MODEL,
        )
        return client

    kwargs: dict = {}
    if api_key:
        kwargs["api_key"] = api_key
    return _anthropic_mod.AsyncAnthropic(**kwargs)
