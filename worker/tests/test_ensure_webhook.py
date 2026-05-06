"""Tests for ensure_webhook (Phase 3 worker-side webhook auto-registration)."""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pioneer_worker import github_pr
from pioneer_worker.config import Config


def _make_cfg(public_backend_url: str | None = None) -> Config:
    return Config(
        backend_url="http://backend:8000",
        guild_id="g-test",
        worker_id="w-test01",
        auth_token="tok-worker",
        github_token="gh-tok",
        public_backend_url=public_backend_url,
    )


def _fake_response(payload, status=200):
    """Return a context-manager-mimicking object urlopen would yield."""
    body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.status = status
    return resp


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def test_webhook_target_url_uses_http_when_no_override():
    cfg = _make_cfg()
    assert cfg.webhook_target_url == "http://backend:8000/webhooks/github/g-test"


def test_webhook_target_url_prefers_public_url():
    cfg = _make_cfg(public_backend_url="https://tunnel.example.com")
    assert cfg.webhook_target_url == "https://tunnel.example.com/webhooks/github/g-test"


def test_webhook_target_url_strips_trailing_slash():
    cfg = _make_cfg(public_backend_url="https://tunnel.example.com/")
    assert cfg.webhook_target_url == "https://tunnel.example.com/webhooks/github/g-test"


def test_repo_from_pr_url():
    assert github_pr._repo_from_pr_url("https://github.com/owner/repo/pull/42") == "owner/repo"
    assert github_pr._repo_from_pr_url("https://github.com/o/r/pull/1?foo=bar") == "o/r"
    assert github_pr._repo_from_pr_url("") is None
    assert github_pr._repo_from_pr_url("not-a-url") is None
    assert github_pr._repo_from_pr_url("https://github.com/o/r/issues/1") is None


# ---------------------------------------------------------------------------
# ensure_webhook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_webhook_skips_without_auth_token():
    emit = AsyncMock()
    ok = await github_pr.ensure_webhook(
        repo="o/r",
        target_url="https://example/webhooks/github/g",
        http_url="http://b",
        guild_id="g",
        auth_token=None,
        github_token="gh",
        emit=emit,
    )
    assert ok is False
    emit.assert_called()


@pytest.mark.asyncio
async def test_ensure_webhook_skips_without_github_token():
    emit = AsyncMock()
    ok = await github_pr.ensure_webhook(
        repo="o/r",
        target_url="https://example/webhooks/github/g",
        http_url="http://b",
        guild_id="g",
        auth_token="tok",
        github_token=None,
        emit=emit,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_ensure_webhook_skips_when_secret_fetch_fails():
    emit = AsyncMock()
    # secret-fetch raises -> _fetch_webhook_secret returns None -> skip
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("backend down"),
    ):
        ok = await github_pr.ensure_webhook(
            repo="o/r",
            target_url="https://example/webhooks/github/g",
            http_url="http://b",
            guild_id="g",
            auth_token="tok",
            github_token="gh",
            emit=emit,
        )
    assert ok is False


@pytest.mark.asyncio
async def test_ensure_webhook_creates_when_no_existing_hook():
    emit = AsyncMock()
    secret_resp = _fake_response({"webhook_secret": "supersecret"})
    list_resp = _fake_response([])
    create_resp = _fake_response({"id": 99})
    responses = iter([secret_resp, list_resp, create_resp])
    captured: list = []

    def _fake_urlopen(req, timeout=None):
        captured.append(req)
        return next(responses)

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        ok = await github_pr.ensure_webhook(
            repo="owner/repo",
            target_url="https://example/webhooks/github/g",
            http_url="http://b",
            guild_id="g",
            auth_token="tok",
            github_token="gh",
            emit=emit,
        )
    assert ok is True
    # 3 calls: secret, list, create
    assert len(captured) == 3
    # The create body must include the events list and the secret
    create_req = captured[2]
    create_body = json.loads(create_req.data.decode())
    assert create_body["config"]["url"] == "https://example/webhooks/github/g"
    assert create_body["config"]["secret"] == "supersecret"
    assert create_body["config"]["content_type"] == "json"
    assert "pull_request" in create_body["events"]
    assert "check_run" in create_body["events"]
    assert create_body["active"] is True


