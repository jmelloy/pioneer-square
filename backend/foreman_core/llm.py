"""Anthropic client factory supporting both the direct API and Amazon Bedrock.

Both the embedded foreman (backend) and standalone foreman import this to build
their own module-level singleton — the factory is shared, the instance is not.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

try:
    import anthropic as _anthropic_mod

    HAS_ANTHROPIC = True
except ImportError:
    _anthropic_mod = None  # type: ignore[assignment]
    HAS_ANTHROPIC = False

logger = logging.getLogger(__name__)


class BedrockModelNotConfiguredError(ValueError):
    """Raised when provider=bedrock is selected but no model/inference-profile is configured.

    Bedrock has no universal default model — inference profile ARNs are scoped to a
    single AWS account — so there is no safe value to fall back to silently. Callers
    must configure one explicitly (guild settings `model` field or FOREMAN_BEDROCK_MODEL).
    """


FOREMAN_MODEL = os.environ.get("FOREMAN_MODEL", "claude-sonnet-4-6")
# Bedrock uses cross-region inference profiles, not plain model IDs, and those
# profiles are scoped to a single AWS account, so there is no valid cross-account
# default. Set this to the profile ARN/ID appropriate for your account/region, e.g.:
#   arn:aws:bedrock:us-east-1:<account-id>:inference-profile/us.anthropic.claude-sonnet-4-6
FOREMAN_BEDROCK_MODEL = os.environ.get("FOREMAN_BEDROCK_MODEL")

# Set FOREMAN_PROVIDER=bedrock to use Amazon Bedrock instead of the Anthropic API.
# Requires: pip install "anthropic[bedrock]"  +  AWS credentials in env / IAM role.
FOREMAN_PROVIDER = os.environ.get("FOREMAN_PROVIDER", "anthropic").lower()
_BEDROCK_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def get_foreman_model(provider: str | None = None) -> str:
    """Return the model ID to use for the given provider.

    When provider is 'bedrock' (or FOREMAN_PROVIDER=bedrock), returns
    FOREMAN_BEDROCK_MODEL, raising BedrockModelNotConfiguredError if it's unset —
    Bedrock inference-profile ARNs are AWS-account-scoped, so there is no safe
    default to fall back to. Otherwise returns FOREMAN_MODEL.

    Reads os.environ on every call so that tests can patch env vars directly.
    """
    resolved = (provider or os.environ.get("FOREMAN_PROVIDER", "anthropic")).lower()
    if resolved == "bedrock":
        bedrock_model = os.environ.get("FOREMAN_BEDROCK_MODEL")
        if not bedrock_model:
            raise BedrockModelNotConfiguredError(
                "Bedrock provider selected but no model is configured. Set a model "
                "(inference-profile ARN or model ID) in the guild's foreman settings, "
                "or set the FOREMAN_BEDROCK_MODEL environment variable."
            )
        return bedrock_model
    return os.environ.get("FOREMAN_MODEL", "claude-sonnet-4-6")


def _log_bedrock_credentials(
    region: str | None,
    profile: str | None,
    *,
    access_key: str | None = None,
    secret_key: str | None = None,
    session_token: str | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Probe boto3's credential chain and log what it resolves (or why it can't).

    The anthropic SDK builds a ``boto3.Session`` and raises the opaque
    ``"could not resolve credentials from session"`` when
    ``session.get_credentials()`` returns ``None``. We replicate that session
    here at client-creation time so the *reason* (no creds, wrong profile,
    expired SSO token, missing region) shows up in the logs instead.

    ``env`` is the effective credential source (guild env_vars overlaid on
    os.environ); explicit access/secret keys, when given, mirror what we pass
    to the SDK so the probe reflects the real resolution.
    """
    env = env if env is not None else os.environ
    # Surface the relevant env vars regardless of whether boto3 imports.
    env_summary = {
        "AWS_PROFILE": env.get("AWS_PROFILE"),
        "AWS_BEARER_TOKEN_BEDROCK": "set" if env.get("AWS_BEARER_TOKEN_BEDROCK") else None,
        "AWS_DEFAULT_REGION": env.get("AWS_DEFAULT_REGION"),
        "AWS_REGION": env.get("AWS_REGION"),
        "AWS_ACCESS_KEY_ID": "set" if (access_key or env.get("AWS_ACCESS_KEY_ID")) else None,
        "AWS_SECRET_ACCESS_KEY": "set"
        if (secret_key or env.get("AWS_SECRET_ACCESS_KEY"))
        else None,
        "AWS_SESSION_TOKEN": "set" if (session_token or env.get("AWS_SESSION_TOKEN")) else None,
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": env.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"),
        "AWS_WEB_IDENTITY_TOKEN_FILE": env.get("AWS_WEB_IDENTITY_TOKEN_FILE"),
    }
    logger.info(
        "Bedrock credential probe: requested region=%s profile=%s explicit_keys=%s; env=%s",
        region,
        profile,
        bool(access_key and secret_key),
        {k: v for k, v in env_summary.items() if v is not None},
    )

    try:
        import boto3  # noqa: PLC0415  (lazy: only needed for the bedrock path)
    except ImportError:
        logger.warning(
            "Bedrock credential probe: boto3 not importable; install "
            "anthropic[bedrock]. Falling back to SDK credential resolution."
        )
        return

    try:
        session = boto3.Session(
            profile_name=profile,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )
    except Exception as exc:  # e.g. ProfileNotFound
        logger.error(
            "Bedrock credential probe: failed to build boto3 session "
            "(profile=%s region=%s): %s: %s",
            profile,
            region,
            type(exc).__name__,
            exc,
        )
        return

    if not session.region_name:
        logger.warning(
            "Bedrock credential probe: no region resolved (pass region or set "
            "AWS_DEFAULT_REGION); SigV4 signing will fail without one."
        )

    try:
        creds = session.get_credentials()
    except Exception as exc:  # e.g. SSO token expired, unable to load credentials
        logger.error(
            "Bedrock credential probe: get_credentials() raised %s: %s "
            "(this is the cause of 'could not resolve credentials from session')",
            type(exc).__name__,
            exc,
        )
        return

    if creds is None:
        logger.error(
            "Bedrock credential probe: boto3 resolved NO credentials. This is "
            "exactly what makes the anthropic SDK raise 'could not resolve "
            "credentials from session'. Checked profile=%s. Provide credentials "
            "via AWS_PROFILE, ~/.aws/credentials, env keys, or an IAM role. "
            "(When configuring via the guild settings dialogue, AWS_* env vars "
            "must reach the foreman process, not just spawned workers.)",
            profile or env.get("AWS_PROFILE") or "default",
        )
        return

    method = getattr(creds, "method", "unknown")
    frozen = creds.get_frozen_credentials()
    logger.info(
        "Bedrock credential probe: resolved credentials via method=%s "
        "(access_key=…%s, session_token=%s, region=%s)",
        method,
        (frozen.access_key or "")[-4:],
        "present" if frozen.token else "none",
        session.region_name,
    )


