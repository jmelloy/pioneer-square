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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

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


# ---------------------------------------------------------------------------
# _supersede_prior_bot_reviews unit tests
# ---------------------------------------------------------------------------


def _make_review(review_id: int, login: str, body: str = "Old review.") -> dict:
    return {"id": review_id, "user": {"login": login}, "body": body}


def _make_gql_threads(threads: list[dict]) -> dict:
    """Build a minimal GraphQL response for GetPRThreads."""
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": threads}}}}}


def _make_thread(thread_id: str, review_db_id: int, is_resolved: bool = False) -> dict:
    return {
        "id": thread_id,
        "isResolved": is_resolved,
        "comments": {
            "nodes": [{"databaseId": 1, "pullRequestReview": {"databaseId": review_db_id}}]
        },
    }


class TestSupersedePriorBotReviews:
    @pytest.mark.asyncio
    async def test_no_prior_reviews_is_noop(self):
        """When there are no prior reviews, nothing is called."""
        from foreman.tools import _supersede_prior_bot_reviews

        with patch("foreman.tools._gh_api", return_value=[]):
            result = await _supersede_prior_bot_reviews("org/repo", 42, "bot", "tok")

        assert result == 0

    @pytest.mark.asyncio
    async def test_no_bot_reviews_is_noop(self):
        """When no reviews belong to the bot user, nothing is superseded."""
        from foreman.tools import _supersede_prior_bot_reviews

        reviews = [_make_review(1, "other-user")]
        with patch("foreman.tools._gh_api", return_value=reviews):
            result = await _supersede_prior_bot_reviews("org/repo", 42, "bot", "tok")

        assert result == 0

    @pytest.mark.asyncio
    async def test_resolves_unresolved_inline_threads(self):
        """Unresolved threads from a prior bot review are resolved via GraphQL."""
        from foreman.tools import _supersede_prior_bot_reviews

        reviews = [_make_review(10, "bot")]
        thread = _make_thread("TID_xyz", review_db_id=10, is_resolved=False)
        gql_responses = [_make_gql_threads([thread]), {}]
        gql_iter = iter(gql_responses)
        graphql_calls = []

        def fake_graphql(token, query, variables):
            graphql_calls.append({"query": query, "variables": variables})
            return next(gql_iter)

        with (
            patch("foreman.tools._gh_api", return_value=reviews),
            patch("foreman.tools._gh_graphql", side_effect=fake_graphql),
            patch("foreman.tools._gh_api_post", return_value={}),
        ):
            await _supersede_prior_bot_reviews("org/repo", 42, "bot", "tok")

        resolve_calls = [c for c in graphql_calls if "resolveReviewThread" in c["query"]]
        assert len(resolve_calls) == 1
        assert resolve_calls[0]["variables"]["threadId"] == "TID_xyz"

    @pytest.mark.asyncio
    async def test_already_resolved_threads_are_skipped(self):
        """Threads already marked isResolved are not re-resolved."""
        from foreman.tools import _supersede_prior_bot_reviews

        reviews = [_make_review(11, "bot")]
        thread = _make_thread("TID_done", review_db_id=11, is_resolved=True)
        graphql_calls = []

        def fake_graphql(token, query, variables):
            graphql_calls.append({"query": query, "variables": variables})
            return _make_gql_threads([thread]) if "GetPRThreads" in query else {}

        with (
            patch("foreman.tools._gh_api", return_value=reviews),
            patch("foreman.tools._gh_graphql", side_effect=fake_graphql),
            patch("foreman.tools._gh_api_post", return_value={}),
        ):
            await _supersede_prior_bot_reviews("org/repo", 42, "bot", "tok")

        resolve_calls = [c for c in graphql_calls if "resolveReviewThread" in c["query"]]
        assert len(resolve_calls) == 0


