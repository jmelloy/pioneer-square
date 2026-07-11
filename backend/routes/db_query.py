"""Read-only DB query endpoint workers use to look up task state directly.

Lets a worker process check task state, issue linkage, branch, and PR URL
without going through the Foreman AI conversation loop. This endpoint uses
``require_any_worker_or_member_path`` — any valid worker auth_token or member
login_token authenticates the caller, regardless of which guild issued it.
Guild membership is intentionally NOT enforced here (see #879 follow-up):
this is a read-only lookup of non-sensitive task metadata, so any known
worker/member can query any guild's task state.

``query_tasks`` (GET) builds a parameterized SQLAlchemy query from named
filters and is the endpoint most callers should use.

Raw SQL and other deep-inspection queries live under ``/debug/...`` in
``routes/debug_query.py``, gated by the ``DEBUG_TOKEN`` env var instead of
worker/member auth.
"""

from __future__ import annotations

from auth_deps import get_guild_pk, require_any_worker_or_member_path
from database import get_db_dep
from fastapi import APIRouter, Depends, HTTPException
from models import Task, live_tasks_filter
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter()

_MAX_LIMIT = 500
_DEFAULT_LIMIT = 100

_TASK_COLUMNS = (
    col(Task.id),
    col(Task.name),
    col(Task.state),
    col(Task.phase),
    col(Task.branch),
    col(Task.pr_url),
    col(Task.issue_number),
    col(Task.issue_repo),
    col(Task.worker_id),
    col(Task.created_at),
    col(Task.deleted_at),
)


@router.get("/guilds/{guild_id}/db/tasks")
async def query_tasks(
    guild_id: str,
    state: str | None = None,
    phase: str | None = None,
    issue_number: int | None = None,
    issue_repo: str | None = None,
    branch: str | None = None,
    pr_url: str | None = None,
    worker_id: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    _principal: str = Depends(require_any_worker_or_member_path),
    db: AsyncSession = Depends(get_db_dep),
):
    """Filtered read-only lookup of this guild's tasks.

    All filters are ANDed together; omitted filters are not applied. Returns
    live (non-soft-deleted) tasks, most recent first.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    stmt = select(*_TASK_COLUMNS).where(col(Task.guild_id) == guild_pk, live_tasks_filter())
    if state is not None:
        stmt = stmt.where(col(Task.state) == state)
    if phase is not None:
        stmt = stmt.where(col(Task.phase) == phase)
    if issue_number is not None:
        stmt = stmt.where(col(Task.issue_number) == issue_number)
    if issue_repo is not None:
        stmt = stmt.where(col(Task.issue_repo) == issue_repo)
    if branch is not None:
        stmt = stmt.where(col(Task.branch) == branch)
    if pr_url is not None:
        stmt = stmt.where(col(Task.pr_url) == pr_url)
    if worker_id is not None:
        stmt = stmt.where(col(Task.worker_id) == worker_id)

    stmt = stmt.order_by(col(Task.created_at).desc()).limit(max(1, min(limit, _MAX_LIMIT)))
    result = await db.exec(stmt)
    return [dict(r._mapping) for r in result.all()]
