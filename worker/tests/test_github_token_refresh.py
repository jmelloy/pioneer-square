"""Tests for GitHub token refresh — long-lived workers must not run forever on
a token fetched once at startup.

Covers:
  - _fetch_github_token_if_needed: fetches + marks dynamic when unconfigured
  - _fetch_github_token_if_needed: skips fetch when a static token is configured
  - _refresh_github_token: overwrites cfg.github_token and GITHUB_TOKEN unconditionally
  - _idle_puller: triggers _refresh_github_token after the interval, only for dynamic tokens
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pioneer_worker.config import Config
from pioneer_worker.worker import GITHUB_TOKEN_REFRESH_INTERVAL_SECONDS, Worker


def _make_cfg(**kwargs) -> Config:
    return Config(
        backend_url="ws://localhost:8000",
        guild_id="test-guild",
        worker_id="w-test01",
        auth_token="tok-worker",
        max_agents=1,
        pull_interval=300.0,
        **kwargs,
    )


def _make_worker(**kwargs) -> Worker:
    worker = Worker(_make_cfg(**kwargs))
    worker._send = AsyncMock()
    return worker


class _FakeCtx:
    """Minimal async-context-manager stand-in for httpx.AsyncClient."""

    def __init__(self, status_code: int, body: dict):
        self._status_code = status_code
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = self._status_code
        resp.json.return_value = self._body
        return resp


# ---------------------------------------------------------------------------
# _fetch_github_token_if_needed (startup path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_fetches_and_marks_dynamic_when_unconfigured(monkeypatch):
    """No static token → fetches from backend and marks the token as dynamic."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    worker = _make_worker()  # github_token unset

    ctx = _FakeCtx(200, {"access_token": "ghs_fetched", "username": "app[bot]"})
    with patch.object(worker, "_http", return_value=ctx):
        await worker._fetch_github_token_if_needed()

    assert worker.cfg.github_token == "ghs_fetched"
    assert worker._github_token_dynamic is True
    assert os.environ["GITHUB_TOKEN"] == "ghs_fetched"
    del os.environ["GITHUB_TOKEN"]


@pytest.mark.asyncio
async def test_startup_skips_fetch_when_statically_configured():
    """A pre-configured token (config file / env var) is left alone, not marked dynamic."""
    worker = _make_worker(github_token="ghp_static")

    http_called = False

    async def _fake_http(*a, **kw):
        nonlocal http_called
        http_called = True

    with patch.object(worker, "_http", side_effect=_fake_http):
        await worker._fetch_github_token_if_needed()

    assert not http_called
    assert worker.cfg.github_token == "ghp_static"
    assert worker._github_token_dynamic is False


# ---------------------------------------------------------------------------
# _refresh_github_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_overwrites_stale_token_and_env(monkeypatch):
    """A stale cached token and stale GITHUB_TOKEN env var are both replaced."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_stale")
    worker = _make_worker(github_token="ghs_stale")

    ctx = _FakeCtx(200, {"access_token": "ghs_fresh", "username": "app[bot]"})
    with patch.object(worker, "_http", return_value=ctx):
        await worker._refresh_github_token()

    assert worker.cfg.github_token == "ghs_fresh"
    assert os.environ["GITHUB_TOKEN"] == "ghs_fresh"
    del os.environ["GITHUB_TOKEN"]


@pytest.mark.asyncio
async def test_refresh_leaves_token_untouched_on_backend_error(monkeypatch):
    """A failed refresh call keeps the previous token rather than clearing it."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_still_good")
    worker = _make_worker(github_token="ghs_still_good")

    ctx = _FakeCtx(500, {})
    with patch.object(worker, "_http", return_value=ctx):
        await worker._refresh_github_token()

    assert worker.cfg.github_token == "ghs_still_good"
    assert os.environ["GITHUB_TOKEN"] == "ghs_still_good"
    del os.environ["GITHUB_TOKEN"]


# ---------------------------------------------------------------------------
# Periodic refresh in _idle_puller
# ---------------------------------------------------------------------------


async def _run_one_idle_puller_tick(worker, fake_time):
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
        patch("asyncio.get_event_loop") as mock_loop,
    ):
        mock_loop.return_value.time.return_value = fake_time
        await worker._idle_puller()


@pytest.mark.asyncio
async def test_idle_puller_refreshes_dynamic_token_after_interval():
    """Dynamic token, interval elapsed → _refresh_github_token is called."""
    worker = _make_worker(github_token="ghs_old")
    worker._github_token_dynamic = True
    worker._last_github_token_refresh = 0.0
    worker._fetch_pending_tasks = AsyncMock(return_value=[])
    worker._refresh_github_token = AsyncMock()
    worker._refresh_github_repos = AsyncMock()

    fake_time = GITHUB_TOKEN_REFRESH_INTERVAL_SECONDS + 100
    await _run_one_idle_puller_tick(worker, fake_time)

    worker._refresh_github_token.assert_awaited()


@pytest.mark.asyncio
async def test_idle_puller_skips_refresh_before_interval():
    """Dynamic token, interval NOT elapsed → no refresh."""
    worker = _make_worker(github_token="ghs_old")
    worker._github_token_dynamic = True
    worker._last_github_token_refresh = GITHUB_TOKEN_REFRESH_INTERVAL_SECONDS + 500
    worker._fetch_pending_tasks = AsyncMock(return_value=[])
    worker._refresh_github_token = AsyncMock()
    worker._refresh_github_repos = AsyncMock()

    fake_time = worker._last_github_token_refresh + 60
    await _run_one_idle_puller_tick(worker, fake_time)

    worker._refresh_github_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_puller_never_refreshes_static_token():
    """A statically configured token (not fetched from the backend) is never
    periodically overwritten, no matter how much time has elapsed."""
    worker = _make_worker(github_token="ghp_static")
    worker._github_token_dynamic = False
    worker._last_github_token_refresh = 0.0
    worker._fetch_pending_tasks = AsyncMock(return_value=[])
    worker._refresh_github_token = AsyncMock()
    worker._refresh_github_repos = AsyncMock()

    fake_time = GITHUB_TOKEN_REFRESH_INTERVAL_SECONDS * 10
    await _run_one_idle_puller_tick(worker, fake_time)

    worker._refresh_github_token.assert_not_awaited()
