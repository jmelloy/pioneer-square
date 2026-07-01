"""GitHub issue proxy routes.

Proxies GitHub API calls for issue details, comments, and updates so the
frontend can use the backend's stored OAuth token rather than holding a
raw GitHub token in localStorage indefinitely.

Routes:
  GET  /api/issues/{owner}/{repo}/{issue_number}
  POST /api/issues/{owner}/{repo}/{issue_number}/comments
  PATCH /api/issues/{owner}/{repo}/{issue_number}
"""

from __future__ import annotations

import httpx
from auth_deps import require_user
from database import get_db_dep
from fastapi import APIRouter, Depends, HTTPException
from models import GithubToken
from pydantic import BaseModel
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter()

GH_API = "https://api.github.com"


async def _get_github_token(github_user_id: str, db: AsyncSession) -> str:
    result = await db.exec(
        select(col(GithubToken.access_token)).where(
            col(GithubToken.github_user_id) == github_user_id
        )
    )
    token = result.one_or_none()
    if not token:
        raise HTTPException(status_code=401, detail="No GitHub token on file — please log in")
    return token


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


class CommentCreate(BaseModel):
    body: str


class IssueUpdate(BaseModel):
    title: str | None = None
    body: str | None = None


@router.get("/api/issues/{owner}/{repo}/{issue_number}")
async def get_issue(
    owner: str,
    repo: str,
    issue_number: int,
    github_user_id: str = Depends(require_user),
    db: AsyncSession = Depends(get_db_dep),
):
    """Fetch issue details and all comments from GitHub."""
    token = await _get_github_token(github_user_id, db)
    headers = _gh_headers(token)
    async with httpx.AsyncClient(timeout=15) as client:
        issue_res = await client.get(
            f"{GH_API}/repos/{owner}/{repo}/issues/{issue_number}", headers=headers
        )
        if not issue_res.is_success:
            raise HTTPException(
                status_code=issue_res.status_code,
                detail=f"GitHub error: {issue_res.text[:200]}",
            )
        comments_res = await client.get(
            f"{GH_API}/repos/{owner}/{repo}/issues/{issue_number}/comments?per_page=100",
            headers=headers,
        )
        if not comments_res.is_success:
            raise HTTPException(
                status_code=comments_res.status_code,
                detail=f"GitHub error: {comments_res.text[:200]}",
            )
    return {"issue": issue_res.json(), "comments": comments_res.json()}


@router.post("/api/issues/{owner}/{repo}/{issue_number}/comments")
async def post_comment(
    owner: str,
    repo: str,
    issue_number: int,
    body: CommentCreate,
    github_user_id: str = Depends(require_user),
    db: AsyncSession = Depends(get_db_dep),
):
    """Post a new comment on the issue."""
    token = await _get_github_token(github_user_id, db)
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.post(
            f"{GH_API}/repos/{owner}/{repo}/issues/{issue_number}/comments",
            headers=_gh_headers(token),
            json={"body": body.body},
        )
    if not res.is_success:
        raise HTTPException(status_code=res.status_code, detail=f"GitHub error: {res.text[:200]}")
    return res.json()


@router.patch("/api/issues/{owner}/{repo}/{issue_number}")
async def update_issue(
    owner: str,
    repo: str,
    issue_number: int,
    update: IssueUpdate,
    github_user_id: str = Depends(require_user),
    db: AsyncSession = Depends(get_db_dep),
):
    """Update issue title and/or body."""
    token = await _get_github_token(github_user_id, db)
    payload = {k: v for k, v in update.model_dump().items() if v is not None}
    if not payload:
        raise HTTPException(status_code=400, detail="Nothing to update")
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.patch(
            f"{GH_API}/repos/{owner}/{repo}/issues/{issue_number}",
            headers=_gh_headers(token),
            json=payload,
        )
    if not res.is_success:
        raise HTTPException(status_code=res.status_code, detail=f"GitHub error: {res.text[:200]}")
    return res.json()
