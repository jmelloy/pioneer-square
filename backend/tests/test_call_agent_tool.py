"""Unit tests for the call_agent foreman tool.

All external calls (_fetch_agent_card, _post_agent_task, DB) are mocked —
no real network usage.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from _test_config import TEST_DATABASE_URL  # noqa: E402
from foreman.tools import exec_tools
from helpers import create_db, insert_guild, truncate_all

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(monkeypatch):
    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)
    db_url = TEST_DATABASE_URL
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)
    yield db_url


def _fake_tu(name: str, inputs: dict, tool_id: str = "tool-agent1") -> SimpleNamespace:
    return SimpleNamespace(name=name, input=inputs, id=tool_id)


def _mock_agent_card(
    name: str = "Test Agent",
    skill_ids: list[str] | None = None,
) -> dict:
    if skill_ids is None:
        skill_ids = ["verify_identity_record", "lookup_dnsid"]
    return {
        "name": name,
        "description": "A test A2A agent",
        "url": "http://localhost:8080",
        "version": "1.0.0",
        "skills": [{"id": sid, "name": sid.replace("_", " ").title()} for sid in skill_ids],
    }


def _mock_agent_response(result_data: dict | None = None) -> dict:
    return result_data or {"status": "completed", "output": "ok"}


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestCallAgentValidation:
    @pytest.mark.asyncio
    async def test_missing_agent_url_returns_error(self, db_session):
        insert_guild(db_session, "g-ca-nourl")
        results = await exec_tools(
            "g-ca-nourl",
            [_fake_tu("call_agent", {"skill": "verify_identity_record"})],
        )
        assert results[0].get("is_error") is True
        assert "agent_url" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_missing_skill_returns_error(self, db_session):
        insert_guild(db_session, "g-ca-noskill")
        results = await exec_tools(
            "g-ca-noskill",
            [_fake_tu("call_agent", {"agent_url": "http://localhost:8080"})],
        )
        assert results[0].get("is_error") is True
        assert "skill" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_unknown_skill_returns_error_with_available_list(self, db_session):
        insert_guild(db_session, "g-ca-badskill")
        with patch(
            "foreman.tools._fetch_agent_card",
            return_value=_mock_agent_card(skill_ids=["lookup_dnsid"]),
        ):
            results = await exec_tools(
                "g-ca-badskill",
                [
                    _fake_tu(
                        "call_agent",
                        {"agent_url": "http://localhost:8080", "skill": "nonexistent_skill"},
                    )
                ],
            )
        assert results[0].get("is_error") is True
        assert "nonexistent_skill" in results[0]["content"]
        assert "lookup_dnsid" in results[0]["content"]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestCallAgentHappyPath:
    @pytest.mark.asyncio
    async def test_successful_skill_call_returns_structured_result(self, db_session):
        insert_guild(db_session, "g-ca-ok")
        card = _mock_agent_card(name="dnsid-go", skill_ids=["verify_identity_record"])
        response = {"status": "verified", "record": "example.com"}

        with (
            patch("foreman.tools._fetch_agent_card", return_value=card),
            patch("foreman.tools._post_agent_task", return_value=response),
        ):
            results = await exec_tools(
                "g-ca-ok",
                [
                    _fake_tu(
                        "call_agent",
                        {
                            "agent_url": "http://localhost:8080",
                            "skill": "verify_identity_record",
                            "params": {"domain": "example.com"},
                        },
                    )
                ],
            )

        assert results[0].get("is_error") is not True
        parsed = json.loads(results[0]["content"])
        assert parsed["agent_url"] == "http://localhost:8080"
        assert parsed["skill"] == "verify_identity_record"
        assert parsed["agent_name"] == "dnsid-go"
        assert parsed["response"] == response

    @pytest.mark.asyncio
    async def test_trailing_slash_stripped_from_agent_url(self, db_session):
        insert_guild(db_session, "g-ca-slash")
        card = _mock_agent_card(skill_ids=["lookup_dnsid"])
        post_calls: list[str] = []

        def capture_post(task_url: str, body: bytes) -> dict:
            post_calls.append(task_url)
            return {"status": "ok"}

        with (
            patch("foreman.tools._fetch_agent_card", return_value=card),
            patch("foreman.tools._post_agent_task", side_effect=capture_post),
        ):
            results = await exec_tools(
                "g-ca-slash",
                [
                    _fake_tu(
                        "call_agent",
                        {"agent_url": "http://localhost:8080/", "skill": "lookup_dnsid"},
                    )
                ],
            )

        assert results[0].get("is_error") is not True
        # URL should have slash stripped, then /jsonrpc appended
        assert post_calls[0] == "http://localhost:8080/jsonrpc"

    @pytest.mark.asyncio
    async def test_agent_with_no_skills_in_card_still_calls(self, db_session):
        """An empty skills list means no restriction check — call proceeds."""
        insert_guild(db_session, "g-ca-noskills")
        card = {"name": "bare-agent", "url": "http://bare", "skills": []}
        response = {"output": "bare ok"}

        with (
            patch("foreman.tools._fetch_agent_card", return_value=card),
            patch("foreman.tools._post_agent_task", return_value=response),
        ):
            results = await exec_tools(
                "g-ca-noskills",
                [_fake_tu("call_agent", {"agent_url": "http://bare", "skill": "any_skill"})],
            )

        assert results[0].get("is_error") is not True
        parsed = json.loads(results[0]["content"])
        assert parsed["skill"] == "any_skill"

    @pytest.mark.asyncio
    async def test_params_omitted_defaults_to_empty_object(self, db_session):
        """params is optional — omitting it sends an empty JSON object."""
        insert_guild(db_session, "g-ca-noparams")
        card = _mock_agent_card(skill_ids=["lookup_dnsid"])
        captured_bodies: list[dict] = []

        def capture_post(task_url: str, body: bytes) -> dict:
            captured_bodies.append(json.loads(body))
            return {"ok": True}

        with (
            patch("foreman.tools._fetch_agent_card", return_value=card),
            patch("foreman.tools._post_agent_task", side_effect=capture_post),
        ):
            await exec_tools(
                "g-ca-noparams",
                [
                    _fake_tu(
                        "call_agent",
                        {"agent_url": "http://localhost:8080", "skill": "lookup_dnsid"},
                    )
                ],
            )

        sent = captured_bodies[0]
        # params should be an empty JSON object serialised into the message text
        msg_text = sent["params"]["message"]["parts"][0]["text"]
        assert json.loads(msg_text) == {}

    @pytest.mark.asyncio
    async def test_result_has_correct_tool_use_id(self, db_session):
        insert_guild(db_session, "g-ca-tid")
        with (
            patch("foreman.tools._fetch_agent_card", return_value=_mock_agent_card()),
            patch("foreman.tools._post_agent_task", return_value={"ok": True}),
        ):
            results = await exec_tools(
                "g-ca-tid",
                [
                    _fake_tu(
                        "call_agent",
                        {"agent_url": "http://localhost:8080", "skill": "lookup_dnsid"},
                        "my-agent-tool-id",
                    )
                ],
            )

        assert results[0]["tool_use_id"] == "my-agent-tool-id"
        assert results[0]["type"] == "tool_result"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestCallAgentErrors:
    @pytest.mark.asyncio
    async def test_http_404_on_agent_card_surfaces_as_error(self, db_session):
        insert_guild(db_session, "g-ca-404")
        err = urllib.error.HTTPError("url", 404, "Not Found", {}, None)  # type: ignore

        with patch("foreman.tools._fetch_agent_card", side_effect=err):
            results = await exec_tools(
                "g-ca-404",
                [_fake_tu("call_agent", {"agent_url": "http://localhost:8080", "skill": "x"})],
            )

        assert results[0].get("is_error") is True
        assert "404" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_network_error_on_card_fetch_surfaces_as_error(self, db_session):
        insert_guild(db_session, "g-ca-neterr")
        with patch("foreman.tools._fetch_agent_card", side_effect=OSError("connection refused")):
            results = await exec_tools(
                "g-ca-neterr",
                [_fake_tu("call_agent", {"agent_url": "http://localhost:8080", "skill": "x"})],
            )

        assert results[0].get("is_error") is True
        assert "connection refused" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_agent_task_error_propagates(self, db_session):
        insert_guild(db_session, "g-ca-taskerr")
        with (
            patch("foreman.tools._fetch_agent_card", return_value=_mock_agent_card()),
            patch(
                "foreman.tools._post_agent_task",
                side_effect=RuntimeError("Agent error -32000: skill not supported"),
            ),
        ):
            results = await exec_tools(
                "g-ca-taskerr",
                [
                    _fake_tu(
                        "call_agent",
                        {"agent_url": "http://localhost:8080", "skill": "lookup_dnsid"},
                    )
                ],
            )

        assert results[0].get("is_error") is True
        assert "Agent" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_http_500_on_task_post_surfaces_as_error(self, db_session):
        insert_guild(db_session, "g-ca-500")
        err = urllib.error.HTTPError("url", 500, "Internal Server Error", {}, None)  # type: ignore

        with (
            patch("foreman.tools._fetch_agent_card", return_value=_mock_agent_card()),
            patch("foreman.tools._post_agent_task", side_effect=err),
        ):
            results = await exec_tools(
                "g-ca-500",
                [
                    _fake_tu(
                        "call_agent",
                        {"agent_url": "http://localhost:8080", "skill": "lookup_dnsid"},
                    )
                ],
            )

        assert results[0].get("is_error") is True
        assert "500" in results[0]["content"]
