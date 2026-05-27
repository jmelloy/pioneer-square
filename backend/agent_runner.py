"""Spawn and stream a Claude/Codex/Pi agent subprocess as terminal output.

Used by the ``/guilds/{gid}/agents/{aid}/run`` endpoint pair (the *one-off*
agent invocations from the UI's "Run" button).  Worker subprocesses are
managed by the standalone ``/worker`` package and don't touch this module.
"""

from __future__ import annotations

import asyncio
import json

from database import get_db
from events import broadcast, emit_terminal_line
from models import Agent
from pydantic import BaseModel
from sqlalchemy import update
from sqlmodel import col

# Running agent subprocesses: agent_id -> Process. Separate from the worker
# subprocess registry because workers run out-of-process.
running_processes: dict[str, asyncio.subprocess.Process] = {}


class RunAgentRequest(BaseModel):
    tool: str  # "claude" | "codex" | "pi"
    prompt: str
    model: str | None = None
    provider: str | None = None  # pi only


async def set_agent_state(guild_id: str, agent_id: str, state: str) -> None:
    """Broadcast and persist an agent state change (clears activity)."""
    await broadcast(
        guild_id, {"type": "agent-state", "agentId": agent_id, "state": state, "activity": None}
    )
    db = await get_db()
    try:
        from auth_deps import get_guild_pk

        guild_pk = await get_guild_pk(db, guild_id)
        await db.execute(
            update(Agent)
            .where(col(Agent.id) == agent_id, col(Agent.guild_id) == guild_pk)
            .values(state=state, activity=None)
        )
        await db.commit()
    finally:
        await db.close()


def build_command(req: RunAgentRequest) -> tuple[list[str], bool]:
    """Return (cmd_list, needs_stdin_prompt).

    needs_stdin_prompt=True means we must write the RPC prompt to stdin
    (Pi RPC mode) rather than passing it on the command line.
    """
    tool = req.tool.lower()

    if tool == "claude":
        cmd = [
            "claude",
            "-p",
            req.prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            "20",
        ]
        if req.model:
            cmd += ["--model", req.model]
        return cmd, False

    if tool == "codex":
        # codex exec --full-auto accepts a prompt as positional arg; --json for structured output
        cmd = ["codex", "exec", "--json", req.prompt]
        if req.model:
            cmd += ["--model", req.model]
        return cmd, False

    if tool == "pi":
        # Pi --mode rpc: bidirectional JSONL over stdin/stdout
        cmd = ["pi", "--mode", "rpc", "--no-session"]
        if req.provider:
            cmd += ["--provider", req.provider]
        if req.model:
            cmd += ["--model", req.model]
        return cmd, True

    raise ValueError(f"Unknown tool: {req.tool!r}")


async def stream_agent(guild_id: str, agent_id: str, req: RunAgentRequest) -> None:
    """Spawn the agent subprocess and stream its output as terminal-output events."""
    tool = req.tool.lower()

    try:
        cmd, needs_stdin = build_command(req)
    except ValueError as exc:
        await emit_terminal_line(guild_id, agent_id, f"✗ {exc}")
        return

    stdin_pipe = asyncio.subprocess.PIPE if needs_stdin else asyncio.subprocess.DEVNULL

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=stdin_pipe,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        await emit_terminal_line(guild_id, agent_id, f"✗ command not found: {cmd[0]}")
        await set_agent_state(guild_id, agent_id, "error")
        return
    except Exception as exc:
        await emit_terminal_line(guild_id, agent_id, f"✗ failed to start process: {exc}")
        await set_agent_state(guild_id, agent_id, "error")
        return

    if needs_stdin:
        # Pi RPC: send the initial prompt as a JSON command, then leave stdin open
        rpc_msg = json.dumps({"type": "prompt", "content": req.prompt}) + "\n"
        assert proc.stdin is not None  # PIPE was set above
        proc.stdin.write(rpc_msg.encode())
        await proc.stdin.drain()

    running_processes[agent_id] = proc
    await set_agent_state(guild_id, agent_id, "working")

    # For Pi message_update we track accumulated text to emit only deltas
    pi_last_text = ""

    async def _drain_stderr() -> None:
        async for raw in proc.stderr:  # type: ignore[union-attr]
            line = raw.decode(errors="replace").strip()
            if line:
                await emit_terminal_line(guild_id, agent_id, f"[stderr] {line}")

    stderr_task = asyncio.create_task(_drain_stderr())

    try:
        async for raw_line in proc.stdout or []:
            line_str = raw_line.decode(errors="replace").strip()
            if not line_str:
                continue

            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                await emit_terminal_line(guild_id, agent_id, line_str)
                continue

            text_out = parse_event(tool, event, pi_last_text)

            # Pi: update delta baseline
            if tool == "pi" and event.get("type") == "message_update":
                full = ""
                for blk in event.get("message", {}).get("content", []):
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        full += blk.get("text", "")
                pi_last_text = full
            elif tool == "pi" and event.get("type") == "agent_end":
                pi_last_text = ""

            if text_out:
                await emit_terminal_line(guild_id, agent_id, text_out)

    finally:
        if needs_stdin and proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        exit_code = await proc.wait()
        await stderr_task
        running_processes.pop(agent_id, None)
        await set_agent_state(guild_id, agent_id, "idle" if exit_code == 0 else "error")


