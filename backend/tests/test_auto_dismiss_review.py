"""Tests for auto-dismissal of stale changes_requested reviews on push.

See foreman.tools.maybe_dismiss_stale_changes_requested_review, wired into
routes.webhooks on pull_request/synchronize.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import urllib.error
from unittest.mock import MagicMock, patch

from foreman.tools import (
    _auto_dismiss_enabled,
    _auto_dismiss_requires_fix_keyword,
    _commit_message_mentions_fix,
    maybe_dismiss_stale_changes_requested_review,
)
from helpers import _sync_session, insert_guild
from models import Guild
from sqlalchemy import update
from sqlmodel import col

_PUSH_AT = "2026-08-10T12:00:00Z"
_STALE_SUBMITTED_AT = "2026-08-10T10:00:00Z"  # before the push
_FRESH_SUBMITTED_AT = "2026-08-10T13:00:00Z"  # after the push


def _bot_review(*, state="CHANGES_REQUESTED", submitted_at=_STALE_SUBMITTED_AT, review_id=1):
    return {
        "id": review_id,
        "state": state,
        "user": {"login": "pioneer-square-bot[bot]"},
        "submitted_at": submitted_at,
    }


def _human_review(*, state="CHANGES_REQUESTED", submitted_at=_STALE_SUBMITTED_AT, review_id=99):
    return {
        "id": review_id,
        "state": state,
        "user": {"login": "some-human"},
        "submitted_at": submitted_at,
    }


# ---------------------------------------------------------------------------
# Env-var flag helpers
# ---------------------------------------------------------------------------


class TestFlags:
    def test_auto_dismiss_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("AUTO_DISMISS_CHANGES_REQUESTED", raising=False)
        assert _auto_dismiss_enabled() is False

    def test_auto_dismiss_enabled_true(self, monkeypatch):
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")
        assert _auto_dismiss_enabled() is True

    def test_auto_dismiss_enabled_accepts_1(self, monkeypatch):
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "1")
        assert _auto_dismiss_enabled() is True

    def test_require_fix_keyword_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("AUTO_DISMISS_REQUIRE_FIX_KEYWORD", raising=False)
        assert _auto_dismiss_requires_fix_keyword() is False

    def test_require_fix_keyword_enabled(self, monkeypatch):
        monkeypatch.setenv("AUTO_DISMISS_REQUIRE_FIX_KEYWORD", "true")
        assert _auto_dismiss_requires_fix_keyword() is True


class TestCommitMessageMentionsFix:
    def test_matches_fix(self):
        assert _commit_message_mentions_fix("Fix the flaky test") is True

    def test_matches_address(self):
        assert _commit_message_mentions_fix("address review feedback") is True

    def test_matches_resolve(self):
        assert _commit_message_mentions_fix("Resolve lint error") is True

    def test_no_match(self):
        assert _commit_message_mentions_fix("Add a new feature") is False


# ---------------------------------------------------------------------------
# maybe_dismiss_stale_changes_requested_review
# ---------------------------------------------------------------------------


class TestMaybeDismissStaleChangesRequestedReview:
    async def test_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("AUTO_DISMISS_CHANGES_REQUESTED", raising=False)
        with patch("foreman.tools._guild_github_token") as mock_creds:
            result = await maybe_dismiss_stale_changes_requested_review(
                "g1", "o/r", 1, _PUSH_AT, "sha123"
            )
        assert result is None
        mock_creds.assert_not_called()

    async def test_noop_when_no_push_timestamp(self, monkeypatch):
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")
        with patch("foreman.tools._guild_github_token") as mock_creds:
            result = await maybe_dismiss_stale_changes_requested_review(
                "g1", "o/r", 1, None, "sha123"
            )
        assert result is None
        mock_creds.assert_not_called()

    async def test_noop_when_no_github_credentials(self, monkeypatch):
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")
        with patch("foreman.tools._guild_github_token", return_value=None):
            result = await maybe_dismiss_stale_changes_requested_review(
                "g1", "o/r", 1, _PUSH_AT, "sha123"
            )
        assert result is None

    async def test_dismisses_stale_bot_changes_requested_review(self, monkeypatch):
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")
        reviews = [_bot_review()]
        dismiss_calls = []

        def fake_gh_api(path, token):
            assert path == "/repos/o/r/pulls/1/reviews?per_page=50"
            return reviews

        def fake_gh_api_post(path, token, payload, method="POST"):
            dismiss_calls.append((path, payload, method))
            return {}

        with (
            patch(
                "foreman.tools._guild_github_token",
                return_value=("tok", "pioneer-square-bot[bot]"),
            ),
            patch("foreman.tools._gh_api", side_effect=fake_gh_api),
            patch("foreman.tools._gh_api_post", side_effect=fake_gh_api_post),
        ):
            result = await maybe_dismiss_stale_changes_requested_review(
                "g1", "o/r", 1, _PUSH_AT, "sha123"
            )

        assert result == "pioneer-square-bot[bot]"
        assert len(dismiss_calls) == 1
        path, payload, method = dismiss_calls[0]
        assert path == "/repos/o/r/pulls/1/reviews/1/dismissals"
        assert payload["event"] == "DISMISS"
        assert method == "PUT"

    async def test_does_not_dismiss_when_review_is_after_push(self, monkeypatch):
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")
        reviews = [_bot_review(submitted_at=_FRESH_SUBMITTED_AT)]

        with (
            patch(
                "foreman.tools._guild_github_token",
                return_value=("tok", "pioneer-square-bot[bot]"),
            ),
            patch("foreman.tools._gh_api", return_value=reviews),
            patch("foreman.tools._gh_api_post") as mock_post,
        ):
            result = await maybe_dismiss_stale_changes_requested_review(
                "g1", "o/r", 1, _PUSH_AT, "sha123"
            )

        assert result is None
        mock_post.assert_not_called()

    async def test_does_not_dismiss_approved_review(self, monkeypatch):
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")
        reviews = [_bot_review(state="APPROVED")]

        with (
            patch(
                "foreman.tools._guild_github_token",
                return_value=("tok", "pioneer-square-bot[bot]"),
            ),
            patch("foreman.tools._gh_api", return_value=reviews),
            patch("foreman.tools._gh_api_post") as mock_post,
        ):
            result = await maybe_dismiss_stale_changes_requested_review(
                "g1", "o/r", 1, _PUSH_AT, "sha123"
            )

        assert result is None
        mock_post.assert_not_called()

    async def test_ignores_human_reviewer_changes_requested(self, monkeypatch):
        """Only the guild's own bot review may ever be auto-dismissed."""
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")
        reviews = [_human_review()]

        with (
            patch(
                "foreman.tools._guild_github_token",
                return_value=("tok", "pioneer-square-bot[bot]"),
            ),
            patch("foreman.tools._gh_api", return_value=reviews),
            patch("foreman.tools._gh_api_post") as mock_post,
        ):
            result = await maybe_dismiss_stale_changes_requested_review(
                "g1", "o/r", 1, _PUSH_AT, "sha123"
            )

        assert result is None
        mock_post.assert_not_called()

    async def test_uses_bot_s_latest_review_not_a_superseded_one(self, monkeypatch):
        """An older changes_requested review the bot itself later approved is not stale-dismissed."""
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")
        reviews = [
            _bot_review(
                state="CHANGES_REQUESTED", submitted_at="2026-08-10T08:00:00Z", review_id=1
            ),
            _bot_review(state="APPROVED", submitted_at="2026-08-10T09:00:00Z", review_id=2),
        ]

        with (
            patch(
                "foreman.tools._guild_github_token",
                return_value=("tok", "pioneer-square-bot[bot]"),
            ),
            patch("foreman.tools._gh_api", return_value=reviews),
            patch("foreman.tools._gh_api_post") as mock_post,
        ):
            result = await maybe_dismiss_stale_changes_requested_review(
                "g1", "o/r", 1, _PUSH_AT, "sha123"
            )

        assert result is None
        mock_post.assert_not_called()

    async def test_reviews_fetch_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")

        def raise_http_error(path, token):
            raise urllib.error.HTTPError(path, 500, "boom", {}, None)

        with (
            patch(
                "foreman.tools._guild_github_token",
                return_value=("tok", "pioneer-square-bot[bot]"),
            ),
            patch("foreman.tools._gh_api", side_effect=raise_http_error),
        ):
            result = await maybe_dismiss_stale_changes_requested_review(
                "g1", "o/r", 1, _PUSH_AT, "sha123"
            )

        assert result is None

    async def test_dismiss_call_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")
        reviews = [_bot_review()]

        def raise_http_error(path, token, payload, method="POST"):
            raise urllib.error.HTTPError(path, 422, "nope", {}, None)

        with (
            patch(
                "foreman.tools._guild_github_token",
                return_value=("tok", "pioneer-square-bot[bot]"),
            ),
            patch("foreman.tools._gh_api", return_value=reviews),
            patch("foreman.tools._gh_api_post", side_effect=raise_http_error),
        ):
            result = await maybe_dismiss_stale_changes_requested_review(
                "g1", "o/r", 1, _PUSH_AT, "sha123"
            )

        assert result is None

    async def test_require_fix_keyword_blocks_dismissal_when_absent(self, monkeypatch):
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")
        monkeypatch.setenv("AUTO_DISMISS_REQUIRE_FIX_KEYWORD", "true")
        reviews = [_bot_review()]
        commit = {"commit": {"message": "Add a new feature"}}

        def fake_gh_api(path, token):
            if path.endswith("/reviews?per_page=50"):
                return reviews
            if path == "/repos/o/r/commits/sha123":
                return commit
            raise AssertionError(f"unexpected path {path}")

        with (
            patch(
                "foreman.tools._guild_github_token",
                return_value=("tok", "pioneer-square-bot[bot]"),
            ),
            patch("foreman.tools._gh_api", side_effect=fake_gh_api),
            patch("foreman.tools._gh_api_post") as mock_post,
        ):
            result = await maybe_dismiss_stale_changes_requested_review(
                "g1", "o/r", 1, _PUSH_AT, "sha123"
            )

        assert result is None
        mock_post.assert_not_called()

    async def test_require_fix_keyword_allows_dismissal_when_present(self, monkeypatch):
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")
        monkeypatch.setenv("AUTO_DISMISS_REQUIRE_FIX_KEYWORD", "true")
        reviews = [_bot_review()]
        commit = {"commit": {"message": "Fix the bug flagged in review"}}

        def fake_gh_api(path, token):
            if path.endswith("/reviews?per_page=50"):
                return reviews
            if path == "/repos/o/r/commits/sha123":
                return commit
            raise AssertionError(f"unexpected path {path}")

        with (
            patch(
                "foreman.tools._guild_github_token",
                return_value=("tok", "pioneer-square-bot[bot]"),
            ),
            patch("foreman.tools._gh_api", side_effect=fake_gh_api),
            patch("foreman.tools._gh_api_post", return_value={}) as mock_post,
        ):
            result = await maybe_dismiss_stale_changes_requested_review(
                "g1", "o/r", 1, _PUSH_AT, "sha123"
            )

        assert result == "pioneer-square-bot[bot]"
        mock_post.assert_called_once()

    async def test_require_fix_keyword_without_head_sha_skips(self, monkeypatch):
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")
        monkeypatch.setenv("AUTO_DISMISS_REQUIRE_FIX_KEYWORD", "true")
        reviews = [_bot_review()]

        with (
            patch(
                "foreman.tools._guild_github_token",
                return_value=("tok", "pioneer-square-bot[bot]"),
            ),
            patch("foreman.tools._gh_api", return_value=reviews),
            patch("foreman.tools._gh_api_post") as mock_post,
        ):
            result = await maybe_dismiss_stale_changes_requested_review(
                "g1", "o/r", 1, _PUSH_AT, None
            )

        assert result is None
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Webhook wiring: pull_request/synchronize schedules the dismissal check
#
# routes.webhooks imports maybe_dismiss_stale_changes_requested_review lazily
# (inside the synchronize branch, matching the existing finalize_closed_issue
# local-import convention in that module), so patching
# foreman.tools._guild_github_token/_gh_api/_gh_api_post below is picked up by
# the real call the handler makes. spawn() is patched to capture the
# background coroutine instead of scheduling it, since a fire-and-forget
# asyncio.create_task isn't guaranteed to run to completion before the
# synchronous TestClient request returns; the captured coroutine is then run
# to completion directly so its GitHub API side effects can be asserted on.
# ---------------------------------------------------------------------------


