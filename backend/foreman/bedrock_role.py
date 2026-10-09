"""Cross-account Bedrock access by assuming an IAM role instead of an API key.

``BEDROCK_ROLE_ARN`` (deployment env, or a guild's env vars) names a role in the
account that owns the Bedrock models. Bedrock clients then sign with that role's
temporary credentials, assumed from this process's own credentials (the ECS task
role) and refreshed in memory before they expire. Nothing is written to disk and
there is no long-lived key to rotate.

Everything is keyed on the role ARN: two guilds on two roles get two sessions
and two clients, never each other's. Only Bedrock clients use these sessions;
S3, ECS and everything else keep the default credential chain.

Precedence, highest first, matching boto3's own: ``AWS_BEARER_TOKEN_BEDROCK``,
explicit access keys, an explicit ``AWS_PROFILE``, then ``BEDROCK_ROLE_ARN``.

The worker's CLIs (claude, pi) are separate programs and cannot take in-memory
credentials; see ``worker/pioneer_worker/bedrock_role.py`` for their side.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from typing import Any

_SESSION_NAME = "pioneer-square"
_ROLE_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]{1,512}$")

_sessions: dict[tuple[str, str], Any] = {}
_sessions_lock = threading.Lock()


class BedrockRoleConfigError(ValueError):
    """BEDROCK_ROLE_ARN is set but is not an IAM role ARN."""


def bedrock_role_arn(env: Mapping[str, str]) -> str | None:
    """Return the validated role ARN from *env*, or None when no role is configured."""
    role_arn = (env.get("BEDROCK_ROLE_ARN") or "").strip()
    if not role_arn:
        return None
    if not _ROLE_ARN_RE.match(role_arn):
        raise BedrockRoleConfigError(f"BEDROCK_ROLE_ARN is not an IAM role ARN: {role_arn!r}")
    return role_arn


def uses_role(env: Mapping[str, str], profile: str | None) -> str | None:
    """Return the role ARN to use for *env*, or None when a higher-precedence credential is set."""
    if env.get("AWS_BEARER_TOKEN_BEDROCK"):
        return None
    if env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
        return None
    if profile:
        return None
    return bedrock_role_arn(env)


def role_session(role_arn: str, region: str):
    """Return a boto3 Session whose credentials assume *role_arn* and refresh themselves.

    Cached per (role, region). The source credentials are this process's default
    chain, never a guild's env vars.
    """
    key = (role_arn, region)
    with _sessions_lock:
        session = _sessions.get(key)
        if session is None:
            session = _new_role_session(role_arn, region)
            _sessions[key] = session
        return session


def _new_role_session(role_arn: str, region: str):
    import boto3
    import botocore.session
    from botocore.credentials import AssumeRoleCredentialFetcher, DeferredRefreshableCredentials

    source = botocore.session.get_session()
    source.set_config_variable("region", region)
    source_credentials = source.get_credentials()
    if source_credentials is None:
        raise BedrockRoleConfigError(
            "BEDROCK_ROLE_ARN is set but this process has no AWS credentials to assume it from"
        )
    fetcher = AssumeRoleCredentialFetcher(
        client_creator=source.create_client,
        source_credentials=source_credentials,
        role_arn=role_arn,
        extra_args={"RoleSessionName": _SESSION_NAME},
    )
    target = botocore.session.get_session()
    target.set_config_variable("region", region)
    target._credentials = DeferredRefreshableCredentials(
        method="assume-role", refresh_using=fetcher.fetch_credentials
    )
    return boto3.Session(botocore_session=target, region_name=region)


def make_role_anthropic_bedrock(*, role_arn: str, region: str):
    """Return an AsyncAnthropicBedrock that signs every request with the role's current credentials.

    The SDK only accepts static keys or a profile name, so the subclass copies the
    session's current (refreshed when due) credentials onto itself before the SDK
    signs each request.
    """
    from anthropic import AsyncAnthropicBedrock

    session = role_session(role_arn, region)

    class _RoleAsyncAnthropicBedrock(AsyncAnthropicBedrock):
        async def _prepare_request(self, request):  # type: ignore[override]
            creds = session.get_credentials().get_frozen_credentials()
            self.aws_access_key = creds.access_key
            self.aws_secret_key = creds.secret_key
            self.aws_session_token = creds.token
            await super()._prepare_request(request)

    # No credentials at construction: the first request fetches them, so building
    # the client never blocks the event loop on an STS call.
    return _RoleAsyncAnthropicBedrock(aws_region=region)
