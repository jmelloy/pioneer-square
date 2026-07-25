"""Unit tests for GitHub App JWT/installation-token generation and caching."""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import github_app_auth  # noqa: E402
from github_app_auth import (  # noqa: E402
    DEFAULT_APP_SLUG,
    generate_jwt,
    get_app_installation_token,
    get_app_slug,
    get_github_token,
    get_installation_token,
)


def _b64url_decode(data: str) -> bytes:
    padding_needed = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding_needed)


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return private_key, pem


@pytest.fixture(autouse=True)
def _reset_token_cache(monkeypatch):
    """Clear the module-level installation-token cache and App env vars for isolation."""
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_SLUG", raising=False)
    github_app_auth._token_cache.clear()
    github_app_auth._bot_identity_cache.clear()
    yield
    github_app_auth._token_cache.clear()
    github_app_auth._bot_identity_cache.clear()


def test_generate_jwt_has_correct_claims_and_signature(rsa_keypair):
    private_key, pem = rsa_keypair
    token = generate_jwt("123456", pem)

    header_b64, payload_b64, sig_b64 = token.split(".")
    header = json.loads(_b64url_decode(header_b64))
    payload = json.loads(_b64url_decode(payload_b64))

    assert header == {"alg": "RS256", "typ": "JWT"}
    assert payload["iss"] == "123456"
    assert payload["exp"] - payload["iat"] <= 660  # ~10 min + clock-drift allowance

    signature = _b64url_decode(sig_b64)
    signing_input = f"{header_b64}.{payload_b64}".encode()
    public_key = private_key.public_key()
    # Raises InvalidSignature if the token wasn't actually signed by this key.
    public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())


def test_get_github_token_falls_back_to_env_var_when_app_not_configured(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fallback_token")
    assert get_github_token() == "ghp_fallback_token"


def test_get_github_token_falls_back_to_explicit_fallback_arg(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert get_github_token(fallback="explicit-fallback") == "explicit-fallback"


def test_get_installation_token_calls_api_with_jwt(monkeypatch, rsa_keypair):
    _private_key, pem = rsa_keypair
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"token": "ghs_installation_token", "expires_at": "2099-01-01T00:00:00Z"}

    def fake_post(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(github_app_auth.httpx, "post", fake_post)

    token = get_installation_token("123456", pem, "789")

    assert token == "ghs_installation_token"
    assert captured["url"] == "https://api.github.com/app/installations/789/access_tokens"
    assert captured["headers"]["Authorization"].startswith("Bearer ")


def test_get_app_installation_token_none_when_not_configured():
    assert get_app_installation_token() is None


def test_token_caching_avoids_refetch_within_expiry_window(monkeypatch, rsa_keypair):
    _private_key, pem = rsa_keypair
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "789")

    call_count = {"n": 0}
    far_future = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"token": f"ghs_token_{call_count['n']}", "expires_at": far_future}

    def fake_post(url, headers=None, timeout=None):
        call_count["n"] += 1
        return FakeResponse()

    monkeypatch.setattr(github_app_auth.httpx, "post", fake_post)

    first = get_github_token()
    second = get_github_token()

    assert first == second == "ghs_token_1"
    assert call_count["n"] == 1


def test_token_refetches_when_near_expiry(monkeypatch, rsa_keypair):
    _private_key, pem = rsa_keypair
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "789")

    call_count = {"n": 0}
    soon = (datetime.now(UTC) + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            call_count["n"] += 1
            return {"token": f"ghs_token_{call_count['n']}", "expires_at": soon}

    def fake_post(url, headers=None, timeout=None):
        return FakeResponse()

    monkeypatch.setattr(github_app_auth.httpx, "post", fake_post)

    first = get_github_token()
    second = get_github_token()

    assert first == "ghs_token_1"
    assert second == "ghs_token_2"
    assert call_count["n"] == 2


def test_get_github_token_prefers_app_token_over_fallback(monkeypatch, rsa_keypair):
    _private_key, pem = rsa_keypair
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "789")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_be_ignored")

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"token": "ghs_app_token", "expires_at": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(github_app_auth.httpx, "post", lambda *a, **kw: FakeResponse())

    assert get_github_token() == "ghs_app_token"


