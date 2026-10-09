"""BEDROCK_ROLE_ARN for the Bedrock-capable CLIs (claude, pi) a worker launches.

The backend assumes the role in memory (``backend/foreman/bedrock_role.py``), but
the CLIs are separate programs that only understand the AWS SDK's standard
credential chain. So each role gets its own AWS config file, private to the
worker::

    [profile pioneer-bedrock]
    role_arn = <BEDROCK_ROLE_ARN>
    credential_source = EcsContainer
    role_session_name = pioneer-square

and the tool's subprocess env gets ``AWS_CONFIG_FILE`` (that file) and
``AWS_PROFILE=pioneer-bedrock``. The CLI's SDK assumes the role from the task
role and refreshes it itself.

- One file per role ARN (named by its hash), so two roles never share or
  overwrite a profile.
- The directory is chosen by the worker, never by guild env vars, and the file
  is written 0600 with an atomic rename.
- Nothing is set in the worker's own environment: its S3 session-log sync keeps
  the task role.

Precedence matches the backend: a bearer token, access keys or an explicit
AWS_PROFILE for the tool win over the role.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

PROFILE_NAME = "pioneer-bedrock"
_SESSION_NAME = "pioneer-square"
_ROLE_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]{1,512}$")


class BedrockRoleConfigError(ValueError):
    """BEDROCK_ROLE_ARN is set but is not an IAM role ARN."""


def _config_dir() -> Path:
    return Path(tempfile.gettempdir()) / "pioneer-bedrock-roles"


def _profile_text(role_arn: str) -> str:
    return (
        f"[profile {PROFILE_NAME}]\n"
        f"role_arn = {role_arn}\n"
        "credential_source = EcsContainer\n"
        f"role_session_name = {_SESSION_NAME}\n"
    )


def role_config_file(role_arn: str, directory: Path | None = None) -> Path:
    """Return the private config file for *role_arn*, creating it if needed."""
    if not _ROLE_ARN_RE.match(role_arn):
        raise BedrockRoleConfigError(f"BEDROCK_ROLE_ARN is not an IAM role ARN: {role_arn!r}")
    directory = directory or _config_dir()
    path = directory / f"{hashlib.sha256(role_arn.encode()).hexdigest()[:16]}.config"
    text = _profile_text(role_arn)
    if path.exists() and path.read_text() == text:
        return path

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".role-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def apply_bedrock_role(env: dict[str, str], directory: Path | None = None) -> None:
    """Point a tool's subprocess *env* at its role's profile, unless a stronger credential is set."""
    role_arn = (env.get("BEDROCK_ROLE_ARN") or "").strip()
    if not role_arn:
        return
    if (
        env.get("AWS_BEARER_TOKEN_BEDROCK")
        or (env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"))
        or env.get("AWS_PROFILE")
    ):
        return
    env["AWS_CONFIG_FILE"] = str(role_config_file(role_arn, directory))
    env["AWS_PROFILE"] = PROFILE_NAME
