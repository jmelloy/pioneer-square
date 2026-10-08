"""Tests for BEDROCK_ROLE_ARN: the assume-role profile and which Bedrock clients use it."""

from __future__ import annotations

import configparser
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foreman.bedrock_role import (
    PROFILE_NAME,
    BedrockRoleConfigError,
    bedrock_role_profile,
)

ROLE = "arn:aws:iam::123456789012:role/pioneer-square-bedrock"


def _env(tmp_path, **extra):
    return {"AWS_CONFIG_FILE": str(tmp_path / "config"), "BEDROCK_ROLE_ARN": ROLE, **extra}


def _read(tmp_path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(tmp_path / "config")
    return parser


def test_no_role_means_no_profile_and_no_file(tmp_path):
    assert bedrock_role_profile({"AWS_CONFIG_FILE": str(tmp_path / "config")}) is None
    assert not (tmp_path / "config").exists()


def test_writes_assume_role_profile(tmp_path):
    assert bedrock_role_profile(_env(tmp_path)) == PROFILE_NAME
    section = dict(_read(tmp_path).items(f"profile {PROFILE_NAME}"))
    assert section == {
        "role_arn": ROLE,
        "credential_source": "EcsContainer",
        "role_session_name": "pioneer-square",
    }


def test_keeps_other_profiles_and_updates_in_place(tmp_path):
    (tmp_path / "config").write_text("[profile other]\nregion = eu-west-1\n")
    bedrock_role_profile(_env(tmp_path))
    other_role = "arn:aws:iam::210987654321:role/other"
    bedrock_role_profile(_env(tmp_path, BEDROCK_ROLE_ARN=other_role))
    parser = _read(tmp_path)
    assert parser.get("profile other", "region") == "eu-west-1"
    assert parser.get(f"profile {PROFILE_NAME}", "role_arn") == other_role


def test_unchanged_profile_is_not_rewritten(tmp_path):
    bedrock_role_profile(_env(tmp_path))
    before = (tmp_path / "config").stat().st_mtime_ns
    bedrock_role_profile(_env(tmp_path))
    assert (tmp_path / "config").stat().st_mtime_ns == before


def test_credential_source_override(tmp_path):
    bedrock_role_profile(_env(tmp_path, BEDROCK_ROLE_CREDENTIAL_SOURCE="Ec2InstanceMetadata"))
    source = _read(tmp_path).get(f"profile {PROFILE_NAME}", "credential_source")
    assert source == "Ec2InstanceMetadata"


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-arn",
        "arn:aws:iam::123456789012:user/someone",
        ROLE + "\ncredential_process = /bin/sh -c id",
    ],
)
def test_rejects_anything_but_a_role_arn(tmp_path, bad):
    with pytest.raises(BedrockRoleConfigError):
        bedrock_role_profile(_env(tmp_path, BEDROCK_ROLE_ARN=bad))
    assert not (tmp_path / "config").exists()


@pytest.mark.parametrize("source", ["Environment", "credential_process"])
def test_rejects_unsupported_credential_source(tmp_path, source):
    with pytest.raises(BedrockRoleConfigError):
        bedrock_role_profile(_env(tmp_path, BEDROCK_ROLE_CREDENTIAL_SOURCE=source))


class TestForemanClient:
    """make_anthropic_client picks the role profile only as the last resort."""

    def _native_client(self, monkeypatch, tmp_path, **extra):
        from foreman import llm

        for key in ("AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID"):
            monkeypatch.delenv(key, raising=False)
        return llm.make_anthropic_client(
            provider="bedrock",
            model="amazon.nova-micro-v1:0",
            region="us-east-1",
            extra_env=_env(tmp_path, **extra),
        )

    def test_uses_role_profile(self, monkeypatch, tmp_path):
        client = self._native_client(monkeypatch, tmp_path)
        assert client._profile == PROFILE_NAME

    def test_explicit_profile_wins(self, monkeypatch, tmp_path):
        client = self._native_client(monkeypatch, tmp_path, AWS_PROFILE="mine")
        assert client._profile == "mine"


class TestModelCatalog:
    def test_lists_models_through_the_role(self, monkeypatch, tmp_path):
        from util.bedrock_enricher import _make_bedrock_client

        monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        for key, value in _env(tmp_path).items():
            monkeypatch.setenv(key, value)
        boto3_mod = MagicMock()
        _make_bedrock_client(boto3_mod)
        boto3_mod.Session.assert_called_once_with(profile_name=PROFILE_NAME)
        boto3_mod.client.assert_not_called()

    def test_without_role_uses_default_chain(self, monkeypatch):
        from util.bedrock_enricher import _make_bedrock_client

        for key in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE", "BEDROCK_ROLE_ARN"):
            monkeypatch.delenv(key, raising=False)
        boto3_mod = MagicMock()
        _make_bedrock_client(boto3_mod)
        boto3_mod.client.assert_called_once_with("bedrock")