def make_anthropic_client(
    provider: str | None = None,
    api_key: str | None = None,
    region: str | None = None,
    aws_profile: str | None = None,
    extra_env: Mapping[str, str] | None = None,
    model: str | None = None,
):
    """Create and return an Anthropic async client.

    provider:    'anthropic' (default, reads FOREMAN_PROVIDER env) or 'bedrock'.
    api_key:     API key for direct Anthropic API (ignored for Bedrock).
    region:      AWS region for Bedrock (defaults to AWS_DEFAULT_REGION or 'us-east-1').
    aws_profile: Named AWS profile for Bedrock (defaults to AWS_PROFILE env).
    extra_env:   Guild-configured env vars (settings dialogue) overlaid on
                 os.environ for credential resolution — AWS_* for Bedrock,
                 ANTHROPIC_* for the direct API. These do NOT reach the foreman
                 process otherwise: the dialogue's env_vars are only injected
                 into spawned workers, never this process.
    model:       Resolved model/inference-profile for Bedrock (e.g. the guild's
                 configured model), checked against FOREMAN_BEDROCK_MODEL when
                 absent. Ignored for the direct Anthropic API. Callers that
                 build a client before resolving the model (see get_foreman_model)
                 would otherwise only discover a missing Bedrock model deep
                 inside the first API call; passing it here fails fast instead.
    """
    if not HAS_ANTHROPIC or _anthropic_mod is None:
        raise ImportError("anthropic package is not installed")

    # Read env dynamically (same as get_foreman_model) so patching os.environ in
    # tests works and a late-set FOREMAN_PROVIDER env var is picked up correctly.
    resolved_provider = (provider or os.environ.get("FOREMAN_PROVIDER", "anthropic")).lower()
    # Effective credential source: guild-supplied env vars take precedence over
    # the foreman process env (guild settings are the more specific config).
    # Explicit args still win over both.
    env: Mapping[str, str] = {**os.environ, **(extra_env or {})}

    if resolved_provider == "bedrock":
        # Fail fast: a Bedrock client with no model would only surface this
        # deep inside the first API call. Check it here, before the client
        # (and any of its credential resolution) is even built — consistent
        # with the same check in get_foreman_model().
        resolved_model = model or env.get("FOREMAN_BEDROCK_MODEL")
        if not resolved_model:
            raise BedrockModelNotConfiguredError(
                "Bedrock provider selected but no model is configured. Set a model "
                "(inference-profile ARN or model ID) in the guild's foreman settings, "
                "or set the FOREMAN_BEDROCK_MODEL environment variable."
            )
        resolved_region = (
            region or env.get("AWS_DEFAULT_REGION") or env.get("AWS_REGION") or _BEDROCK_REGION
        )
        resolved_profile = aws_profile or env.get("AWS_PROFILE") or None
        access_key = env.get("AWS_ACCESS_KEY_ID")
        secret_key = env.get("AWS_SECRET_ACCESS_KEY")
        session_token = env.get("AWS_SESSION_TOKEN")
        # Bearer-token auth (AWS_BEARER_TOKEN_BEDROCK) bypasses SigV4/boto3
        # entirely. The SDK reads it into `api_key` and *raises* if you also
        # pass any AWS credential (aws_profile/keys), so when a token is present
        # we must not pass any AWS credential and we skip the boto3 probe.
        bearer_token = env.get("AWS_BEARER_TOKEN_BEDROCK")
        logger.info(
            "Foreman using Amazon Bedrock (region=%s, profile=%s, auth=%s, model=%s)",
            resolved_region,
            resolved_profile,
            "bearer-token"
            if bearer_token
            else ("explicit-keys" if (access_key and secret_key) else "sigv4"),
            resolved_model,
        )
        bedrock_kwargs: dict = {"aws_region": resolved_region}
        if bearer_token:
            logger.info(
                "Bedrock credential probe: AWS_BEARER_TOKEN_BEDROCK is set "
                "(len=%d); using bearer-token auth, skipping boto3 SigV4 "
                "resolution.",
                len(bearer_token),
            )
            bedrock_kwargs["api_key"] = bearer_token
        else:
            # Probe up front so a missing/expired credential shows up in the
            # logs with a clear reason rather than the SDK's opaque
            # RuntimeError later.
            _log_bedrock_credentials(
                resolved_region,
                resolved_profile,
                access_key=access_key,
                secret_key=secret_key,
                session_token=session_token,
                env=env,
            )
            # Pass credentials explicitly so resolution is deterministic and
            # matches what the probe validated. Only safe when there's no
            # bearer token. Explicit keys (from guild env_vars) take priority
            # over a profile, mirroring boto3's own precedence.
            if access_key and secret_key:
                bedrock_kwargs["aws_access_key"] = access_key
                bedrock_kwargs["aws_secret_key"] = secret_key
                if session_token:
                    bedrock_kwargs["aws_session_token"] = session_token
            elif resolved_profile:
                bedrock_kwargs["aws_profile"] = resolved_profile
        return _anthropic_mod.AsyncAnthropicBedrock(**bedrock_kwargs)

    # Direct Anthropic API. The SDK reads ANTHROPIC_* from os.environ on its own,
    # but guild-configured env_vars never reach this process, so resolve them
    # from the merged `env` and pass explicitly.
    # api_key and auth_token are mutually exclusive; auth_token takes precedence
    # so a guild-configured ANTHROPIC_AUTH_TOKEN overrides a process-level
    # ANTHROPIC_API_KEY rather than causing an SDK ValueError.
    kwargs: dict = {}
    if env.get("ANTHROPIC_AUTH_TOKEN"):
        kwargs["auth_token"] = env["ANTHROPIC_AUTH_TOKEN"]
    else:
        resolved_api_key = api_key or env.get("ANTHROPIC_API_KEY")
        if resolved_api_key:
            kwargs["api_key"] = resolved_api_key
    if env.get("ANTHROPIC_BASE_URL"):
        kwargs["base_url"] = env["ANTHROPIC_BASE_URL"]
    logger.info(
        "Foreman using Anthropic API (auth=%s, base_url=%s)",
        "auth-token"
        if kwargs.get("auth_token")
        else ("api-key" if kwargs.get("api_key") else "default"),
        kwargs.get("base_url", "default"),
    )
    return _anthropic_mod.AsyncAnthropic(**kwargs)
