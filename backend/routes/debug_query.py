"""Read-only raw-SQL debug endpoint, gated by the ``DEBUG_TOKEN`` env var.

Lets an operator run an ad-hoc read-only SELECT against an allow-listed set
of operational tables, for deep debugging without shelling into the
database directly.

Only mounted when ``DEBUG_TOKEN`` is set (see the conditional
``app.include_router`` in ``main.py``) — if the env var is absent, this
route simply doesn't exist. It depends on ``auth_deps.require_debug_token``,
which checks the ``Authorization: Bearer`` or ``X-Debug-Token`` header
against that same env var.

Not guild-scoped: the debug token is an operator-wide credential, so callers
may query across guilds.

Defense in depth (there is no dedicated read-only DB role, so these are the
whole safety story):
  * ``SET TRANSACTION READ ONLY`` — Postgres itself rejects every write and
    write-side function (INSERT/UPDATE/DELETE/DDL, nextval, lo_import, …). This
    is a DB guarantee, not a string match, so it can't be evaded by CTE-wrapped
    DML or creative casing the way a keyword blocklist can.
  * Single ``SELECT`` statement, no comments, no stacked statements, wrapped in
    ``SELECT * FROM (<q>) LIMIT n`` — keeps the input to one bounded read.
  * ``_ALLOWED_TABLES`` — the only thing keeping credential-bearing tables
    (github_tokens, user_sessions, …) out of reach.
  * ``_FORBIDDEN_FUNCTIONS`` — the read-only transaction does NOT stop pure-read
    disclosure functions (pg_read_file, pg_ls_dir, dblink, lo_get), so those
    stay explicitly blocked.
  * ``statement_timeout`` — bounds runtime (covers the pg_sleep DoS case).
"""

from __future__ import annotations

import re

from auth_deps import require_debug_token
from database import get_db_dep
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter(prefix="/debug", dependencies=[Depends(require_debug_token)])

_MAX_LIMIT = 500

# Bounds how long a caller-supplied query can run — set via SET LOCAL
# statement_timeout below, so it's transaction-scoped and doesn't affect other
# sessions.
_STATEMENT_TIMEOUT_MS = 30_000

# Tables safe to expose to a raw SELECT: operational/state tables only — no
# credential-bearing tables (github_tokens, user_sessions, guild_keys,
# discord_pending_connects, discord_account_links, push_tokens).
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

# Pure-read functions that would leak the server's filesystem or open network
# connections. The READ ONLY transaction can't stop these (they don't write),
# so they stay explicitly blocked. Writes and write-side functions need no entry
# here — the read-only transaction rejects them at the DB.
_FORBIDDEN_FUNCTIONS = re.compile(
    r"\b(PG_READ_FILE|PG_READ_BINARY_FILE|PG_LS_DIR|PG_STAT_FILE|"
    r"LO_IMPORT|LO_EXPORT|LO_GET|LO_PUT|DBLINK)\b",
    re.IGNORECASE,
)
_TABLE_REF_RE = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)
_SELECT_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)


class DebugRawQueryRequest(BaseModel):
    sql: str


def _validate_readonly_query(sql: str) -> str:
    """Return the validated, semicolon-stripped query, or raise HTTPException(400).

    Requires a single ``SELECT ...`` statement, no comments, no filesystem/network
    disclosure functions, and no reference to any table outside ``_ALLOWED_TABLES``
    anywhere in the statement (including in subqueries). Writes are not checked
    here — the ``SET TRANSACTION READ ONLY`` in the handler rejects them at the DB.
    Colons are allowed — they're needed for ``::type`` casts and ISO timestamp
    literals — but a bare ``:name`` token that ``text()`` mistakes for a bind
    parameter will surface as a clear 400 from the execute call, not a silent misfire.
    """
    stripped = sql.strip()
    if not stripped:
        raise HTTPException(status_code=400, detail="sql must not be empty")
    if "--" in stripped or "/*" in stripped:
        raise HTTPException(status_code=400, detail="SQL comments are not allowed")
    # Allow one optional trailing semicolon, but not a second statement.
    body = stripped[:-1].strip() if stripped.endswith(";") else stripped
    if ";" in body:
        raise HTTPException(status_code=400, detail="Only a single statement is allowed")
    if not _SELECT_RE.match(body):
        raise HTTPException(status_code=400, detail="sql must be a SELECT statement")
    if _FORBIDDEN_FUNCTIONS.search(body):
        raise HTTPException(status_code=400, detail="Filesystem/network functions are not allowed")
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
    # READ ONLY makes Postgres reject any write or write-side function for this
    # transaction — a DB guarantee that replaces a fragile keyword blocklist.
    # Issued first, before any snapshot-taking statement, as Postgres requires.
    await db.exec(text("SET TRANSACTION READ ONLY"))
    # SET LOCAL is transaction-scoped — bounds how long a caller-supplied
    # query can run without affecting other sessions.
    await db.exec(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT_MS}ms'"))
    wrapped = text(f"SELECT * FROM ({validated}) AS debug_query LIMIT :limit")
    try:
        result = await db.exec(wrapped, params={"limit": _MAX_LIMIT})
        return [dict(r._mapping) for r in result.all()]
    except DBAPIError as exc:
        if "canceling statement due to statement timeout" in str(exc.orig):
            raise HTTPException(
                status_code=504,
                detail=f"Query exceeded the {_STATEMENT_TIMEOUT_MS // 1000}s statement timeout",
            ) from exc
        raise HTTPException(status_code=400, detail=f"Query failed: {exc.orig}") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=400, detail=f"Query failed: {exc}") from exc
