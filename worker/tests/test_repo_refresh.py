"""Tests for GitHub repo-list refresh logic.

Covers:
  - fetch_accessible_repos: pagination, short last page, network error
  - _refresh_github_repos: merges API repos with static config, updates backend
  - _refresh_github_repos: no-op when list is unchanged
  - _refresh_github_repos: skips when no GitHub token
  - Periodic refresh triggered by _idle_puller when interval elapses
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from pioneer_worker.config import Config
from pioneer_worker.worker import REPO_REFRESH_INTERVAL_SECONDS, Worker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(**kwargs) -> Config:
    return Config(
        backend_url="ws://localhost:8000",
        guild_id="test-guild",
        worker_id="w-test01",
        max_agents=1,
        pull_interval=300.0,
        **kwargs,
    )


def _make_worker(**kwargs) -> Worker:
    worker = Worker(_make_cfg(**kwargs))
    worker._send = AsyncMock()
    return worker


# ---------------------------------------------------------------------------
# fetch_accessible_repos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_accessible_repos_single_page():
    """Returns full_name strings from a single-page response."""
    from pioneer_worker.github_pr import fetch_accessible_repos

    page_data = [{"full_name": "owner/repo1"}, {"full_name": "owner/repo2"}]

    with patch("asyncio.to_thread", new=AsyncMock(return_value=page_data)):
        repos = await fetch_accessible_repos("ghp_token")

    assert repos == ["owner/repo1", "owner/repo2"]


@pytest.mark.asyncio
async def test_fetch_accessible_repos_pagination():
    """Fetches a second page when the first page is full (100 items)."""
    from pioneer_worker.github_pr import fetch_accessible_repos

    page1 = [{"full_name": f"owner/repo{i}"} for i in range(100)]
    page2 = [{"full_name": "owner/repo100"}, {"full_name": "owner/repo101"}]
    call_count = 0

    async def fake_to_thread(fn, *args):
        nonlocal call_count
        call_count += 1
        return page1 if call_count == 1 else page2

    with patch("asyncio.to_thread", side_effect=fake_to_thread):
        repos = await fetch_accessible_repos("ghp_token")

    assert len(repos) == 102
    assert call_count == 2


@pytest.mark.asyncio
async def test_fetch_accessible_repos_network_error_returns_empty():
    """Returns empty list (not an exception) when the API call fails."""
    from pioneer_worker.github_pr import fetch_accessible_repos

    with patch("asyncio.to_thread", new=AsyncMock(side_effect=OSError("timeout"))):
        repos = await fetch_accessible_repos("ghp_token")

    assert repos == []


@pytest.mark.asyncio
async def test_fetch_accessible_repos_skips_missing_full_name():
    """Entries without full_name are silently skipped."""
    from pioneer_worker.github_pr import fetch_accessible_repos

    page_data = [{"full_name": "owner/repo1"}, {"id": 99}, {"full_name": "owner/repo2"}]

    with patch("asyncio.to_thread", new=AsyncMock(return_value=page_data)):
        repos = await fetch_accessible_repos("ghp_token")

    assert repos == ["owner/repo1", "owner/repo2"]


# ---------------------------------------------------------------------------
# _refresh_github_repos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_merges_new_api_repos():
    """Org repos from API not in static config are appended and backend is notified."""
    worker = _make_worker(
        repos=["owner/repo1"],
        org="owner",
        github_token="ghp_token",
    )

    api_repos = ["owner/repo1", "owner/repo2", "owner/repo3"]

    with patch(
        "pioneer_worker.worker.github_pr.fetch_accessible_repos",
        new=AsyncMock(return_value=api_repos),
    ):
        await worker._refresh_github_repos()

    # cfg.repos stays as the static config — only _broadcast_repos expands
    assert worker.cfg.repos == ["owner/repo1"]
    assert worker._broadcast_repos == ["owner/repo1", "owner/repo2", "owner/repo3"]
    worker._send.assert_awaited_once()
    msg = worker._send.call_args[0][0]
    assert msg["type"] == "worker-register"
    assert set(msg["repos"]) == {"owner/repo1", "owner/repo2", "owner/repo3"}


@pytest.mark.asyncio
async def test_refresh_preserves_static_order():
    """Static config repos keep their position; org extras are appended."""
    worker = _make_worker(
        repos=["owner/z-repo", "owner/a-repo"],
        org="owner",
        github_token="ghp_token",
    )

    api_repos = ["owner/a-repo", "owner/z-repo", "owner/new-repo"]

    with patch(
        "pioneer_worker.worker.github_pr.fetch_accessible_repos",
        new=AsyncMock(return_value=api_repos),
    ):
        await worker._refresh_github_repos()

    # cfg.repos stays as the static config — only _broadcast_repos expands
    assert worker.cfg.repos == ["owner/z-repo", "owner/a-repo"]
    assert worker._broadcast_repos[0] == "owner/z-repo"
    assert worker._broadcast_repos[1] == "owner/a-repo"
    assert worker._broadcast_repos[2] == "owner/new-repo"


@pytest.mark.asyncio
async def test_refresh_filters_out_other_org_repos():
    """Repos from other orgs in the API response are not added to broadcast list."""
    worker = _make_worker(
        repos=["myorg/repo1"],
        org="myorg",
        github_token="ghp_token",
    )

    api_repos = ["myorg/repo1", "myorg/repo2", "otherorg/repo3", "unrelated/thing"]

    with patch(
        "pioneer_worker.worker.github_pr.fetch_accessible_repos",
        new=AsyncMock(return_value=api_repos),
    ):
        await worker._refresh_github_repos()

    assert worker.cfg.repos == ["myorg/repo1"]
    assert worker._broadcast_repos == ["myorg/repo1", "myorg/repo2"]
    assert "otherorg/repo3" not in worker._broadcast_repos
    assert "unrelated/thing" not in worker._broadcast_repos


@pytest.mark.asyncio
async def test_refresh_noop_when_no_org():
    """Without cfg.org, no API call is made and broadcast list stays as static config."""
    worker = _make_worker(
        repos=["owner/repo1", "owner/repo2"],
        github_token="ghp_token",
    )

    with patch(
        "pioneer_worker.worker.github_pr.fetch_accessible_repos",
        new=AsyncMock(return_value=["owner/repo1", "owner/repo2", "owner/extra"]),
    ) as mock_fetch:
        await worker._refresh_github_repos()

    mock_fetch.assert_not_awaited()
    worker._send.assert_not_awaited()
    assert worker.cfg.repos == ["owner/repo1", "owner/repo2"]
    assert worker._broadcast_repos == ["owner/repo1", "owner/repo2"]


@pytest.mark.asyncio
async def test_refresh_noop_when_list_unchanged():
    """No worker-register message is sent when the org repo set hasn't changed."""
    worker = _make_worker(
        repos=["owner/repo1", "owner/repo2"],
        org="owner",
        github_token="ghp_token",
    )

    api_repos = ["owner/repo1", "owner/repo2"]

    with patch(
        "pioneer_worker.worker.github_pr.fetch_accessible_repos",
        new=AsyncMock(return_value=api_repos),
    ):
        await worker._refresh_github_repos()

    worker._send.assert_not_awaited()
    assert worker.cfg.repos == ["owner/repo1", "owner/repo2"]


