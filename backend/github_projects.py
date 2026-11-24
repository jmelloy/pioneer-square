"""Cache of linked GitHub Projects' issues (#1513).

``sync_project`` pages a Projects v2 board through GraphQL, upserts each issue
into the shared ``github_issues`` cache (``db.github_cache.upsert_issue``) and
records membership + Status in ``github_project_items``. ``list_project_issues``
serves the cache to the UI; ``project_sync_sweeper`` refreshes every guild's
linked projects in the background.

Classic Projects are sunset by GitHub and have no item API left, so they are
not synced; the UI keeps linking out for them.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

import database
import httpx
from db.github_cache import upsert_issue
from models import GithubIssue, GithubProjectItem, GithubToken, Guild, GuildMember
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger(__name__)

GH_GRAPHQL = "https://api.github.com/graphql"
PROJECT_SYNC_INTERVAL_SECONDS = float(os.environ.get("PROJECT_SYNC_INTERVAL_SECONDS", "600"))
_MAX_PAGES = 20  # 2000 items; bigger boards are truncated

_ITEMS_QUERY = """
query($id: ID!, $after: String) {
  node(id: $id) {
    ... on ProjectV2 {
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          status: fieldValueByName(name: "Status") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          content {
            ... on Issue {
              number title body state url createdAt updatedAt closedAt
              author { login }
              milestone { title }
              labels(first: 20) { nodes { name color } }
              assignees(first: 10) { nodes { login } }
              repository { nameWithOwner }
            }
          }
        }
      }
    }
  }
}
"""


class ProjectSyncError(Exception):
    pass


def _rest_payload(issue: dict) -> dict:
    """Shape a GraphQL Issue like the REST payload ``upsert_issue`` expects."""
    return {
        "number": issue["number"],
        "title": issue.get("title") or "",
        "body": issue.get("body"),
        "state": (issue.get("state") or "OPEN").lower(),
        "html_url": issue.get("url"),
        "created_at": issue.get("createdAt"),
        "updated_at": issue.get("updatedAt"),
        "closed_at": issue.get("closedAt"),
        "user": issue.get("author"),
        "milestone": issue.get("milestone"),
        "labels": (issue.get("labels") or {}).get("nodes") or [],
        "assignees": (issue.get("assignees") or {}).get("nodes") or [],
    }


async def _fetch_items(project_id: str, token: str) -> list[tuple[str, dict, str | None]]:
    """Return ``(repo, rest_payload, status)`` for every issue on the board."""
    out: list[tuple[str, dict, str | None]] = []
    after: str | None = None
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(_MAX_PAGES):
            res = await client.post(
                GH_GRAPHQL,
                headers={"Authorization": f"Bearer {token}"},
                json={"query": _ITEMS_QUERY, "variables": {"id": project_id, "after": after}},
            )
            if not res.is_success:
                raise ProjectSyncError(f"GitHub error {res.status_code} syncing project")
            body = res.json()
            conn = ((body.get("data") or {}).get("node") or {}).get("items")
            if conn is None:
                msg = (body.get("errors") or [{}])[0].get("message", "project not accessible")
                raise ProjectSyncError(f"GitHub: {msg}")
            for node in conn.get("nodes") or []:
                content = (node or {}).get("content") or {}
                repo = (content.get("repository") or {}).get("nameWithOwner")
                if not repo or "number" not in content:
                    continue  # draft issue or pull request
                status = (node.get("status") or {}).get("name")
                out.append((repo, _rest_payload(content), status))
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
    return out


async def sync_project(db: AsyncSession, project_id: str, token: str) -> int:
    """Refresh one Projects v2 board into the cache. Returns the issue count."""
    items = await _fetch_items(project_id, token)
    now = datetime.now(UTC)
    for position, (repo, payload, status) in enumerate(items):
        await upsert_issue(db, repo, payload)
        values = {
            "project_id": project_id,
            "repo": repo,
            "number": payload["number"],
            "status": status,
            "position": position,
            "synced_at": now,
        }
        stmt = pg_insert(GithubProjectItem).values(**values)
        await db.exec(
            stmt.on_conflict_do_update(
                index_elements=["project_id", "repo", "number"],
                set_={k: stmt.excluded[k] for k in ("status", "position", "synced_at")},
            )
        )
    # Anything not seen this pass has left the board.
    await db.exec(
        delete(GithubProjectItem).where(
            col(GithubProjectItem.project_id) == project_id,
            col(GithubProjectItem.synced_at) < now,
        )
    )
    await db.commit()
    return len(items)


async def list_project_issues(db: AsyncSession, project_id: str) -> list[dict]:
    """Cached issues on a project, in board order, shaped like the UI's GitHubIssue."""
    rows = (
        await db.exec(
            select(GithubProjectItem, GithubIssue)
            .join(
                GithubIssue,
                (col(GithubIssue.repo) == col(GithubProjectItem.repo))
                & (col(GithubIssue.number) == col(GithubProjectItem.number)),
            )
            .where(col(GithubProjectItem.project_id) == project_id)
            .order_by(col(GithubProjectItem.position))
        )
    ).all()
    return [
        {
            "id": issue.id,
            "number": issue.number,
            "title": issue.title,
            "repo": issue.repo,
            "state": issue.state,
            "status": item.status,
            "labels": [
                {"name": lb.get("name"), "color": lb.get("color", "")}
                for lb in (issue.raw.get("labels") or [])
                if isinstance(lb, dict)
            ],
            "assignees": issue.assignees,
            "html_url": issue.raw.get("html_url"),
            "created_at": issue.created_at.isoformat(),
            "updated_at": issue.updated_at.isoformat(),
            "synced_at": item.synced_at.isoformat(),
        }
        for item, issue in rows
    ]


async def _owner_token(db: AsyncSession, guild_pk: int) -> str | None:
    # The owner's OAuth token carries the `project` scope; App installation
    # tokens can't read user-owned boards.
    return (
        await db.exec(
            select(col(GithubToken.access_token))
            .join(GuildMember, col(GuildMember.user_id) == col(GithubToken.github_user_id))
            .where(col(GuildMember.guild_id) == guild_pk, col(GuildMember.role) == "owner")
            .limit(1)
        )
    ).first()


async def sync_all_projects_once() -> int:
    """Sync every guild's linked Projects v2 boards. Returns boards synced."""
    synced = 0
    async with database.AsyncSessionLocal() as db:
        guilds = (await db.exec(select(Guild).where(col(Guild.browse_projects).is_not(None)))).all()
        for guild in guilds:
            projects = [p for p in guild.browse_projects or [] if p.get("kind") == "v2"]
            if not projects:
                continue
            token = await _owner_token(db, guild.id)
            if not token:
                continue
            for project in projects:
                try:
                    await sync_project(db, project["id"], token)
                    synced += 1
                except Exception:
                    await db.rollback()
                    logger.warning(
                        "guild=%s failed to sync project %s",
                        guild.slug,
                        project["id"],
                        exc_info=True,
                    )
    return synced


async def project_sync_sweeper() -> None:
    """Background task: keep linked projects' cached issues fresh."""
    while True:
        try:
            await asyncio.sleep(PROJECT_SYNC_INTERVAL_SECONDS)
            await sync_all_projects_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Project sync sweeper iteration failed")
