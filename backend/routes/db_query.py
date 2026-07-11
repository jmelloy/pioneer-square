"""Read-only DB query endpoints workers use to look up task state directly.

Lets a worker process check task state, issue linkage, branch, and PR URL
without going through the Foreman AI conversation loop. Both endpoints reuse
``require_worker_or_member_path`` — the same worker auth_token / member
login_token dependency other worker-facing REST endpoints use (see
``auth_deps.authorize_worker_or_member``).

``query_tasks`` (GET) builds a parameterized SQLAlchemy query from named
filters and is the endpoint most callers should use.

``raw_query_tasks`` (POST) accepts a raw SQL string for callers that need
more flexibility than the structured filters allow. Since the caller (a
worker) is less trusted than the backend itself, the raw query is restricted
to a single ``SELECT * FROM tasks ...`` statement — no writes, no other
tables, no multiple statements — and always wrapped in a guild_id filter so
a worker can never read another guild's tasks even without its own WHERE
clause. This is regex-based validation, not a full SQL parser, so it's
deliberately conservative: anything it can't prove safe is rejected.
"""

from __future__ import annotations

import re

from auth_deps import get_guild_pk, require_worker_or_member_path
from database import get_db_dep
from fastapi import APIRouter, Depends, HTTPException
from models import Task, live_tasks_filter
from pydantic import BaseModel
from sqlalchemy import text
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
    _principal: str = Depends(require_worker_or_member_path),
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


# ---------------------------------------------------------------------------
# Raw SQL option — validated to a single read-only "SELECT * FROM tasks ..."
# ---------------------------------------------------------------------------

_ALLOWED_TABLE = "tasks"

# Anything that mutates data or schema, plus a few functions that would let a
# read-only SELECT still do damage (sleep-based DoS, file/large-object access).
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|MERGE|CALL|"
    r"EXEC|EXECUTE|COPY|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|LOCK|INTO|COMMENT|DO|"
    r"PG_SLEEP|PG_READ_FILE|PG_READ_BINARY_FILE|PG_LS_DIR|LO_IMPORT|LO_EXPORT|"
    r"LO_GET|LO_PUT|DBLINK|SET)\b",
    re.IGNORECASE,
)
_TABLE_REF_RE = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)
_SELECT_STAR_FROM_TASKS_RE = re.compile(r"^\s*SELECT\s+\*\s+FROM\s+tasks\b", re.IGNORECASE)


class DbRawQueryRequest(BaseModel):
    sql: str


def _validate_readonly_tasks_query(sql: str) -> str:
    """Return the validated, semicolon-stripped query, or raise HTTPException(400).

    Requires a single statement of the exact shape ``SELECT * FROM tasks ...``
    (optional WHERE/ORDER BY/LIMIT), no comments, no bind-marker-colliding
    colons, no forbidden keywords, and no reference to any table but
    ``tasks`` anywhere in the statement (including in subqueries).
    """
    stripped = sql.strip()
    if not stripped:
        raise HTTPException(status_code=400, detail="sql must not be empty")
    if "--" in stripped or "/*" in stripped:
        raise HTTPException(status_code=400, detail="SQL comments are not allowed")
    if ":" in stripped:
        raise HTTPException(status_code=400, detail="':' is not allowed in the query text")
    # Allow one optional trailing semicolon, but not a second statement.
    body = stripped[:-1].strip() if stripped.endswith(";") else stripped
    if ";" in body:
        raise HTTPException(status_code=400, detail="Only a single statement is allowed")
    if _FORBIDDEN_KEYWORDS.search(body):
        raise HTTPException(status_code=400, detail="Only read-only SELECT queries are allowed")
    if not _SELECT_STAR_FROM_TASKS_RE.match(body):
        raise HTTPException(
            status_code=400,
            detail="sql must be of the form 'SELECT * FROM tasks ...'",
        )
    tables = {m.group(1).lower() for m in _TABLE_REF_RE.finditer(body)}
    if tables - {_ALLOWED_TABLE}:
        raise HTTPException(status_code=400, detail="Only the tasks table may be queried")
    return body


@router.post("/guilds/{guild_id}/db/query")
async def raw_query_tasks(
    guild_id: str,
    data: DbRawQueryRequest,
    _principal: str = Depends(require_worker_or_member_path),
    db: AsyncSession = Depends(get_db_dep),
):
    """Raw read-only SQL lookup, restricted to 'SELECT * FROM tasks ...'.

    The validated query is wrapped and filtered by this guild's id
    server-side, so a caller can never read another guild's tasks even
    without an explicit ``WHERE guild_id = ...`` clause of their own.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    validated = _validate_readonly_tasks_query(data.sql)
    # SET LOCAL is transaction-scoped — bounds how long a caller-supplied
    # query can run without affecting other sessions.
    await db.exec(text("SET LOCAL statement_timeout = '2000ms'"))
    wrapped = text(
        f"SELECT * FROM ({validated}) AS raw_query "
        "WHERE raw_query.guild_id = :guild_pk LIMIT :limit"
    )
    result = await db.exec(wrapped, params={"guild_pk": guild_pk, "limit": _MAX_LIMIT})
    return [dict(r._mapping) for r in result.all()]
