"""One-off agent subprocess routes (``/guilds/{gid}/agents/{aid}/run``).

The Run/Stop pair starts and kills a Claude/Codex/Pi subprocess attached to
an existing agent row. Output is streamed back over the guild WebSocket
via ``emit_terminal_line``; the HTTP responses are minimal status payloads.
"""

from __future__ import annotations

from agent_runner import RunAgentRequest, running_processes, stream_agent
from auth_deps import require_member
from fastapi import APIRouter, Depends, HTTPException
from util.tasks import spawn

router = APIRouter()


@router.post("/guilds/{guild_id}/agents/{agent_id}/run")
async def start_agent_run(
    guild_id: str,
    agent_id: str,
    req: RunAgentRequest,
    github_user_id: str = Depends(require_member()),
):
    """Spawn an AI coding agent subprocess and stream its output over WebSocket."""
    old = running_processes.get(agent_id)
    if old:
        try:
            old.kill()
        except ProcessLookupError:
            pass

    spawn(stream_agent(guild_id, agent_id, req), name=f"agent-stream:{agent_id}")
    return {"status": "started", "agentId": agent_id, "tool": req.tool}


@router.delete("/guilds/{guild_id}/agents/{agent_id}/run")
async def stop_agent_run(
    guild_id: str,
    agent_id: str,
    github_user_id: str = Depends(require_member()),
):
    """Terminate a running agent subprocess."""
    proc = running_processes.get(agent_id)
    if not proc:
        raise HTTPException(status_code=404, detail="No running process for this agent")
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    return {"status": "stopped", "agentId": agent_id}
