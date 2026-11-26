"""Bounded, credential-scoped conditional GET cache for GitHub issue pages."""

from __future__ import annotations

from collections import OrderedDict
from hashlib import sha256
from time import monotonic

import httpx

CACHE_TTL = 300
CACHE_MAX_PAGES = 256
_cache: OrderedDict[tuple[str, str], tuple[float, httpx.Response]] = OrderedDict()


async def get_issue_page(
    client: httpx.AsyncClient, url: str, headers: dict[str, str]
) -> httpx.Response:
    # Never share private responses between credentials, or keep raw tokens in cache keys.
    key = (sha256(headers["Authorization"].encode()).hexdigest(), url)
    now = monotonic()
    while _cache and next(iter(_cache.values()))[0] <= now:
        _cache.popitem(last=False)
    cached = _cache.get(key)
    request_headers = headers.copy()
    if cached:
        request_headers["If-None-Match"] = cached[1].headers["etag"]
    response = await client.get(url, headers=request_headers)
    if response.status_code == 304 and cached:
        # Link is needed even on a 304 to continue through cached pagination.
        response = httpx.Response(200, content=cached[1].content, headers=cached[1].headers)
    elif response.status_code != 200:
        _cache.pop(key, None)
        return response
    if response.status_code == 200 and response.headers.get("etag"):
        # Store only response data and pagination metadata, not the authenticated request.
        stored = httpx.Response(
            200,
            content=response.content,
            headers={k: v for k, v in response.headers.items() if k in ("etag", "link")},
        )
        _cache[key] = (monotonic() + CACHE_TTL, stored)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX_PAGES:
            _cache.popitem(last=False)
    else:
        _cache.pop(key, None)
    return response
