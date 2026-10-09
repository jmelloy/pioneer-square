"""Tests for BEDROCK_ROLE_ARN: in-memory assumed-role credentials, per guild."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foreman import bedrock_role
from foreman.bedrock_role import BedrockRoleConfigError, bedrock_role_arn, uses_role
from foreman.providers import bedrock as bedrock_provider

ROLE_A = "arn:aws:iam::123456789012:role/guild-a-bedrock"
ROLE_B = "arn:aws:iam::210987654321:role/guild-b-bedrock"


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch, tmp_path):
    for key in ("AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID"):
        monkeypatch.delenv(key, raising=False)
    # Never read the developer's own AWS config or credentials.
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    bedrock_role._sessions.clear()
    bedrock_provider._clients.clear()
    yield
    bedrock_role._sessions.clear()
    bedrock_provider._clients.clear()


def _fake_session(name: str):
    session = MagicMock(name=name)
    session.client.side_effect = lambda service, **_: SimpleNamespace(session=name, service=service)
    return session


class TestRoleArn:
    def test_unset_means_no_role(self):
        assert bedrock_role_arn({}) is None

    @pytest.mark.parametrize(
        "bad",
        [
            "not-an-arn",
            "arn:aws:iam::123456789012:user/someone",
            ROLE_A + "\ncredential_process = /bin/sh -c id",
        ],
    )
    def test_rejects_anything_but_a_role_arn(self, bad):
        with pytest.raises(BedrockRoleConfigError):
            bedrock_role_arn({"BEDROCK_ROLE_ARN": bad})

    @pytest.mark.parametrize(
        ("extra", "profile"),
        [
            ({"AWS_BEARER_TOKEN_BEDROCK": "tok"}, None),
            ({"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "s"}, None),
            ({}, "explicit-profile"),
        ],
    )
    def test_stronger_credentials_win(self, extra, profile):
        assert uses_role({"BEDROCK_ROLE_ARN": ROLE_A, **extra}, profile) is None

    def test_role_is_the_fallback(self):
        assert uses_role({"BEDROCK_ROLE_ARN": ROLE_A}, None) == ROLE_A


class TestSessions:
    def test_cached_per_role_and_region(self):
        with patch.object(bedrock_role, "_new_role_session", side_effect=lambda r, g: (r, g)):
            assert bedrock_role.role_session(ROLE_A, "us-east-1") == (ROLE_A, "us-east-1")
            assert bedrock_role.role_session(ROLE_B, "us-east-1") == (ROLE_B, "us-east-1")
            assert bedrock_role.role_session(ROLE_A, "us-west-2") == (ROLE_A, "us-west-2")
            assert bedrock_role._new_role_session.call_count == 3
            bedrock_role.role_session(ROLE_A, "us-east-1")
            assert bedrock_role._new_role_session.call_count == 3

    def test_real_session_assumes_the_role_lazily(self, monkeypatch, tmp_path):
        # Source credentials from env; nothing is fetched until a request signs.
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.setenv("HOME", str(tmp_path))
        session = bedrock_role._new_role_session(ROLE_A, "us-east-1")
        creds = session.get_credentials()
        assert creds.method == "assume-role"
        assert session.region_name == "us-east-1"
        assert list(tmp_path.iterdir()) == []  # nothing written to disk

    def test_no_source_credentials_is_a_clear_error(self, monkeypatch):
        fake = MagicMock()
        fake.get_credentials.return_value = None
        with patch("botocore.session.get_session", return_value=fake):
            with pytest.raises(BedrockRoleConfigError, match="no AWS credentials"):
                bedrock_role._new_role_session(ROLE_A, "us-east-1")


class TestConverseClient:
    def test_two_guilds_two_roles_two_clients(self):
        sessions = {ROLE_A: _fake_session("A"), ROLE_B: _fake_session("B")}
        with patch.object(bedrock_role, "role_session", side_effect=lambda r, g: sessions[r]):
            a = bedrock_provider._get_client("us-east-1", None, {"BEDROCK_ROLE_ARN": ROLE_A})
            b = bedrock_provider._get_client("us-east-1", None, {"BEDROCK_ROLE_ARN": ROLE_B})
            a_again = bedrock_provider._get_client("us-east-1", None, {"BEDROCK_ROLE_ARN": ROLE_A})
        assert (a.session, b.session) == ("A", "B")
        assert a_again is a

    def test_bearer_token_beats_role(self):
        with patch.object(bedrock_role, "role_session") as role_session:
            bedrock_provider._get_client(
                "us-east-1",
                None,
                {"BEDROCK_ROLE_ARN": ROLE_A, "AWS_BEARER_TOKEN_BEDROCK": "tok"},
            )
        role_session.assert_not_called()


class TestAnthropicClient:
    def test_make_anthropic_client_uses_the_guild_role(self):
        from foreman import llm

        with patch.object(bedrock_role, "make_role_anthropic_bedrock") as make:
            client = llm.make_anthropic_client(
                provider="bedrock",
                model="us.anthropic.claude-sonnet-4-6",
                region="us-east-1",
                extra_env={"BEDROCK_ROLE_ARN": ROLE_B},
            )
        make.assert_called_once_with(role_arn=ROLE_B, region="us-east-1")
        assert client is make.return_value

    async def test_each_request_signs_with_current_credentials(self):
        frozen = iter(
            [
                SimpleNamespace(access_key="K1", secret_key="S1", token="T1"),
                SimpleNamespace(access_key="K2", secret_key="S2", token="T2"),
            ]
        )
        session = MagicMock()
        session.get_credentials.return_value.get_frozen_credentials.side_effect = lambda: next(
            frozen
        )
        with patch.object(bedrock_role, "role_session", return_value=session):
            client = bedrock_role.make_role_anthropic_bedrock(role_arn=ROLE_A, region="us-east-1")
        session.get_credentials.assert_not_called()  # no STS call at construction

        import anthropic

        with patch.object(
            anthropic.AsyncAnthropicBedrock, "_prepare_request", new=AsyncMock()
        ) as signed:
            await client._prepare_request(MagicMock())
            assert client.aws_access_key == "K1"
            await client._prepare_request(MagicMock())
        assert signed.await_count == 2
        assert (client.aws_access_key, client.aws_secret_key, client.aws_session_token) == (
            "K2",
            "S2",
            "T2",
        )


class TestModelCatalog:
    def test_lists_models_through_the_deployment_role(self, monkeypatch):
        from util.bedrock_enricher import _make_bedrock_client

        monkeypatch.setenv("BEDROCK_ROLE_ARN", ROLE_A)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
        session = _fake_session("A")
        with patch.object(bedrock_role, "role_session", return_value=session) as role_session:
            client = _make_bedrock_client(MagicMock())
        role_session.assert_called_once_with(ROLE_A, "us-west-2")
        assert client.service == "bedrock"

    def test_without_role_uses_default_chain(self, monkeypatch):
        from util.bedrock_enricher import _make_bedrock_client

        monkeypatch.delenv("BEDROCK_ROLE_ARN", raising=False)
        boto3_mod = MagicMock()
        _make_bedrock_client(boto3_mod)
        boto3_mod.client.assert_called_once_with("bedrock")