def test_app_credentials_supports_private_key_path(monkeypatch, rsa_keypair, tmp_path):
    _private_key, pem = rsa_keypair
    key_file = tmp_path / "app-key.pem"
    key_file.write_text(pem)

    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(key_file))
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "789")

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"token": "ghs_from_path", "expires_at": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(github_app_auth.httpx, "post", lambda *a, **kw: FakeResponse())

    assert get_app_installation_token() == "ghs_from_path"


def test_get_github_token_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert get_github_token() is None


def test_app_credentials_returns_none_when_private_key_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(tmp_path / "does-not-exist.pem"))
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "789")

    assert get_app_installation_token() is None


def test_concurrent_get_github_token_is_race_free(monkeypatch, rsa_keypair):
    """Two threads racing on an empty cache must not both hit the network.

    Regression test for the module-level ``_token_cache`` dict being mutated
    without a lock (jmelloy's PR #912 review comment): without
    ``_cache_lock`` serializing the check-then-fetch-then-store sequence,
    both threads would see no cached token and both would call the
    installation-token endpoint.
    """
    _private_key, pem = rsa_keypair
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "789")

    call_count = {"n": 0}
    call_lock = threading.Lock()
    far_future = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"token": "ghs_racey_token", "expires_at": far_future}

    def fake_post(url, headers=None, timeout=None):
        with call_lock:
            call_count["n"] += 1
        # Widen the race window so an unlocked cache would very likely fetch twice.
        time.sleep(0.05)
        return FakeResponse()

    monkeypatch.setattr(github_app_auth.httpx, "post", fake_post)

    results = [None, None]

    def worker(idx):
        results[idx] = get_github_token()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == ["ghs_racey_token", "ghs_racey_token"]
    assert call_count["n"] == 1
    assert github_app_auth._token_cache["789"]["token"] == "ghs_racey_token"


def test_installation_id_arg_overrides_env_and_caches_per_id(monkeypatch, rsa_keypair):
    """An explicit installation_id selects that installation and caches separately."""
    _private_key, pem = rsa_keypair
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "789")  # env default

    seen_urls = []
    far_future = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")

    class FakeResponse:
        def __init__(self, url):
            self._url = url

        def raise_for_status(self):
            pass

        def json(self):
            return {"token": f"ghs_{self._url.split('/')[-2]}", "expires_at": far_future}

    def fake_post(url, headers=None, timeout=None):
        seen_urls.append(url)
        return FakeResponse(url)

    monkeypatch.setattr(github_app_auth.httpx, "post", fake_post)

    assert get_app_installation_token("555") == "ghs_555"  # arg wins over env 789
    assert get_app_installation_token() == "ghs_789"  # env default
    assert get_app_installation_token("555") == "ghs_555"  # cached, no refetch
    assert len(seen_urls) == 2  # 555 and 789 fetched once each


def test_bot_identity_builds_noreply_email(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_SLUG", "pioneer-square-melloy[bot]")

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": 424242, "login": "pioneer-square-melloy[bot]"}

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        return FakeResponse()

    monkeypatch.setattr(github_app_auth.httpx, "get", fake_get)

    name, email = github_app_auth.get_app_bot_identity()
    assert name == "pioneer-square-melloy[bot]"
    assert email == "424242+pioneer-square-melloy[bot]@users.noreply.github.com"
    assert captured["url"].endswith("pioneer-square-melloy%5Bbot%5D")
    # Second call is cached — no second GET (would KeyError on the missing url).
    assert github_app_auth.get_app_bot_identity() == (name, email)


def test_bot_identity_none_when_slug_unset():
    assert github_app_auth.get_app_bot_identity() is None


def test_get_app_slug_defaults_when_unset():
    assert get_app_slug() == DEFAULT_APP_SLUG


def test_get_app_slug_uses_env_var_when_set(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_SLUG", "pioneer-square-melloy[bot]")
    assert get_app_slug() == "pioneer-square-melloy[bot]"
