"""Local DB cache of GitHub issue/PR state (#864).

Upsert helpers translate a raw GitHub REST/webhook payload into a row in
``github_issues`` / ``github_pull_requests``, keyed unique on (repo, number)
so repeated deliveries (webhook redelivery, backfill re-runs) are idempotent.
The full payload is retained in ``raw`` for future-proofing.
"""

from __future__ import annotations

from datetime import datetime

from models import GithubIssue, GithubPullRequest
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _login(obj: dict | None) -> str | None:
    return obj.get("login") if isinstance(obj, dict) else None


def _label_names(payload: dict) -> list[str]:
    labels = payload.get("labels") or []
    return [lb["name"] for lb in labels if isinstance(lb, dict) and lb.get("name")]


def _assignee_logins(payload: dict) -> list[str]:
    assignees = payload.get("assignees") or []
    return [a["login"] for a in assignees if isinstance(a, dict) and a.get("login")]


async def upsert_issue(db: AsyncSession, repo: str, payload: dict) -> GithubIssue:
    """Insert or update the cached issue row for *repo* from a GitHub issue payload."""
    milestone = payload.get("milestone")
    values = {
        "repo": repo,
        "number": payload["number"],
        "title": payload.get("title") or "",
        "body": payload.get("body"),
        "state": payload.get("state") or "open",
        "assignees": _assignee_logins(payload),
        "labels": _label_names(payload),
        "milestone": milestone.get("title") if isinstance(milestone, dict) else None,
        "author": _login(payload.get("user")),
        "created_at": _parse_dt(payload.get("created_at")),
        "updated_at": _parse_dt(payload.get("updated_at")),
        "closed_at": _parse_dt(payload.get("closed_at")),
        "raw": payload,
    }
    stmt = pg_insert(GithubIssue).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["repo", "number"],
        set_={k: stmt.excluded[k] for k in values if k not in ("repo", "number")},
    )
    await db.exec(stmt)
    await db.commit()
    result = await db.exec(
        select(GithubIssue).where(
            col(GithubIssue.repo) == repo, col(GithubIssue.number) == payload["number"]
        )
    )
    return result.one()


async def upsert_pr(db: AsyncSession, repo: str, payload: dict) -> GithubPullRequest:
    """Insert or update the cached PR row for *repo* from a GitHub pull_request payload."""
    head = payload.get("head") or {}
    base = payload.get("base") or {}
    # The REST "list pull requests" endpoint omits the "merged" boolean present
    # on webhook/single-PR payloads, so fall back to merged_at as the signal.
    merged = bool(payload.get("merged")) or payload.get("merged_at") is not None
    state = "merged" if merged else (payload.get("state") or "open")
    values = {
        "repo": repo,
        "number": payload["number"],
        "title": payload.get("title") or "",
        "body": payload.get("body"),
        "state": state,
        "merged": merged,
        "draft": bool(payload.get("draft")),
        "head_sha": head.get("sha"),
        "head_ref": head.get("ref"),
        "base_ref": base.get("ref"),
        "author": _login(payload.get("user")),
        "assignees": _assignee_logins(payload),
        "labels": _label_names(payload),
        "created_at": _parse_dt(payload.get("created_at")),
        "updated_at": _parse_dt(payload.get("updated_at")),
        "closed_at": _parse_dt(payload.get("closed_at")),
        "merged_at": _parse_dt(payload.get("merged_at")),
        "raw": payload,
    }
    stmt = pg_insert(GithubPullRequest).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["repo", "number"],
        set_={k: stmt.excluded[k] for k in values if k not in ("repo", "number")},
    )
    await db.exec(stmt)
    await db.commit()
    result = await db.exec(
        select(GithubPullRequest).where(
            col(GithubPullRequest.repo) == repo, col(GithubPullRequest.number) == payload["number"]
        )
    )
    return result.one()


async def get_issue(db: AsyncSession, repo: str, number: int) -> GithubIssue | None:
    result = await db.exec(
        select(GithubIssue).where(col(GithubIssue.repo) == repo, col(GithubIssue.number) == number)
    )
    return result.one_or_none()


async def get_pr(db: AsyncSession, repo: str, number: int) -> GithubPullRequest | None:
    result = await db.exec(
        select(GithubPullRequest).where(
            col(GithubPullRequest.repo) == repo, col(GithubPullRequest.number) == number
        )
    )
    return result.one_or_none()


async def list_issues(
    db: AsyncSession, repo: str, state: str | None = None, label: str | None = None
) -> list[GithubIssue]:
    """List cached issues for *repo*, optionally filtered by state and/or label name."""
    stmt = select(GithubIssue).where(col(GithubIssue.repo) == repo)
    if state:
        stmt = stmt.where(col(GithubIssue.state) == state)
    result = await db.exec(stmt.order_by(col(GithubIssue.number).desc()))
    issues = result.all()
    if label:
        issues = [i for i in issues if label in i.labels]
    return issues


async def list_prs(
    db: AsyncSession, repo: str, state: str | None = None
) -> list[GithubPullRequest]:
    """List cached pull requests for *repo*, optionally filtered by state."""
    stmt = select(GithubPullRequest).where(col(GithubPullRequest.repo) == repo)
    if state:
        stmt = stmt.where(col(GithubPullRequest.state) == state)
    result = await db.exec(stmt.order_by(col(GithubPullRequest.number).desc()))
    return result.all()
