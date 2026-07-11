"""Deep-inspection DB endpoints gated by the ``DEBUG_TOKEN`` env var.

These are the "dangerous" counterparts to ``routes/db_query.py``'s safe,
structured task lookup: raw SQL and full (including soft-deleted) row dumps
across a wider set of operational tables. They are for operator/deep-debug
use, not for the worker's day-to-day task-state polling.

Only mounted when ``DEBUG_TOKEN`` is set (see the conditional
``app.include_router`` in ``main.py``) — if the env var is absent, these
routes simply don't exist. Every endpoint also depends on
``auth_deps.require_debug_token``, which checks the ``Authorization: Bearer``
or ``X-Debug-Token`` header against that same env var.

Unlike ``routes/db_query.py``, these endpoints are NOT guild-scoped: the
debug token is an operator-wide credential, so callers may query across
guilds. Raw SQL is still restricted to read-only SELECTs against an allow-
listed set of operational tables (no credential-bearing tables like
``github_tokens`` or ``user_sessions``).
"""

from __future__ import annotations

import re

from auth_deps import get_guild_pk, require_debug_token
from database import get_db_dep
from fastapi import APIRouter, Depends, HTTPException
from models import Task, TaskLog, live_tasks_filter
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from utils import row_to_dict

router = APIRouter(prefix="/debug", dependencies=[Depends(require_debug_token)])

_MAX_LIMIT = 500
_DEFAULT_LIMIT = 100


# ---------------------------------------------------------------------------
# Full task dump — every column, every guild, including soft-deleted rows.
# ---------------------------------------------------------------------------


@router.get("/tasks")
async def debug_list_tasks(
    guild_id: str | None = None,
    state: str | None = None,
    phase: str | None = None,
    issue_number: int | None = None,
    issue_repo: str | None = None,
    branch: str | None = None,
    pr_url: str | None = None,
    worker_id: str | None = None,
    include_deleted: bool = True,
    limit: int = _DEFAULT_LIMIT,
    db: AsyncSession = Depends(get_db_dep),
):
    """Full-column task dump across all guilds, for deep debugging.

    Unlike ``GET /guilds/{guild_id}/db/tasks``, this returns every task
    column (not just the safe subset) and includes soft-deleted rows by
    default.
    """
    stmt = select(Task)
    if guild_id is not None:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        stmt = stmt.where(col(Task.guild_id) == guild_pk)
    if not include_deleted:
        stmt = stmt.where(live_tasks_filter())
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
    return [row_to_dict(r) for r in result.all()]


@router.get("/tasks/{task_id}/logs")
async def debug_task_logs(
    task_id: str,
    limit: int = _DEFAULT_LIMIT,
    db: AsyncSession = Depends(get_db_dep),
):
    """Raw agent/worker log lines for a task, for deep debugging."""
    result = await db.exec(
        select(TaskLog)
        .where(col(TaskLog.task_id) == task_id)
        .order_by(col(TaskLog.id).asc())
        .limit(max(1, min(limit, _MAX_LIMIT)))
    )
    return [row_to_dict(r) for r in result.all()]


# ---------------------------------------------------------------------------
# Raw SQL option — validated to a single read-only SELECT against an
# allow-listed set of operational tables.
# ---------------------------------------------------------------------------

# Tables safe to expose to a raw SELECT: operational/state tables only — no
# credential-bearing tables (github_tokens, user_sessions, claude_credentials,
# guild_keys, discord_connect_tokens, discord_account_links, push_tokens).
_ALLOWED_TABLES = frozenset(
    {
        "tasks",
        "task_logs",
        "task_events",
        "workers",
        "agents",
        "github_events",
        "llm_usage",
        "foreman_turns",
    }
)

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
_SELECT_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)


class DebugRawQueryRequest(BaseModel):
    sql: str


def _validate_readonly_query(sql: str) -> str:
    """Return the validated, semicolon-stripped query, or raise HTTPException(400).

    Requires a single ``SELECT ...`` statement, no comments, no bind-marker-
    colliding colons, no forbidden keywords, and no reference to any table
    outside ``_ALLOWED_TABLES`` anywhere in the statement (including in
    subqueries).
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
    if not _SELECT_RE.match(body):
        raise HTTPException(status_code=400, detail="sql must be a SELECT statement")
    tables = {m.group(1).lower() for m in _TABLE_REF_RE.finditer(body)}
    if tables - _ALLOWED_TABLES:
        raise HTTPException(
            status_code=400,
            detail=f"Only these tables may be queried: {', '.join(sorted(_ALLOWED_TABLES))}",
        )
    return body


@router.post("/query")
async def debug_raw_query(
    data: DebugRawQueryRequest,
    db: AsyncSession = Depends(get_db_dep),
):
    """Raw read-only SQL lookup across the allow-listed operational tables.

    Not guild-scoped — the debug token is an operator-wide credential.
    """
    validated = _validate_readonly_query(data.sql)
    # SET LOCAL is transaction-scoped — bounds how long a caller-supplied
    # query can run without affecting other sessions.
    await db.exec(text("SET LOCAL statement_timeout = '2000ms'"))
    wrapped = text(f"SELECT * FROM ({validated}) AS debug_query LIMIT :limit")
    result = await db.exec(wrapped, params={"limit": _MAX_LIMIT})
    return [dict(r._mapping) for r in result.all()]
