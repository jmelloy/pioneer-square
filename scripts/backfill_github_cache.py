#!/usr/bin/env python3
"""Backfill the github_issues / github_pull_requests DB cache (#864).

Paginates the GitHub REST API for issues and pull requests on one or more
repos and upserts each into the local cache via backend/db/github_cache.py.
Useful for seeding the cache for a repo that predates webhook coverage, or
for re-syncing after downtime.

Usage:
    python scripts/backfill_github_cache.py --repo owner/repo [--repo owner/repo2] [--token ghp_...]

GITHUB_TOKEN env var is used when --token is omitted. Requires DATABASE_URL
to be set (same variable the backend reads).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from database import AsyncSessionLocal  # noqa: E402
from db import github_cache  # noqa: E402

GH_API = "https://api.github.com"
PER_PAGE = 100


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _paginate(client: httpx.AsyncClient, url: str, params: dict) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        res = await client.get(url, params={**params, "page": page, "per_page": PER_PAGE})
        res.raise_for_status()
        batch = res.json()
        if not batch:
            break
        items.extend(batch)
        if len(batch) < PER_PAGE:
            break
        page += 1
    return items


async def backfill_repo(client: httpx.AsyncClient, repo: str) -> None:
    # /issues returns both issues and PRs (PRs carry a "pull_request" key);
    # filter those out since PRs are backfilled separately from /pulls.
    print(f"[{repo}] fetching issues...")
    raw_issues = await _paginate(client, f"{GH_API}/repos/{repo}/issues", {"state": "all"})
    issues = [item for item in raw_issues if "pull_request" not in item]
    print(f"[{repo}] fetched {len(issues)} issues ({len(raw_issues) - len(issues)} PRs excluded)")

    print(f"[{repo}] fetching pull requests...")
    prs = await _paginate(client, f"{GH_API}/repos/{repo}/pulls", {"state": "all"})
    print(f"[{repo}] fetched {len(prs)} pull requests")

    async with AsyncSessionLocal() as db:
        for i, issue in enumerate(issues, start=1):
            await github_cache.upsert_issue(db, repo, issue)
            print(f"[{repo}] upserted issue #{issue['number']} ({i}/{len(issues)})")
        for i, pr in enumerate(prs, start=1):
            await github_cache.upsert_pr(db, repo, pr)
            print(f"[{repo}] upserted PR #{pr['number']} ({i}/{len(prs)})")


async def main(repos: list[str], token: str | None) -> None:
    async with httpx.AsyncClient(headers=_headers(token), timeout=30) as client:
        for repo in repos:
            await backfill_repo(client, repo)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", action="append", required=True, dest="repos", help="owner/repo (repeatable)"
    )
    parser.add_argument(
        "--token", default=None, help="GitHub token (defaults to GITHUB_TOKEN env var)"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    token = args.token or os.environ.get("GITHUB_TOKEN")
    asyncio.run(main(args.repos, token))
