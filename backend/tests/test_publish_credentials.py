"""App publication never uses personal credentials or another installation."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import github_app_auth as app_auth
import httpx
import pytest


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(app_auth, "_publish_token_cache", {})
    monkeypatch.setattr(app_auth, "_app_key_credentials", lambda: ("app-id", "private-key"))
    monkeypatch.setattr(app_auth, "generate_jwt", lambda *_: "app-jwt")
    installation = {
        "id": 123,
        "app_slug": "pioneer-square",
        "permissions": {"contents": "write", "workflows": "write"},
    }
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    get = Mock(
        return_value=httpx.Response(
            200, json=installation, request=httpx.Request("GET", "https://api.github.com")
        )
    )
    post = Mock(
        return_value=httpx.Response(
            201,
            json={"token": "scoped", "expires_at": expiry},
            request=httpx.Request("POST", "https://api.github.com"),
        )
    )
    monkeypatch.setattr(app_auth.httpx, "get", get)
    monkeypatch.setattr(app_auth.httpx, "post", post)
    return get, post


def test_scopes_to_one_repository_and_keeps_workflow_permission(app):
    result = app_auth.get_app_publish_credentials("owner/repo", "123")
    assert result["access_token"] == "scoped"
    assert result["username"] == "pioneer-square[bot]"
    assert app[0].call_args.args == ("https://api.github.com/repos/owner/repo/installation",)
    assert app[1].call_args.kwargs["json"] == {
        "repositories": ["repo"],
        "permissions": {"contents": "write", "workflows": "write"},
    }
    assert app[1].call_args.kwargs["headers"]["Authorization"] == "Bearer app-jwt"


def test_does_not_request_ungranted_workflow_access(app):
    app[0].return_value = httpx.Response(
        200,
        json={"id": 123, "app_slug": "app", "permissions": {"contents": "write"}},
        request=httpx.Request("GET", "https://api.github.com"),
    )
    app_auth.get_app_publish_credentials("owner/repo", "123")
    assert app[1].call_args.kwargs["json"]["permissions"] == {"contents": "write"}


def test_other_installation_cannot_mint_token(app):
    with pytest.raises(ValueError, match="different"):
        app_auth.get_app_publish_credentials("owner/repo", "456")
    app[1].assert_not_called()


def test_personal_token_is_not_a_fallback(app, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "personal-token")
    monkeypatch.setattr(app_auth, "_app_key_credentials", lambda: None)
    with pytest.raises(ValueError, match="App is not configured"):
        app_auth.get_app_publish_credentials("owner/repo", "123")
    app[1].assert_not_called()


def test_cache_is_repository_scoped_and_refreshes_before_expiry(app):
    result = app_auth.get_app_publish_credentials("owner/repo", "123")
    result["access_token"] = "caller-mutation"
    assert app_auth.get_app_publish_credentials("owner/repo", "123")["access_token"] == "scoped"
    assert app[1].call_count == 1
    app_auth.get_app_publish_credentials("owner/second", "123")
    assert app[1].call_count == 2
    app_auth._publish_token_cache[("123", "owner/repo")]["expires_at"] = datetime.now(
        UTC
    ) + timedelta(minutes=1)
    app_auth.get_app_publish_credentials("owner/repo", "123")
    assert app[1].call_count == 3
