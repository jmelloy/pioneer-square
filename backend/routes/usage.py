"""Claude usage reporting from workers' headless runs.

Workers parse ``claude --output-format stream-json`` and POST per-API-call
usage (one record per assistant message) plus a single ``result`` record per
run. Rows are stored in ``claude_usage`` and a summary is broadcast so the
frontend can surface live token/cost figures per task.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from auth_deps import get_guild_pk, require_member, require_worker_or_member_path
from database import get_db_dep
from events import broadcast_msg
from fastapi import APIRouter, Depends, HTTPException
from models import ClaudeUsage
from pydantic import BaseModel, ValidationError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from utils import row_to_dict
from ws_types import ClaudeUsageMsg

router = APIRouter()
logger = logging.getLogger(__name__)


class UsageRecord(BaseModel):
    kind: str  # "api_call" | "result"
    call_index: int | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float | None = None
    num_turns: int | None = None
    stop_reason: str | None = None


class UsageReport(BaseModel):
    task_id: str | None = None
    worker_id: str | None = None
    session_id: str | None = None
    repo: str | None = None
    reporter: str | None = None
    records: list[UsageRecord]


def _summarize(records: list[UsageRecord]) -> dict:
    """Aggregate a batch into a compact summary for the WS broadcast."""
    calls = [r for r in records if r.kind == "api_call"]
    result = next((r for r in records if r.kind == "result"), None)
    summed_input = sum(r.input_tokens for r in calls)
    summed_output = sum(r.output_tokens for r in calls)
    summed_cache_read = sum(r.cache_read_input_tokens for r in calls)
    summed_cache_creation = sum(r.cache_creation_input_tokens for r in calls)
    return {
        "apiCalls": len(calls),
        "summedInputTokens": summed_input,
        "summedOutputTokens": summed_output,
        "summedCacheReadTokens": summed_cache_read,
        "summedCacheCreationTokens": summed_cache_creation,
        # Claude's self-reported result figures (may disagree with the sums above).
        "reportedInputTokens": result.input_tokens if result else None,
        "reportedOutputTokens": result.output_tokens if result else None,
        "reportedCacheReadTokens": result.cache_read_input_tokens if result else None,
        "reportedCacheCreationTokens": result.cache_creation_input_tokens if result else None,
        "costUsd": result.cost_usd if result else None,
        "numTurns": result.num_turns if result else None,
        "stopReason": result.stop_reason if result else None,
    }


@router.post("/guilds/{guild_id}/usage")
async def report_usage(
    guild_id: str,
    data: UsageReport,
    principal: str = Depends(require_worker_or_member_path),
    db: AsyncSession = Depends(get_db_dep),
):
    """Persist a batch of usage records for a task run and broadcast a summary."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    if not data.records:
        return {"stored": 0}

    created_at = datetime.now(UTC)
    for rec in data.records:
        db.add(
            ClaudeUsage(
                guild_id=guild_pk,
                task_id=data.task_id,
                worker_id=data.worker_id,
                session_id=data.session_id,
                kind=rec.kind,
                call_index=rec.call_index,
                model=rec.model,
                input_tokens=rec.input_tokens,
                output_tokens=rec.output_tokens,
                cache_read_input_tokens=rec.cache_read_input_tokens,
                cache_creation_input_tokens=rec.cache_creation_input_tokens,
                cost_usd=rec.cost_usd,
                num_turns=rec.num_turns,
                stop_reason=rec.stop_reason,
                repo=data.repo,
                reporter=data.reporter,
                created_at=created_at,
            )
        )
    await db.commit()

    try:
        usage_msg = ClaudeUsageMsg(
            taskId=data.task_id,
            workerId=data.worker_id,
            sessionId=data.session_id,
            model=next((r.model for r in data.records if r.model), None),
            repo=data.repo,
            reporter=data.reporter,
            **_summarize(data.records),
        )
    except ValidationError as exc:
        logger.error("Failed to construct ClaudeUsageMsg for guild %s: %s", guild_id, exc)
        return {"stored": len(data.records)}
    await broadcast_msg(guild_id, usage_msg)
    return {"stored": len(data.records)}


@router.get("/guilds/{guild_id}/usage")
async def list_usage(
    guild_id: str,
    task_id: str | None = None,
    limit: int = 500,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """List usage rows for a guild, most recent first (optionally one task)."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    query = select(ClaudeUsage).where(col(ClaudeUsage.guild_id) == guild_pk)
    if task_id is not None:
        query = query.where(col(ClaudeUsage.task_id) == task_id)
    query = query.order_by(col(ClaudeUsage.id).desc()).limit(min(limit, 2000))
    result = await db.exec(query)
    return [row_to_dict(r) for r in result.all()]
