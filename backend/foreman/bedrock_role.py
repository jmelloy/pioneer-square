"""Cross-account Bedrock access by assuming an IAM role instead of an API key.

When ``BEDROCK_ROLE_ARN`` is set, Bedrock calls authenticate as that role: this
module writes a named profile into the AWS shared config file::

    [profile pioneer-bedrock]
    role_arn = <BEDROCK_ROLE_ARN>
    credential_source = EcsContainer
    role_session_name = pioneer-square

and every Bedrock caller passes that profile name to its SDK. The SDK then
assumes the role from the container's own credentials (the ECS task role) and
refreshes the session before it expires, so there is no long-lived key to
rotate. Only Bedrock clients use the profile; S3, ECS and everything else keep
the default credential chain.

Precedence is unchanged: an explicit ``AWS_PROFILE``, explicit access keys, or
``AWS_BEARER_TOKEN_BEDROCK`` still win, so remove the bearer token to switch.

``BEDROCK_ROLE_CREDENTIAL_SOURCE`` overrides where the base credentials come
from: ``EcsContainer`` (default) or ``Ec2InstanceMetadata``. Static access keys
are not a source on purpose: AWS_ACCESS_KEY_ID in the environment outranks any
profile, both in the AWS SDKs and in this app, so with keys set there is
nothing for the role to do.

Kept in sync with ``worker/pioneer_worker/bedrock_role.py``.
"""

from __future__ import annotations

import configparser
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

PROFILE_NAME = "pioneer-bedrock"
_SESSION_NAME = "pioneer-square"
_CREDENTIAL_SOURCES = ("EcsContainer", "Ec2InstanceMetadata")
# The value is written into an INI file, so it must not be able to smuggle in
# a newline or another key: accept only a well-formed role ARN.
_ROLE_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]{1,512}$")


class BedrockRoleConfigError(ValueError):
    """BEDROCK_ROLE_ARN or BEDROCK_ROLE_CREDENTIAL_SOURCE is malformed."""


def _config_path(env: Mapping[str, str]) -> Path:
    configured = env.get("AWS_CONFIG_FILE") or os.environ.get("AWS_CONFIG_FILE")
    return Path(configured).expanduser() if configured else Path.home() / ".aws" / "config"


def ensure_profile(role_arn: str, credential_source: str, path: Path) -> None:
    """Create or update the ``pioneer-bedrock`` profile in *path*; other profiles are kept."""
    if not _ROLE_ARN_RE.match(role_arn):
        raise BedrockRoleConfigError(f"BEDROCK_ROLE_ARN is not an IAM role ARN: {role_arn!r}")
    if credential_source not in _CREDENTIAL_SOURCES:
        raise BedrockRoleConfigError(
            f"BEDROCK_ROLE_CREDENTIAL_SOURCE must be one of {', '.join(_CREDENTIAL_SOURCES)}"
        )

    wanted = {
        "role_arn": role_arn,
        "credential_source": credential_source,
        "role_session_name": _SESSION_NAME,
    }
    section = f"profile {PROFILE_NAME}"
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    if parser.has_section(section) and dict(parser.items(section)) == wanted:
        return
    if parser.has_section(section):
        parser.remove_section(section)
    parser.add_section(section)
    for key, value in wanted.items():
        parser.set(section, key, value)

    # Write-then-rename so a concurrent reader never sees a half-written file.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        with os.fdopen(fd, "w") as fh:
            parser.write(fh)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def bedrock_role_profile(env: Mapping[str, str] | None = None) -> str | None:
    """Return the profile name Bedrock clients should use, or None when no role is configured.

    *env* is the effective environment (guild env vars overlaid on os.environ).
    """
    env = env if env is not None else os.environ
    role_arn = (env.get("BEDROCK_ROLE_ARN") or "").strip()
    if not role_arn:
        return None
    source = (env.get("BEDROCK_ROLE_CREDENTIAL_SOURCE") or "EcsContainer").strip()
    ensure_profile(role_arn, source, _config_path(env))
    return PROFILE_NAME