def parse_event(tool: str, event: dict, pi_last_text: str) -> str | None:
    """Extract a human-readable line from one stream-JSON / RPC event."""

    if tool == "claude":
        t = event.get("type")
        if t == "assistant":
            parts = []
            for blk in event.get("message", {}).get("content", []):
                btype = blk.get("type")
                if btype == "text":
                    txt = blk.get("text", "").strip()
                    if txt:
                        parts.append(txt)
                elif btype == "thinking":
                    thinking = blk.get("thinking", "").strip()
                    if thinking:
                        preview = thinking[:100].replace("\n", " ")
                        parts.append(f"[thinking] {preview}{'...' if len(thinking) > 100 else ''}")
                elif btype == "tool_use":
                    name = blk.get("name", "")
                    inp = blk.get("input", {})
                    if name == "Bash":
                        parts.append(f"▶ bash: {inp.get('command', '')[:120]}")
                    elif name in ("Read", "Write", "Edit"):
                        fp = inp.get("file_path", inp.get("path", ""))
                        parts.append(f"▶ {name.lower()}: {fp}")
                    else:
                        parts.append(f"▶ {name}: {json.dumps(inp)[:80]}")
            return "\n".join(parts) or None
        if t == "user":
            parts = []
            for blk in event.get("message", {}).get("content", []):
                if blk.get("type") == "tool_result":
                    content = blk.get("content", "")
                    if isinstance(content, list):
                        content = "\n".join(
                            b.get("text", "") for b in content if b.get("type") == "text"
                        )
                    if isinstance(content, str) and content.strip():
                        lines = content.strip().split("\n")
                        preview = lines[0][:120]
                        if len(lines) > 1:
                            preview += f" (+{len(lines) - 1} lines)"
                        parts.append(f"  → {preview}")
            return "\n".join(parts) or None
        if t == "result":
            subtype = event.get("subtype", "success")
            turns = event.get("num_turns", 0)
            cost = event.get("cost_usd")
            cost_str = f" (${cost:.4f})" if cost else ""
            if subtype == "success":
                return f"✓ Done in {turns} turns{cost_str}"
            return f"✗ {subtype}: {event.get('error', '')}"
        if t == "system" and event.get("subtype") == "init":
            tools = event.get("tools", [])
            return f"[claude] tools: {', '.join(tools[:6])}"

    elif tool == "codex":
        t = event.get("type")
        if t == "message" and event.get("role") == "assistant":
            return (event.get("content") or "").strip() or None
        if t == "function_call":
            name = event.get("name", "")
            args = event.get("arguments", "")
            return f"▶ {name}({args[:80]})"
        if t == "function_result":
            return f"  → {str(event.get('output', ''))[:200]}"
        if t == "done":
            return "✓ Done"
        if t == "error":
            return f"✗ {event.get('message', '')}"

    elif tool == "pi":
        t = event.get("type")
        if t == "message_update":
            full = ""
            for blk in event.get("message", {}).get("content", []):
                if isinstance(blk, dict) and blk.get("type") == "text":
                    full += blk.get("text", "")
            delta = full[len(pi_last_text) :]
            return delta if delta.strip() else None
        if t == "tool_execution_start":
            ti = event.get("tool", {})
            name = ti.get("name", "")
            inp = ti.get("input", {})
            if name == "bash":
                return f"▶ bash: {inp.get('command', '')[:120]}"
            if name in ("read", "write", "edit"):
                return f"▶ {name}: {inp.get('path', inp.get('file_path', ''))}"
            return f"▶ {name}({json.dumps(inp)[:80]})"
        if t == "tool_execution_end":
            out = str(event.get("output", "")).strip()
            if not out:
                return None
            lines = out.split("\n")
            preview = lines[0][:120]
            if len(lines) > 1:
                preview += f" (+{len(lines) - 1} lines)"
            return f"  → {preview}"
        if t == "agent_end":
            err = event.get("error")
            return f"✗ {err}" if err else None
        if t == "agent_start":
            return "[pi] agent started"

    return None
