"""Unit tests for the review_pr foreman tool.

All external calls (A2AClient, GitHub API, DB) are mocked — no real network or
subprocess usage.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from foreman.tools import exec_tools
from helpers import create_db, insert_guild

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_review_pr.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("DB_PATH", db_path)
    create_db(db_path)
    engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)
    yield db_path


def _fake_tu(name: str, inputs: dict, tool_id: str = "tool-rev1") -> SimpleNamespace:
    return SimpleNamespace(name=name, input=inputs, id=tool_id)


_REVIEW_REPORT_MIME = "application/vnd.code-review-agent.report+json"


def _mock_a2a_result(
    review_text: str = "## Review\nLooks good.",
    verdict: str = "approved",
) -> dict:
    return {
        "id": "task-abc",
        "status": {"state": "completed"},
        "artifacts": [
            {
                "name": "review",
                "parts": [
                    {"type": "text", "text": review_text},
                    {
                        "type": _REVIEW_REPORT_MIME,
                        "text": json.dumps(
                            {
                                "review_id": "r-001",
                                "pr_url": "https://github.com/org/repo/pull/42",
                                "verdict": verdict,
                                "findings": [],
                            }
                        ),
                    },
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# review_pr input validation
# ---------------------------------------------------------------------------


class TestReviewPrValidation:
    @pytest.mark.asyncio
    async def test_invalid_pr_url_returns_error(self, db_session):
        insert_guild(db_session, "g-rev-badurl")
        with patch("foreman.tools._guild_github_token", return_value=("tok", "user")):
            results = await exec_tools(
                "g-rev-badurl",
                [_fake_tu("review_pr", {"pr_url": "not-a-url"})],
            )
        assert results[0].get("is_error") is True
        assert "Invalid GitHub PR URL" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_no_github_token_returns_error(self, db_session):
        insert_guild(db_session, "g-rev-notoк")
        with patch("foreman.tools._guild_github_token", return_value=None):
            results = await exec_tools(
                "g-rev-notoк",
                [_fake_tu("review_pr", {"pr_url": "https://github.com/org/repo/pull/1"})],
            )
        assert results[0].get("is_error") is True
        assert "No GitHub token" in results[0]["content"]


# ---------------------------------------------------------------------------
# review_pr happy paths
# ---------------------------------------------------------------------------


class TestReviewPrHappyPath:
    @pytest.mark.asyncio
    async def test_approve_verdict_posts_approve_review(self, db_session):
        """When the code-review-agent approves the PR, GitHub APPROVE is posted."""
        insert_guild(db_session, "g-rev-approve")

        mock_client = MagicMock()
        mock_client.review_pr = AsyncMock(return_value=_mock_a2a_result(verdict="approved"))
        gh_post_calls = []

        def capture_gh_post(path, token, payload, method="POST"):
            gh_post_calls.append({"path": path, "payload": payload})
            return {"id": 99, "state": "APPROVED"}

        with (
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.a2a_client.A2AClient", return_value=mock_client),
            patch("foreman.tools._gh_api_post", side_effect=capture_gh_post),
        ):
            results = await exec_tools(
                "g-rev-approve",
                [_fake_tu("review_pr", {"pr_url": "https://github.com/org/repo/pull/42"})],
            )

        assert results[0].get("is_error") is not True
        parsed = json.loads(results[0]["content"])
        assert parsed["verdict"] == "APPROVE"
        assert parsed["review_posted"] is True
        assert parsed["review_id"] == 99

        assert len(gh_post_calls) == 1
        assert "/repos/org/repo/pulls/42/reviews" in gh_post_calls[0]["path"]
        assert gh_post_calls[0]["payload"]["event"] == "APPROVE"

    @pytest.mark.asyncio
    async def test_changes_requested_posts_request_changes(self, db_session):
        """When the agent requests changes, GitHub REQUEST_CHANGES is posted."""
        insert_guild(db_session, "g-rev-changes")

        mock_client = MagicMock()
        mock_client.review_pr = AsyncMock(
            return_value=_mock_a2a_result(verdict="changes-requested")
        )

        with (
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.a2a_client.A2AClient", return_value=mock_client),
            patch(
                "foreman.tools._gh_api_post", return_value={"id": 55, "state": "CHANGES_REQUESTED"}
            ),
        ):
            results = await exec_tools(
                "g-rev-changes",
                [_fake_tu("review_pr", {"pr_url": "https://github.com/org/repo/pull/7"})],
            )

        parsed = json.loads(results[0]["content"])
        assert parsed["verdict"] == "REQUEST_CHANGES"

    @pytest.mark.asyncio
    async def test_no_report_artifact_defaults_to_comment(self, db_session):
        """Without a report artifact, the tool falls back to COMMENT verdict."""
        insert_guild(db_session, "g-rev-nosc")

        plain_result = {"artifacts": [{"parts": [{"type": "text", "text": "Some review text."}]}]}
        mock_client = MagicMock()
        mock_client.review_pr = AsyncMock(return_value=plain_result)

        with (
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.a2a_client.A2AClient", return_value=mock_client),
            patch("foreman.tools._gh_api_post", return_value={"id": 33}),
        ):
            results = await exec_tools(
                "g-rev-nosc",
                [_fake_tu("review_pr", {"pr_url": "https://github.com/org/repo/pull/3"})],
            )

        parsed = json.loads(results[0]["content"])
        assert parsed["verdict"] == "COMMENT"

    @pytest.mark.asyncio
    async def test_review_body_included_in_result(self, db_session):
        """The summary field in the result contains the beginning of the review text."""
        insert_guild(db_session, "g-rev-body")

        review_text = "## Code Review\nEverything looks great!"
        mock_client = MagicMock()
        mock_client.review_pr = AsyncMock(
            return_value=_mock_a2a_result(review_text=review_text, verdict="approved")
        )

        with (
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.a2a_client.A2AClient", return_value=mock_client),
            patch("foreman.tools._gh_api_post", return_value={"id": 1}),
        ):
            results = await exec_tools(
                "g-rev-body",
                [_fake_tu("review_pr", {"pr_url": "https://github.com/org/repo/pull/10"})],
            )

        parsed = json.loads(results[0]["content"])
        assert "Code Review" in parsed["summary"]

    @pytest.mark.asyncio
    async def test_a2a_call_passes_pr_url_and_guild_domain(self, db_session):
        """The foreman passes the PR URL and guild caller domain to review_pr."""
        insert_guild(db_session, "g-rev-args")

        mock_client = MagicMock()
        mock_client.review_pr = AsyncMock(return_value=_mock_a2a_result())

        with (
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.a2a_client.A2AClient", return_value=mock_client),
            patch("foreman.tools._gh_api_post", return_value={"id": 1}),
        ):
            await exec_tools(
                "g-rev-args",
                [_fake_tu("review_pr", {"pr_url": "https://github.com/org/repo/pull/99"})],
            )

        mock_client.review_pr.assert_awaited_once_with(
            "https://github.com/org/repo/pull/99",
            caller_domain="g-rev-args.pioneer-square.melloy.life",
            private_key_pem=None,
        )


# ---------------------------------------------------------------------------
# review_pr error paths
# ---------------------------------------------------------------------------


class TestReviewPrErrors:
    @pytest.mark.asyncio
    async def test_review_agent_error_propagates_as_tool_error(self, db_session):
        """A RuntimeError from the review agent is surfaced as a tool error."""
        insert_guild(db_session, "g-rev-agenterr")

        mock_client = MagicMock()
        mock_client.review_pr = AsyncMock(side_effect=RuntimeError("agent unavailable"))

        with (
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.a2a_client.A2AClient", return_value=mock_client),
        ):
            results = await exec_tools(
                "g-rev-agenterr",
                [_fake_tu("review_pr", {"pr_url": "https://github.com/org/repo/pull/5"})],
            )

        assert results[0].get("is_error") is True

    @pytest.mark.asyncio
    async def test_github_post_failure_surfaces_as_error(self, db_session):
        """If posting the review to GitHub fails, the tool returns an error."""
        import urllib.error

        insert_guild(db_session, "g-rev-gherr")

        mock_client = MagicMock()
        mock_client.review_pr = AsyncMock(return_value=_mock_a2a_result())
        gh_err = urllib.error.HTTPError("url", 422, "Unprocessable Entity", None, None)  # type: ignore

        with (
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.a2a_client.A2AClient", return_value=mock_client),
            patch("foreman.tools._gh_api_post", side_effect=gh_err),
        ):
            results = await exec_tools(
                "g-rev-gherr",
                [_fake_tu("review_pr", {"pr_url": "https://github.com/org/repo/pull/8"})],
            )

        assert results[0].get("is_error") is True
        assert "422" in results[0]["content"] or "GitHub API error" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_result_has_correct_tool_use_id(self, db_session):
        """The tool_result block must echo back the original tool_use_id."""
        insert_guild(db_session, "g-rev-tid")

        mock_client = MagicMock()
        mock_client.review_pr = AsyncMock(return_value=_mock_a2a_result())

        with (
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.a2a_client.A2AClient", return_value=mock_client),
            patch("foreman.tools._gh_api_post", return_value={"id": 1}),
        ):
            results = await exec_tools(
                "g-rev-tid",
                [
                    _fake_tu(
                        "review_pr", {"pr_url": "https://github.com/org/repo/pull/1"}, "my-tool-id"
                    )
                ],
            )

        assert results[0]["tool_use_id"] == "my-tool-id"
        assert results[0]["type"] == "tool_result"