def _pr_payload(*, action: str, repo: str, number: int, updated_at: str, head_sha: str) -> dict:
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {
            "number": number,
            "html_url": f"https://github.com/{repo}/pull/{number}",
            "updated_at": updated_at,
            "head": {"sha": head_sha},
        },
        "sender": {"login": "octocat"},
    }


def _signed_headers(secret: str, body: bytes, *, event: str, delivery: str) -> dict[str, str]:
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": f"sha256={sig}",
        "Content-Type": "application/json",
    }


def _set_webhook_secret(db_url: str, guild_id: str, secret: str) -> None:
    with _sync_session(db_url) as session:
        session.execute(
            update(Guild).where(col(Guild.slug) == guild_id).values(webhook_secret=secret)
        )
        session.commit()


class TestWebhookSchedulesAutoDismiss:
    def test_synchronize_webhook_dismisses_stale_bot_review(self, client, monkeypatch):
        test_client, db_url = client
        insert_guild(db_url, "g-sync1")
        _set_webhook_secret(db_url, "g-sync1", "s-sync1")
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")

        payload = _pr_payload(
            action="synchronize",
            repo="owner/repo",
            number=5,
            updated_at=_PUSH_AT,
            head_sha="abcdef",
        )
        body = json.dumps(payload).encode()
        headers = _signed_headers("s-sync1", body, event="pull_request", delivery="d-sync-1")

        scheduled: list[tuple[str | None, object]] = []

        def fake_spawn(coro, *, name=None):
            scheduled.append((name, coro))
            return MagicMock()

        reviews = [_bot_review()]
        dismiss_calls = []

        def fake_gh_api(path, token):
            return reviews

        def fake_gh_api_post(path, token, post_payload, method="POST"):
            dismiss_calls.append((path, post_payload, method))
            return {}

        with (
            patch("routes.webhooks.spawn", side_effect=fake_spawn),
            patch(
                "foreman.tools._guild_github_token",
                return_value=("tok", "pioneer-square-bot[bot]"),
            ),
            patch("foreman.tools._gh_api", side_effect=fake_gh_api),
            patch("foreman.tools._gh_api_post", side_effect=fake_gh_api_post),
        ):
            resp = test_client.post("/webhooks/github/g-sync1", content=body, headers=headers)
            assert resp.status_code == 202

            dismiss_entry = next(
                (c for c in scheduled if (c[0] or "").startswith("auto-dismiss-review:")), None
            )
            assert dismiss_entry is not None, f"no auto-dismiss task scheduled; got {scheduled}"
            _, dismiss_coro = dismiss_entry

            result = asyncio.run(dismiss_coro)

        # Discard any other spawned coroutines (e.g. the Discord "PR updated"
        # notification) that were captured but never run.
        for _name, coro in scheduled:
            if coro is not dismiss_coro and asyncio.iscoroutine(coro):
                coro.close()

        assert result == "pioneer-square-bot[bot]"
        assert len(dismiss_calls) == 1
        path, _, method = dismiss_calls[0]
        assert path == "/repos/owner/repo/pulls/5/reviews/1/dismissals"
        assert method == "PUT"

    def test_opened_webhook_does_not_schedule_dismissal_check(self, client, monkeypatch):
        test_client, db_url = client
        insert_guild(db_url, "g-sync2")
        _set_webhook_secret(db_url, "g-sync2", "s-sync2")
        monkeypatch.setenv("AUTO_DISMISS_CHANGES_REQUESTED", "true")

        payload = _pr_payload(
            action="opened",
            repo="owner/repo",
            number=6,
            updated_at=_PUSH_AT,
            head_sha="abcdef",
        )
        body = json.dumps(payload).encode()
        headers = _signed_headers("s-sync2", body, event="pull_request", delivery="d-sync-2")

        scheduled: list[tuple[str | None, object]] = []

        def fake_spawn(coro, *, name=None):
            scheduled.append((name, coro))
            if asyncio.iscoroutine(coro):
                coro.close()
            return MagicMock()

        with patch("routes.webhooks.spawn", side_effect=fake_spawn):
            resp = test_client.post("/webhooks/github/g-sync2", content=body, headers=headers)
            assert resp.status_code == 202

        assert not any((name or "").startswith("auto-dismiss-review:") for name, _ in scheduled)
