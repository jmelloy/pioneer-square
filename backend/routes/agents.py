"""One-off/interactive agent routes (``/guilds/{gid}/agents/{aid}/run``).

The Run button now creates an interactive task assigned to the selected agent's
worker. The worker owns the Pi process, worktree, logs, cancellation, and
message injection just like normal tasks.
"""

from __future__ import annotations

import secrets
import string
from datetime import UTC, datetime

from agent_runner import RunAgentRequest
from auth_deps import get_guild_pk, require_member
from database import get_db_dep
from events import broadcast_msg
from fastapi import APIRouter, Depends, HTTPException
from models import Agent, Task
from sqlalchemy import update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from ws_types import TaskAssignedMsg, TaskCancelMsg, TaskUpdateMsg

router = APIRouter()


@router.post("/guilds/{guild_id}/agents/{agent_id}/run")
async def start_agent_run(
    guild_id: str,
    agent_id: str,
    req: RunAgentRequest,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Create an interactive task for the selected agent's worker."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    agent = (
        await db.exec(
            select(Agent).where(col(Agent.id) == agent_id, col(Agent.guild_id) == guild_pk)
        )
    ).one_or_none()
    if agent is None or not agent.worker_id:
        raise HTTPException(status_code=404, detail="Worker-backed agent not found")
    if agent.state not in ("idle", "error"):
        raise HTTPException(status_code=409, detail="Agent is already running")

    tool = req.tool.lower()
    if tool not in {"claude", "codex", "pi"}:
        raise HTTPException(status_code=400, detail="Unknown tool")

    task_id = "t-" + "".join(
        secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6)
    )
    created_at = datetime.now(UTC)
    name = req.prompt[:60] or "Interactive Pi"
    db.add(
        Task(
            id=task_id,
            worker_id=agent.worker_id,
            guild_id=guild_pk,
            name=name,
            description=req.prompt,
            tool=tool,
            task_type="interactive",
            model=req.model,
            provider=req.provider,
            state="pending",
            phase="execute",
            created_at=created_at,
            user_id=github_user_id,
        )
    )
    await db.commit()

    await broadcast_msg(
        guild_id,
        TaskAssignedMsg(
            workerId=agent.worker_id,
            taskId=task_id,
            name=name,
            description=req.prompt,
            tool=tool,
            taskType="interactive",
            targetAgentId=agent_id,
            model=req.model,
            provider=req.provider,
            phase="execute",
        ),
    )
    return {"status": "started", "agentId": agent_id, "taskId": task_id, "tool": tool}


@router.delete("/guilds/{guild_id}/agents/{agent_id}/run")
async def stop_agent_run(
    guild_id: str,
    agent_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Cancel the task currently attached to this agent."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    agent = (
        await db.exec(
            select(Agent).where(col(Agent.id) == agent_id, col(Agent.guild_id) == guild_pk)
        )
    ).one_or_none()
    if agent is None or not agent.worker_id or not agent.current_task_id:
        raise HTTPException(status_code=404, detail="No running task for this agent")

    deleted_at = datetime.now(UTC)
    await db.exec(
        update(Task)
        .where(col(Task.id) == agent.current_task_id, col(Task.guild_id) == guild_pk)
        .values(state="cancelled", deleted_at=deleted_at)
    )
    await db.commit()
    await broadcast_msg(
        guild_id,
        TaskCancelMsg(workerId=agent.worker_id, taskId=agent.current_task_id),
    )
    await broadcast_msg(
        guild_id,
        TaskUpdateMsg(
            taskId=agent.current_task_id,
            state="cancelled",
            deletedAt=deleted_at.isoformat(),
        ),
    )
    return {"status": "stopped", "agentId": agent_id, "taskId": agent.current_task_id}
