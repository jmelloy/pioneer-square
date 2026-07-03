"""Cost summary endpoint: task counts and estimated cost grouped by model tier.

GET /guilds/{guild_id}/cost-summary?days=N
  Returns task counts and aggregated cost (from llm_usage result rows) grouped
  by (model_tier, model) for the last N days (default 30).  Tasks with NULL
  model_tier (legacy rows) are included and returned with model_tier=null.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from auth_deps import get_guild_pk, require_member
from database import get_db_dep
from fastapi import APIRouter, Depends, HTTPException
from models import LlmUsage, Task
from sqlalchemy import func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter()


@router.get("/guilds/{guild_id}/cost-summary")
async def get_cost_summary(
    guild_id: str,
    days: int = 30,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Return task counts and aggregated cost grouped by (model_tier, model).

    Query params:
      - days: look-back window in days (default 30, max 365).

    Each row in the response has:
      - model_tier: "cheap" | "standard" | "powerful" | null
      - model: the model id string or null
      - task_count: number of completed tasks
      - estimated_cost_usd: sum of self-reported cost from llm_usage, or null
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    days = max(1, min(days, 365))
    since = datetime.now(UTC) - timedelta(days=days)

    # Subquery: sum of self-reported cost per task from llm_usage result rows.
    cost_sq = (
        select(
            col(LlmUsage.task_id).label("task_id"),
            func.sum(col(LlmUsage.cost_usd)).label("total_cost"),
        )
        .where(
            col(LlmUsage.guild_id) == guild_pk,
            col(LlmUsage.kind) == "result",
            col(LlmUsage.cost_usd).is_not(None),
        )
        .group_by(col(LlmUsage.task_id))
        .subquery()
    )

    stmt = (
        select(
            col(Task.model_tier),
            col(Task.model),
            func.count(col(Task.id)).label("task_count"),
            func.sum(cost_sq.c.total_cost).label("estimated_cost_usd"),
        )
        .outerjoin(cost_sq, cost_sq.c.task_id == col(Task.id))
        .where(
            col(Task.guild_id) == guild_pk,
            col(Task.state).in_(["done", "failed"]),
            col(Task.created_at) >= since,
        )
        .group_by(col(Task.model_tier), col(Task.model))
        .order_by(col(Task.model_tier).nulls_last(), col(Task.model).nulls_last())
    )

    result = await db.exec(stmt)
    rows = result.all()

    return [
        {
            "model_tier": row.model_tier,
            "model": row.model,
            "task_count": row.task_count,
            "estimated_cost_usd": float(row.estimated_cost_usd)
            if row.estimated_cost_usd is not None
            else None,
        }
        for row in rows
    ]
