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
# Bedrock uses cross-region inference profiles, not plain model IDs.
# Set this to the profile ARN/ID appropriate for your region, e.g.:
#   us.anthropic.claude-sonnet-4-6-20250514-v1:0
FOREMAN_BEDROCK_MODEL = os.environ.get(
    "FOREMAN_BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-6-20250514-v1:0"
)

# Set FOREMAN_PROVIDER=bedrock to use Amazon Bedrock instead of the Anthropic API.
# Requires: pip install "anthropic[bedrock]"  +  AWS credentials in env / IAM role.
FOREMAN_PROVIDER = os.environ.get("FOREMAN_PROVIDER", "anthropic").lower()
_BEDROCK_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def get_foreman_model(provider: str | None = None) -> str:
    """Return the model ID to use for the given provider.

    When provider is 'bedrock' (or FOREMAN_PROVIDER=bedrock), returns
    FOREMAN_BEDROCK_MODEL; otherwise returns FOREMAN_MODEL.
    """
    resolved = (provider or FOREMAN_PROVIDER).lower()
    return FOREMAN_BEDROCK_MODEL if resolved == "bedrock" else FOREMAN_MODEL


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
            get_foreman_model("bedrock"),
        )
        return client

    kwargs: dict = {}
    if api_key:
        kwargs["api_key"] = api_key
    return _anthropic_mod.AsyncAnthropic(**kwargs)