class TestReviewPrInternal:
    """review_pr_internal must use the guild's configured LLM client, not a
    hardcoded direct-Anthropic client — see issue #874."""

    @staticmethod
    def _pr_data():
        return {
            "title": "Fix the thing",
            "body": "Fixes a bug.",
            "base": {"ref": "main"},
            "head": {"ref": "fix-branch"},
        }

    @pytest.mark.asyncio
    async def test_uses_guild_resolved_client_and_model(self, db_session):
        """The tool resolves the guild's configured client/model instead of
        always defaulting to the direct Anthropic API."""
        insert_guild(db_session, "g-revint-provider")

        fake_client = object()
        resolve_calls = []

        async def fake_resolve(guild_id, guild_cfg):
            resolve_calls.append((guild_id, guild_cfg))
            return fake_client, "bedrock", "us.anthropic.claude-sonnet-4-6"

        call_llm_calls = []

        async def fake_call_llm(guild_id, **kwargs):
            call_llm_calls.append(kwargs)
            review_text = json.dumps({"summary": "- Looks fine", "comments": []})
            return SimpleNamespace(
                response=SimpleNamespace(content=[SimpleNamespace(text=review_text)])
            )

        with (
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.tools._gh_api", return_value=self._pr_data()),
            patch("foreman.tools._gh_api_diff", return_value="diff --git a b"),
            patch("foreman.tools._gh_api_post", return_value={"id": 1}),
            patch("foreman.runner._load_foreman_config", return_value={"provider": "bedrock"}),
            patch("foreman.runner.resolve_foreman_client", side_effect=fake_resolve),
            patch("foreman.runner._call_llm", side_effect=fake_call_llm),
        ):
            results = await exec_tools(
                "g-revint-provider",
                [_fake_tu("review_pr_internal", {"pr_url": "https://github.com/org/repo/pull/1"})],
            )

        assert results[0].get("is_error") is not True
        assert len(resolve_calls) == 1
        assert resolve_calls[0][0] == "g-revint-provider"
        assert len(call_llm_calls) == 1
        assert call_llm_calls[0]["client"] is fake_client
        assert call_llm_calls[0]["provider"] == "bedrock"
        assert call_llm_calls[0]["model"] == "us.anthropic.claude-sonnet-4-6"

        parsed = json.loads(results[0]["content"])
        assert "Looks fine" in parsed["summary"]

    @pytest.mark.asyncio
    async def test_ai_failure_surfaces_error_in_summary(self, db_session):
        """When the LLM call fails, the posted review's summary names the
        failure instead of a generic, undiagnosable message."""
        insert_guild(db_session, "g-revint-fail")

        gh_post_calls = []

        def capture_gh_post(path, token, payload, method="POST"):
            gh_post_calls.append(payload)
            return {"id": 5}

        async def fake_resolve(guild_id, guild_cfg):
            return object(), "anthropic", "claude-sonnet-4-6"

        async def fake_call_llm(guild_id, **kwargs):
            raise RuntimeError("could not resolve credentials from session")

        with (
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.tools._gh_api", return_value=self._pr_data()),
            patch("foreman.tools._gh_api_diff", return_value="diff --git a b"),
            patch("foreman.tools._gh_api_post", side_effect=capture_gh_post),
            patch("foreman.runner._load_foreman_config", return_value={}),
            patch("foreman.runner.resolve_foreman_client", side_effect=fake_resolve),
            patch("foreman.runner._call_llm", side_effect=fake_call_llm),
        ):
            results = await exec_tools(
                "g-revint-fail",
                [_fake_tu("review_pr_internal", {"pr_url": "https://github.com/org/repo/pull/2"})],
            )

        assert results[0].get("is_error") is not True
        assert len(gh_post_calls) == 1
        assert "could not resolve credentials from session" in gh_post_calls[0]["body"]
        parsed = json.loads(results[0]["content"])
        assert "could not resolve credentials from session" in parsed["summary"]


class TestReviewPrSupersedePriorReviews:
    """Integration tests: review_pr tool calls _supersede_prior_bot_reviews."""

    @pytest.mark.asyncio
    async def test_supersede_called_before_posting_new_review(self, db_session):
        """_supersede_prior_bot_reviews is invoked before the new review is posted."""
        insert_guild(db_session, "g-rev-sup1")

        mock_client = MagicMock()
        mock_client.review_pr = AsyncMock(return_value=_mock_a2a_result())
        supersede_calls = []

        async def fake_supersede(pr_repo, pr_number, bot_username, token):
            supersede_calls.append((pr_repo, pr_number, bot_username))
            return 1

        with (
            patch("foreman.tools._guild_github_token", return_value=("tok", "bot-user")),
            patch("foreman.a2a_client.A2AClient", return_value=mock_client),
            patch("foreman.tools._supersede_prior_bot_reviews", side_effect=fake_supersede),
            patch("foreman.tools._gh_api_post", return_value={"id": 55}),
        ):
            results = await exec_tools(
                "g-rev-sup1",
                [_fake_tu("review_pr", {"pr_url": "https://github.com/org/repo/pull/20"})],
            )

        assert results[0].get("is_error") is not True
        assert len(supersede_calls) == 1
        assert supersede_calls[0] == ("org/repo", 20, "bot-user")

    @pytest.mark.asyncio
    async def test_supersede_failure_does_not_block_review_post(self, db_session):
        """Even if supersede raises, the new review is still posted (non-fatal)."""
        insert_guild(db_session, "g-rev-sup2")

        mock_client = MagicMock()
        mock_client.review_pr = AsyncMock(return_value=_mock_a2a_result())

        async def failing_supersede(*args):
            raise RuntimeError("supersede blew up")

        with (
            patch("foreman.tools._guild_github_token", return_value=("tok", "bot-user")),
            patch("foreman.a2a_client.A2AClient", return_value=mock_client),
            patch("foreman.tools._supersede_prior_bot_reviews", side_effect=failing_supersede),
            patch("foreman.tools._gh_api_post", return_value={"id": 77}),
        ):
            results = await exec_tools(
                "g-rev-sup2",
                [_fake_tu("review_pr", {"pr_url": "https://github.com/org/repo/pull/21"})],
            )

        # Supersede failure is caught and logged; review post proceeds normally.
        assert results[0].get("is_error") is not True
        parsed = json.loads(results[0]["content"])
        assert parsed["review_id"] == 77