@pytest.mark.asyncio
async def test_refresh_skips_when_no_token():
    """_refresh_github_repos does nothing when github_token is not set."""
    worker = _make_worker(repos=["owner/repo1"], github_token=None)

    with patch(
        "pioneer_worker.worker.github_pr.fetch_accessible_repos",
        new=AsyncMock(return_value=["owner/repo1", "owner/extra"]),
    ) as mock_fetch:
        await worker._refresh_github_repos()

    mock_fetch.assert_not_awaited()
    worker._send.assert_not_awaited()
    assert worker.cfg.repos == ["owner/repo1"]


@pytest.mark.asyncio
async def test_refresh_sets_last_refresh_timestamp():
    """_last_repo_refresh is updated after a successful refresh."""
    worker = _make_worker(repos=[], org="owner", github_token="ghp_token")

    assert worker._last_repo_refresh == 0.0

    with patch(
        "pioneer_worker.worker.github_pr.fetch_accessible_repos",
        new=AsyncMock(return_value=["owner/repo1"]),
    ):
        await worker._refresh_github_repos()

    assert worker._last_repo_refresh > 0.0


@pytest.mark.asyncio
async def test_refresh_noop_when_api_returns_empty():
    """Empty API response is treated as a transient error — config unchanged."""
    worker = _make_worker(repos=["owner/existing"], org="owner", github_token="ghp_token")

    with patch(
        "pioneer_worker.worker.github_pr.fetch_accessible_repos",
        new=AsyncMock(return_value=[]),
    ):
        await worker._refresh_github_repos()

    worker._send.assert_not_awaited()
    assert worker.cfg.repos == ["owner/existing"]


