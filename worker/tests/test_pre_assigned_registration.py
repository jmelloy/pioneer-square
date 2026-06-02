"""Tests for worker pre-assigned credentials (foreman spawn_worker path).

When PIONEER_WORKER_ID and PIONEER_AUTH_TOKEN are set in the environment the
worker should skip its self-registration HTTP call and use the pre-assigned
credentials directly.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pioneer_worker.config import Config
from pioneer_worker.worker import Worker


def _make_cfg(**kwargs) -> Config:
    return Config(
        backend_url="ws://localhost:8000",
        guild_id="test-guild",
        max_agents=1,
        pull_interval=3600.0,
        **kwargs,
    )


async def test_register_skips_http_when_preassigned():
    """_register() must not call POST /workers when worker_id + auth_token are set."""
    cfg = _make_cfg(worker_id="w-pre001", auth_token="tok-preassigned")
    worker = Worker(cfg)

    http_called = False

    async def _fake_http(*args, **kwargs):
        nonlocal http_called
        http_called = True

    with patch.object(worker, "_http", side_effect=_fake_http):
        await worker._register()

    assert not http_called, "_http should not be called when credentials are pre-assigned"
    assert worker.cfg.worker_id == "w-pre001"
    assert worker.cfg.auth_token == "tok-preassigned"


async def test_register_sets_worker_name_from_cfg_when_preassigned():
    """_register() uses cfg.worker_name when skipping self-registration."""
    cfg = _make_cfg(
        worker_id="w-pre002",
        auth_token="tok-x",
        worker_name="custom-name",
    )
    worker = Worker(cfg)
    await worker._register()
    assert worker._worker_name == "custom-name"


async def test_register_falls_back_to_worker_id_for_name():
    """When cfg.worker_name is None, _register() uses worker_id as the display name."""
    cfg = _make_cfg(worker_id="w-pre003", auth_token="tok-y")
    worker = Worker(cfg)
    await worker._register()
    assert worker._worker_name == "w-pre003"


async def test_register_calls_http_when_no_preassigned_credentials():
    """Without pre-assigned credentials, _register() performs the normal HTTP call."""
    cfg = _make_cfg()  # worker_id=None, auth_token=None
    worker = Worker(cfg)

    http_called = False

    class _FakeCtx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *args, **kwargs):
            nonlocal http_called
            http_called = True
            resp = AsyncMock()
            resp.raise_for_status = lambda: None
            resp.json.return_value = {"id": "w-new001", "auth_token": "tok-new", "name": "New"}
            return resp

    with patch.object(worker, "_http", return_value=_FakeCtx()):
        await worker._register()

    assert http_called, "_http should be called when credentials are not pre-assigned"
    assert worker.cfg.worker_id == "w-new001"
    assert worker.cfg.auth_token == "tok-new"
