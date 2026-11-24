"""Linked GitHub Project issues are served from the DB cache and resynced (#1513)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import github_projects
from helpers import insert_guild, make_auth_token

V2 = {"id": "PVT_1", "name": "Roadmap", "url": "https://x/p/1", "owner": "team", "kind": "v2"}
CLASSIC = {"id": "classic-9", "name": "Old", "url": "https://x/p/9", "owner": "team", "kind": "org"}


def _issue(number: int, title: str) -> dict:
    return {
        "number": number,
        "title": title,
        "state": "open",
        "html_url": f"https://github.com/team/app/issues/{number}",
        "created_at": "2026-11-01T00:00:00Z",
        "updated_at": "2026-11-02T00:00:00Z",
        "labels": [{"name": "bug", "color": "d73a4a"}],
    }


async def test_project_issues_cached_and_resynced(client, monkeypatch):
    test_client, db_url = client
    h = {"Authorization": f"Bearer {make_auth_token(db_url)}"}
    insert_guild(db_url, "g-pi1")
    base = "/api/guilds/g-pi1/projects"
    assert (
        test_client.put(
            "/api/guilds/g-pi1/browse-projects", headers=h, json={"projects": [V2, CLASSIC]}
        ).status_code
        == 200
    )

    board = [("team/app", _issue(1, "One"), "Todo"), ("team/app", _issue(2, "Two"), "Done")]
    calls = []

    async def fake_fetch(project_id, token):
        calls.append(project_id)
        return list(board)

    monkeypatch.setattr(github_projects, "_fetch_items", fake_fetch)

    # First view fills the empty cache; the second is served from the DB.
    issues = test_client.get(f"{base}/PVT_1/issues", headers=h).json()["issues"]
    assert [(i["number"], i["status"]) for i in issues] == [(1, "Todo"), (2, "Done")]
    assert issues[0]["labels"] == [{"name": "bug", "color": "d73a4a"}]
    assert issues[0]["html_url"].endswith("/issues/1")
    test_client.get(f"{base}/PVT_1/issues", headers=h)
    assert calls == ["PVT_1"]

    # A manual sync drops items that left the board and picks up changes.
    board[:] = [("team/app", _issue(2, "Two renamed"), "In Progress")]
    issues = test_client.post(f"{base}/PVT_1/issues/sync", headers=h).json()["issues"]
    assert [(i["number"], i["title"], i["status"]) for i in issues] == [
        (2, "Two renamed", "In Progress")
    ]

    # The background sweep resyncs every linked v2 board, never classic ones.
    calls.clear()
    assert await github_projects.sync_all_projects_once() >= 1
    assert "PVT_1" in calls and "classic-9" not in calls

    assert test_client.get(f"{base}/classic-9/issues", headers=h).status_code == 400
    assert test_client.get(f"{base}/PVT_other/issues", headers=h).status_code == 404


async def test_fetch_items_keeps_issues_and_pages(monkeypatch):
    import httpx

    issue = {
        "number": 7,
        "title": "T",
        "state": "CLOSED",
        "url": "u",
        "repository": {"nameWithOwner": "team/app"},
        "labels": {"nodes": [{"name": "bug", "color": "f00"}]},
        "assignees": {"nodes": [{"login": "amy"}]},
    }
    pages = [
        {"hasNextPage": True, "endCursor": "c1"},
        {"hasNextPage": False, "endCursor": None},
    ]
    seen_after = []

    async def fake_post(self, url, headers, json):
        seen_after.append(json["variables"]["after"])
        nodes = [{"status": {"name": "Done"}, "content": issue}, {"status": None, "content": {}}]
        page = pages[len(seen_after) - 1]
        body = {"data": {"node": {"items": {"pageInfo": page, "nodes": nodes}}}}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    items = await github_projects._fetch_items("PVT_1", "tok")
    assert seen_after == [None, "c1"]
    assert len(items) == 2  # the draft (empty content) is skipped on each page
    repo, payload, status = items[0]
    assert (repo, payload["state"], status) == ("team/app", "closed", "Done")
    assert payload["labels"] == [{"name": "bug", "color": "f00"}]
    assert payload["assignees"] == [{"login": "amy"}]