# ---------------------------------------------------------------------------
# Periodic refresh in _idle_puller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_puller_triggers_refresh_after_interval():
    """_idle_puller calls _refresh_github_repos when the interval has elapsed."""
    worker = _make_worker(
        repos=["owner/repo1"],
        github_token="ghp_token",
    )
    worker._fetch_pending_tasks = AsyncMock(return_value=[])
    worker._refresh_github_repos = AsyncMock()

    # Pretend last refresh was long ago so the interval has elapsed
    worker._last_repo_refresh = 0.0

    # Simulate one idle-puller iteration: fire the shutdown after one sleep
    call_count = 0

    async def fake_wait_for(coro, timeout):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            worker._shutdown_event.set()
            raise TimeoutError
        raise TimeoutError  # simulate interval elapsed

    with (
        patch("asyncio.wait_for", side_effect=fake_wait_for),
        patch("pioneer_worker.worker.git_ops.pull_repos", new=AsyncMock()),
    ):
        # Manually set time so interval check passes
        fake_time = REPO_REFRESH_INTERVAL_SECONDS + 100

        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.time.return_value = fake_time
            await worker._idle_puller()

    worker._refresh_github_repos.assert_awaited()


@pytest.mark.asyncio
async def test_idle_puller_skips_refresh_before_interval():
    """_idle_puller does NOT call _refresh_github_repos before the interval elapses."""
    worker = _make_worker(
        repos=["owner/repo1"],
        github_token="ghp_token",
    )
    worker._fetch_pending_tasks = AsyncMock(return_value=[])
    worker._refresh_github_repos = AsyncMock()

    # Pretend refresh just happened
    worker._last_repo_refresh = REPO_REFRESH_INTERVAL_SECONDS + 500  # very recent

    call_count = 0

    async def fake_wait_for(coro, timeout):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            worker._shutdown_event.set()
        raise TimeoutError

    with (
        patch("asyncio.wait_for", side_effect=fake_wait_for),
        patch("pioneer_worker.worker.git_ops.pull_repos", new=AsyncMock()),
    ):
        # Current time is less than last_refresh + interval
        fake_time = worker._last_repo_refresh + 60  # only 60s later

        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.time.return_value = fake_time
            await worker._idle_puller()


@pytest.mark.asyncio
async def test_idle_puller_survives_pull_repos_failure():
    """A git_ops.pull_repos failure (e.g. WS send exhausted retries) must not
    escape _idle_puller — that would crash the whole worker process and kill
    every in-flight task, not just the idle pull."""
    worker = _make_worker(
        repos=["owner/repo1"],
        github_token="ghp_token",
    )
    worker._fetch_pending_tasks = AsyncMock(return_value=[])
    worker._refresh_github_repos = AsyncMock()
    worker._last_repo_refresh = REPO_REFRESH_INTERVAL_SECONDS + 500

    call_count = 0

    async def fake_wait_for(coro, timeout):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            worker._shutdown_event.set()
        raise TimeoutError

    with (
        patch("asyncio.wait_for", side_effect=fake_wait_for),
        patch(
            "pioneer_worker.worker.git_ops.pull_repos",
            new=AsyncMock(side_effect=RuntimeError("ws send failed")),
        ) as mock_pull,
    ):
        fake_time = worker._last_repo_refresh + 60

        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.time.return_value = fake_time
            # Must not raise despite pull_repos failing.
            await worker._idle_puller()

    mock_pull.assert_awaited()

    worker._refresh_github_repos.assert_not_awaited()