@pytest.mark.asyncio
async def test_ensure_webhook_patches_existing_hook():
    emit = AsyncMock()
    secret_resp = _fake_response({"webhook_secret": "newsecret"})
    list_resp = _fake_response(
        [
            {"id": 7, "config": {"url": "https://example/webhooks/github/g"}},
            {"id": 8, "config": {"url": "https://other.example/webhook"}},
        ]
    )
    patch_resp = _fake_response({"id": 7})
    responses = iter([secret_resp, list_resp, patch_resp])
    captured: list = []

    def _fake_urlopen(req, timeout=None):
        captured.append(req)
        return next(responses)

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        ok = await github_pr.ensure_webhook(
            repo="owner/repo",
            target_url="https://example/webhooks/github/g",
            http_url="http://b",
            guild_id="g",
            auth_token="tok",
            github_token="gh",
            emit=emit,
        )
    assert ok is True
    # PATCH targets hook 7 (the matching one), not hook 8
    patch_req = captured[2]
    assert patch_req.full_url.endswith("/repos/owner/repo/hooks/7")
    assert patch_req.get_method() == "PATCH"
    body = json.loads(patch_req.data.decode())
    assert body["config"]["secret"] == "newsecret"


@pytest.mark.asyncio
async def test_ensure_webhook_handles_403_on_list():
    """No admin:repo_hook scope ⇒ 403 Forbidden. Treat as a soft failure."""
    emit = AsyncMock()
    secret_resp = _fake_response({"webhook_secret": "x"})
    forbidden = urllib.error.HTTPError(
        url="https://api.github.com/repos/o/r/hooks",
        code=403,
        msg="Forbidden",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(b""),
    )

    call_count = {"n": 0}

    def _fake_urlopen(req, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return secret_resp
        raise forbidden

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        ok = await github_pr.ensure_webhook(
            repo="o/r",
            target_url="https://example/webhooks/github/g",
            http_url="http://b",
            guild_id="g",
            auth_token="tok",
            github_token="gh",
            emit=emit,
        )
    assert ok is False
    # The 403 must surface in an emit message but never raise.
    last_emit = emit.call_args_list[-1].args[0]
    assert "403" in last_emit


@pytest.mark.asyncio
async def test_ensure_webhook_idempotent_when_create_409():
    """If list_hooks returns no match but create_hook fails (e.g. race), don't crash."""
    emit = AsyncMock()
    secret_resp = _fake_response({"webhook_secret": "x"})
    list_resp = _fake_response([])
    err = urllib.error.HTTPError(
        url="https://api.github.com/repos/o/r/hooks",
        code=422,
        msg="Validation Failed",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(b""),
    )

    call_count = {"n": 0}

    def _fake_urlopen(req, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return secret_resp
        if call_count["n"] == 2:
            return list_resp
        raise err

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        ok = await github_pr.ensure_webhook(
            repo="o/r",
            target_url="https://example/webhooks/github/g",
            http_url="http://b",
            guild_id="g",
            auth_token="tok",
            github_token="gh",
            emit=emit,
        )
    assert ok is False


@pytest.mark.asyncio
async def test_fetch_webhook_secret_returns_none_on_empty_response():
    with patch("urllib.request.urlopen", return_value=_fake_response({})):
        secret = await github_pr._fetch_webhook_secret(
            http_url="http://b", guild_id="g", auth_token="tok"
        )
    assert secret is None


@pytest.mark.asyncio
async def test_fetch_webhook_secret_strips_trailing_slash():
    captured: list = []

    def _fake_urlopen(req, timeout=None):
        captured.append(req.full_url)
        return _fake_response({"webhook_secret": "ok"})

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        await github_pr._fetch_webhook_secret(http_url="http://b/", guild_id="g", auth_token="tok")
    # No double slash in the constructed URL
    assert captured[0] == "http://b/guilds/g/webhook-secret"
