"""Tests for open_pr 422-fallback behaviour."""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pioneer_worker import github_pr

_TASK = {
    "id": "t-abc123",
    "name": "Fix the thing",
    "description": "Fix the thing",
    "issue_repo": "owner/repo",
}
_BRANCH = "claude/fix-the-thing"
_WORKTREE = "/tmp/wt"
_TOKEN = "gh-tok"


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.github.com/repos/owner/repo/pulls",
        code=code,
        msg=f"HTTP {code}",
        hdrs=MagicMock(),  # type: ignore[arg-type]
        fp=BytesIO(b""),
    )


def _fake_urlopen_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@pytest.mark.asyncio
async def test_open_pr_success():
    emit = AsyncMock()
    pr_data = {"html_url": "https://github.com/owner/repo/pull/1"}

    with patch("urllib.request.urlopen", return_value=_fake_urlopen_response(pr_data)):
        result = await github_pr.open_pr(
            task=_TASK,
            branch=_BRANCH,
            worktree_path=_WORKTREE,
            token=_TOKEN,
            emit=emit,
        )

    assert result == "https://github.com/owner/repo/pull/1"


@pytest.mark.asyncio
async def test_open_pr_422_finds_existing_pr():
    """A 422 should trigger a lookup; if an existing PR is found, return it."""
    emit = AsyncMock()
    existing_url = "https://github.com/owner/repo/pull/7"

    with patch(
        "pioneer_worker.github_pr.find_existing_pr",
        new=AsyncMock(return_value=existing_url),
    ):
        with patch(
            "urllib.request.urlopen",
            side_effect=_http_error(422),
        ):
            result = await github_pr.open_pr(
                task=_TASK,
                branch=_BRANCH,
                worktree_path=_WORKTREE,
                token=_TOKEN,
                emit=emit,
            )

    assert result == existing_url
    # Confirm the emit message mentions the branch
    emitted = " ".join(str(c) for c in [call.args[0] for call in emit.call_args_list])
    assert "422" in emitted
    assert existing_url in emitted


@pytest.mark.asyncio
async def test_open_pr_422_no_existing_pr_returns_none():
    """If 422 and no existing PR found, return None (genuine failure)."""
    emit = AsyncMock()

    with patch(
        "pioneer_worker.github_pr.find_existing_pr",
        new=AsyncMock(return_value=None),
    ):
        with patch(
            "urllib.request.urlopen",
            side_effect=_http_error(422),
        ):
            result = await github_pr.open_pr(
                task=_TASK,
                branch=_BRANCH,
                worktree_path=_WORKTREE,
                token=_TOKEN,
                emit=emit,
            )

    assert result is None


@pytest.mark.asyncio
async def test_open_pr_non_422_http_error_returns_none():
    """Non-422 HTTP errors should not trigger the existing-PR lookup."""
    emit = AsyncMock()

    with patch(
        "pioneer_worker.github_pr.find_existing_pr",
        new=AsyncMock(return_value="https://github.com/owner/repo/pull/99"),
    ) as mock_find:
        with patch(
            "urllib.request.urlopen",
            side_effect=_http_error(500),
        ):
            result = await github_pr.open_pr(
                task=_TASK,
                branch=_BRANCH,
                worktree_path=_WORKTREE,
                token=_TOKEN,
                emit=emit,
            )

    assert result is None
    mock_find.assert_not_called()
