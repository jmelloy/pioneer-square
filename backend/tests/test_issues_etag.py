"""Conditional issue-page caching without a database or live GitHub access."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from routes import issues
from util import github_etag


@pytest.fixture(autouse=True)
def clear_cache():
    github_etag._cache.clear()
    yield
    github_etag._cache.clear()


async def test_conditional_get_reuses_body_and_pagination():
    requests = []
    link = '<https://api.github.com/repos/o/r/issues?page=2>; rel="next"'

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200, json=[{"number": 1}], headers={"ETag": 'W/"one"', "Link": link}
            )
        return httpx.Response(304)

    headers = issues._gh_headers("secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await github_etag.get_issue_page(client, "https://api.github.com/issues", headers)
        second = await github_etag.get_issue_page(client, "https://api.github.com/issues", headers)
    assert "if-none-match" not in requests[0].headers
    assert requests[1].headers["if-none-match"] == 'W/"one"'
    assert second.json() == first.json() == [{"number": 1}]
    assert second.links == first.links
    assert "If-None-Match" not in headers
    assert "secret" not in repr(github_etag._cache)


async def test_updated_response_replaces_etag_and_body():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(200, json=[len(requests)], headers={"ETag": str(len(requests))})
        return httpx.Response(304)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(3):
            response = await github_etag.get_issue_page(
                client, "https://api.github.com/issues", issues._gh_headers("t")
            )
    assert [r.headers.get("if-none-match") for r in requests] == [None, "1", "2"]
    assert response.json() == [2]


async def test_cache_is_scoped_to_token_url_and_ttl(monkeypatch):
    requests = []
    clock = [0.0]
    monkeypatch.setattr(github_etag, "monotonic", lambda: clock[0])

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[], headers={"ETag": '"one"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for token, url in [("a", "open"), ("b", "open"), ("a", "closed")]:
            await github_etag.get_issue_page(
                client, f"https://api.github.com/{url}", issues._gh_headers(token)
            )
        clock[0] = github_etag.CACHE_TTL + 1
        await github_etag.get_issue_page(
            client, "https://api.github.com/open", issues._gh_headers("a")
        )
    assert all("if-none-match" not in request.headers for request in requests)
    assert len(github_etag._cache) == 1


@pytest.mark.parametrize("status", [200, 403, 404, 429, 500])
async def test_uncacheable_response_does_not_reuse_old_data(status):
    responses = [
        httpx.Response(200, json=[1], headers={"ETag": '"one"'}),
        httpx.Response(status, json=[2]),
        httpx.Response(200, json=[3]),
    ]
    requests = []

    def handler(request):
        requests.append(request)
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(3):
            response = await github_etag.get_issue_page(
                client, "https://api.github.com/issues", issues._gh_headers("t")
            )
    assert response.json() == [3]
    assert "if-none-match" not in requests[-1].headers
    assert not github_etag._cache


async def test_cache_size_is_bounded(monkeypatch):
    monkeypatch.setattr(github_etag, "CACHE_MAX_PAGES", 2)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=[], headers={"ETag": "one"})
        )
    ) as client:
        for page in range(3):
            await github_etag.get_issue_page(
                client, f"https://api.github.com/issues?page={page}", issues._gh_headers("t")
            )
    assert len(github_etag._cache) == 2
    assert all("page=0" not in key[1] for key in github_etag._cache)


async def test_list_endpoint_revalidates_every_page(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        page = request.url.params["page"]
        assert request.url.params["state"] == "closed"
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        headers = {"ETag": f'"page-{page}"'}
        if page == "1":
            headers["Link"] = '<https://api.github.com/repos/o/r/issues?page=2>; rel="next"'
        return httpx.Response(200, json=[{"number": int(page)}], headers=headers)

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        issues.httpx,
        "AsyncClient",
        lambda **_: client_class(transport=httpx.MockTransport(handler)),
    )
    token = AsyncMock(return_value="t")
    monkeypatch.setattr(issues, "_get_github_token", token)
    first = await issues.list_issues("o", "r", "closed", "user", None)
    second = await issues.list_issues("o", "r", "closed", "user", None)
    assert first == second == [{"number": 1}, {"number": 2}]
    assert len(requests) == 4
    assert [r.headers.get("if-none-match") for r in requests[2:]] == ['"page-1"', '"page-2"']
    assert token.await_count == 2


async def test_list_endpoint_propagates_errors(monkeypatch):
    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda _: httpx.Response(403, headers={"x-ratelimit-remaining": "0"})
    )
    monkeypatch.setattr(issues.httpx, "AsyncClient", lambda **_: client_class(transport=transport))
    monkeypatch.setattr(issues, "_get_github_token", AsyncMock(return_value="t"))
    with pytest.raises(HTTPException, match="rate limit") as error:
        await issues.list_issues("o", "r", "all", "user", None)
    assert error.value.status_code == 403
