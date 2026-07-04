"""Discord slash command interaction handler (Phase 3).

POST /discord/interactions — receives and validates Discord interaction payloads.

Required env vars:
    DISCORD_PUBLIC_KEY          Ed25519 public key (hex) from Discord developer portal
    DISCORD_APPLICATION_ID      Discord application/bot ID (used for followup URLs)
    DISCORD_BOT_TOKEN           Discord bot token (reuses Phase 2 var)
    DISCORD_PIONEER_GUILD_SLUG  Pioneer Square guild slug to target for all commands

Optional env vars:
    DISCORD_ALLOWED_ROLE_IDS    Comma-separated Discord role IDs; empty = allow all

Registered slash commands (see scripts/register_discord_commands.py):
    /ps status              — formatted embed with worker states and active task counts
    /ps workers             — list all workers with state, repos, and agent count
    /ps pickup <issue-url>  — claim an issue and assign to an idle worker
    /ps review <pr-url>     — trigger a PR review task
    /ps cancel <task-id>    — cancel a running task
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import string
from datetime import UTC, datetime, timedelta

import httpx
from database import AsyncSessionLocal
from events import broadcast_msg
from fastapi import APIRouter, BackgroundTasks, Request, Response
from models import Agent, Guild, Task, Worker, live_tasks_filter
from sqlalchemy import func, update
from sqlmodel import col, select
from ws_types import TaskCancelMsg, TaskCreatedMsg, TaskUpdateMsg

logger = logging.getLogger(__name__)

router = APIRouter()

_DISCORD_API_BASE = "https://discord.com/api/v10"

# Discord interaction types
_PING = 1
_APPLICATION_COMMAND = 2

# Discord response types
_PONG = 1
_CHANNEL_MESSAGE = 4
_DEFERRED_CHANNEL_MESSAGE = 5

# Ephemeral message flag
_EPHEMERAL = 64

# Default soft-delete window for cancelled tasks
_CANCEL_TTL = timedelta(days=3)


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _public_key() -> str | None:
    return os.environ.get("DISCORD_PUBLIC_KEY") or None


def _application_id() -> str | None:
    return os.environ.get("DISCORD_APPLICATION_ID") or None


def _bot_token() -> str | None:
    return os.environ.get("DISCORD_BOT_TOKEN") or None


def _pioneer_guild_slug() -> str | None:
    return os.environ.get("DISCORD_PIONEER_GUILD_SLUG") or None


def _allowed_role_ids() -> set[str]:
    raw = os.environ.get("DISCORD_ALLOWED_ROLE_IDS", "")
    return {r.strip() for r in raw.split(",") if r.strip()}


# ---------------------------------------------------------------------------
# Ed25519 signature verification
# ---------------------------------------------------------------------------


def _verify_signature(public_key_hex: str, signature_hex: str, timestamp: str, body: bytes) -> bool:
    """Verify a Discord Ed25519 request signature. Returns False on any error."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
            Ed25519PublicKey,
        )

        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        pub.verify(bytes.fromhex(signature_hex), timestamp.encode() + body)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def _is_authorized(interaction: dict) -> bool:
    """Return True if the interaction's author has an allowed role (or all roles allowed)."""
    allowed = _allowed_role_ids()
    if not allowed:
        return True  # no restriction configured — allow everyone
    member = interaction.get("member")
    if not member:
        # DM interaction — no role info available; deny by default
        return False
    user_roles: list[str] = member.get("roles", [])
    return bool(set(user_roles) & allowed)


# ---------------------------------------------------------------------------
# Discord API helper
# ---------------------------------------------------------------------------


async def _discord_patch(path: str, payload: dict) -> None:
    """PATCH a Discord REST endpoint. Errors are logged and swallowed."""
    token = _bot_token()
    if not token:
        return
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.patch(f"{_DISCORD_API_BASE}{path}", json=payload, headers=headers)
            resp.raise_for_status()
    except Exception:
        logger.warning("discord: PATCH %s failed", path, exc_info=True)


async def _send_followup(
    interaction_token: str, content: str | None = None, embeds: list | None = None
) -> None:
    """Edit the deferred reply for an interaction."""
    app_id = _application_id()
    if not app_id:
        return
    payload: dict = {}
    if content is not None:
        payload["content"] = content
    if embeds is not None:
        payload["embeds"] = embeds
    await _discord_patch(f"/webhooks/{app_id}/{interaction_token}/messages/@original", payload)


# ---------------------------------------------------------------------------
# GitHub URL parsers
# ---------------------------------------------------------------------------

_GH_ISSUE_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)", re.IGNORECASE)
_GH_PR_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)", re.IGNORECASE)


def _parse_issue_url(url: str) -> tuple[str, int] | None:
    m = _GH_ISSUE_RE.search(url)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _parse_pr_url(url: str) -> tuple[str, int] | None:
    m = _GH_PR_RE.search(url)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _new_task_id() -> str:
    return "t-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


# ---------------------------------------------------------------------------
# Command implementations (run in background after deferred ack)
# ---------------------------------------------------------------------------


async def _cmd_status(interaction_token: str, guild_slug: str) -> None:
    """Reply with a summary of workers and active tasks."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.exec(select(Guild).where(col(Guild.slug) == guild_slug))
            guild = result.one_or_none()
            if not guild:
                await _send_followup(
                    interaction_token, content=f"Pioneer guild `{guild_slug}` not found."
                )
                return
            guild_pk = guild.id

            worker_rows = (
                await db.exec(select(Worker).where(col(Worker.guild_id) == guild_pk))
            ).all()

            task_rows = (
                await db.exec(
                    select(Task).where(
                        col(Task.guild_id) == guild_pk,
                        col(Task.state).in_(["pending", "working", "awaiting-review"]),
                        live_tasks_filter(),
                    )
                )
            ).all()

        total = len(worker_rows)
        online = sum(1 for w in worker_rows if w.state == "online")
        idle = sum(1 for w in worker_rows if w.state == "idle")
        offline = total - online - idle

        state_counts: dict[str, int] = {}
        for t in task_rows:
            state_counts[t.state] = state_counts.get(t.state, 0) + 1

        task_lines = (
            "\n".join(f"• **{s}**: {c}" for s, c in sorted(state_counts.items()))
            or "No active tasks"
        )

        embeds = [
            {
                "title": "Pioneer Square Status",
                "color": 0x3498DB,
                "fields": [
                    {
                        "name": "Workers",
                        "value": f"Total: {total} | Online: {online} | Idle: {idle} | Offline: {offline}",
                        "inline": False,
                    },
                    {
                        "name": "Active Tasks",
                        "value": task_lines,
                        "inline": False,
                    },
                ],
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ]
        await _send_followup(interaction_token, embeds=embeds)
    except Exception:
        logger.exception("discord: /ps status failed")
        await _send_followup(interaction_token, content="Failed to fetch status.")


async def _cmd_workers(interaction_token: str, guild_slug: str) -> None:
    """Reply with a list of all workers and their state."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.exec(select(Guild).where(col(Guild.slug) == guild_slug))
            guild = result.one_or_none()
            if not guild:
                await _send_followup(
                    interaction_token, content=f"Pioneer guild `{guild_slug}` not found."
                )
                return
            guild_pk = guild.id

            worker_rows = (
                await db.exec(
                    select(
                        col(Worker.id),
                        col(Worker.state),
                        col(Worker.repos),
                        col(Worker.name),
                        col(Worker.org),
                        func.count(col(Agent.id)).label("agent_count"),
                    )
                    .outerjoin(
                        Agent,
                        (col(Agent.worker_id) == col(Worker.id)) & (col(Agent.state) != "offline"),
                    )
                    .where(col(Worker.guild_id) == guild_pk)
                    .group_by(col(Worker.id))
                )
            ).all()

        if not worker_rows:
            await _send_followup(interaction_token, content="No workers registered.")
            return

        _STATE_EMOJI = {"online": "🟢", "idle": "🟡", "offline": "⚫"}
        lines = []
        for row in worker_rows:
            label = row.name or row.id
            emoji = _STATE_EMOJI.get(row.state, "❓")
            repos_raw = row.repos or "[]"
            try:
                repos = json.loads(repos_raw)
            except Exception:
                repos = []
            if row.org and not repos:
                repos_str = f"{row.org}/*"
            elif repos:
                repos_str = ", ".join(repos[:3]) + ("..." if len(repos) > 3 else "")
            else:
                repos_str = "—"
            lines.append(
                f"{emoji} **{label}** ({row.state}) — agents: {row.agent_count} — repos: {repos_str}"
            )

        embeds = [
            {
                "title": "Pioneer Square Workers",
                "description": "\n".join(lines),
                "color": 0x1ABC9C,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ]
        await _send_followup(interaction_token, embeds=embeds)
    except Exception:
        logger.exception("discord: /ps workers failed")
        await _send_followup(interaction_token, content="Failed to fetch workers.")


async def _cmd_pickup(interaction_token: str, guild_slug: str, issue_url: str) -> None:
    """Create a task to pick up a GitHub issue and assign it to an idle worker."""
    parsed = _parse_issue_url(issue_url)
    if not parsed:
        await _send_followup(
            interaction_token,
            content=f"Invalid issue URL: `{issue_url}`\nExpected: `https://github.com/owner/repo/issues/N`",
        )
        return

    issue_repo, issue_number = parsed

    try:
        async with AsyncSessionLocal() as db:
            result = await db.exec(select(Guild).where(col(Guild.slug) == guild_slug))
            guild = result.one_or_none()
            if not guild:
                await _send_followup(
                    interaction_token, content=f"Pioneer guild `{guild_slug}` not found."
                )
                return
            guild_pk = guild.id

            task_id = _new_task_id()
            task_name = f"{issue_repo}#{issue_number}"
            desc = f"Implement GitHub issue {issue_repo}#{issue_number}: {issue_url}"
            created_at = datetime.now(UTC)
            task = Task(
                id=task_id,
                worker_id=None,
                guild_id=guild_pk,
                name=task_name,
                description=desc,
                tool="claude",
                state="pending",
                phase="execute",
                issue_repo=issue_repo,
                issue_number=issue_number,
                created_at=created_at,
            )
            db.add(task)
            await db.commit()

        await broadcast_msg(
            guild_slug,
            TaskCreatedMsg(
                taskId=task_id,
                name=task_name,
                description=desc,
                phase="execute",
                state="pending",
                createdAt=created_at.isoformat(),
            ),
        )

        embeds = [
            {
                "title": f"Issue Claimed: {issue_repo}#{issue_number}",
                "description": f"Task `{task_id}` created and queued for an idle worker.\n[View issue]({issue_url})",
                "color": 0x2ECC71,
                "timestamp": created_at.isoformat(),
            }
        ]
        await _send_followup(interaction_token, embeds=embeds)
    except Exception:
        logger.exception("discord: /ps pickup failed for %s", issue_url)
        await _send_followup(interaction_token, content="Failed to create task.")


async def _cmd_review(interaction_token: str, guild_slug: str, pr_url: str) -> None:
    """Create a PR review task."""
    parsed = _parse_pr_url(pr_url)
    if not parsed:
        await _send_followup(
            interaction_token,
            content=f"Invalid PR URL: `{pr_url}`\nExpected: `https://github.com/owner/repo/pull/N`",
        )
        return

    pr_repo, pr_number = parsed

    try:
        async with AsyncSessionLocal() as db:
            result = await db.exec(select(Guild).where(col(Guild.slug) == guild_slug))
            guild = result.one_or_none()
            if not guild:
                await _send_followup(
                    interaction_token, content=f"Pioneer guild `{guild_slug}` not found."
                )
                return
            guild_pk = guild.id

            task_id = _new_task_id()
            task_name = f"Review PR {pr_url}"
            desc = f"Review pull request {pr_repo}#{pr_number}: {pr_url}"
            created_at = datetime.now(UTC)
            task = Task(
                id=task_id,
                worker_id=None,
                guild_id=guild_pk,
                name=task_name,
                description=desc,
                tool="claude",
                state="pending",
                phase="review",
                issue_repo=pr_repo,
                issue_number=pr_number,
                pr_url=pr_url,
                pr_number=pr_number,
                pr_repo=pr_repo,
                created_at=created_at,
            )
            db.add(task)
            await db.commit()

        pr_name = f"{pr_repo}#{pr_number}"
        await broadcast_msg(
            guild_slug,
            TaskCreatedMsg(
                taskId=task_id,
                name=pr_name,
                description=desc,
                phase="review",
                state="pending",
                createdAt=created_at.isoformat(),
            ),
        )

        embeds = [
            {
                "title": f"PR Review Queued: {pr_repo}#{pr_number}",
                "description": f"Review task `{task_id}` created and queued.\n[View PR]({pr_url})",
                "color": 0x9B59B6,
                "timestamp": created_at.isoformat(),
            }
        ]
        await _send_followup(interaction_token, embeds=embeds)
    except Exception:
        logger.exception("discord: /ps review failed for %s", pr_url)
        await _send_followup(interaction_token, content="Failed to create review task.")


async def _cmd_cancel(interaction_token: str, guild_slug: str, task_id: str) -> None:
    """Cancel a running task."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.exec(select(Guild).where(col(Guild.slug) == guild_slug))
            guild = result.one_or_none()
            if not guild:
                await _send_followup(
                    interaction_token, content=f"Pioneer guild `{guild_slug}` not found."
                )
                return
            guild_pk = guild.id

            task_result = await db.exec(
                select(col(Task.worker_id), col(Task.state)).where(
                    col(Task.id) == task_id, col(Task.guild_id) == guild_pk
                )
            )
            row = task_result.one_or_none()
            if not row:
                await _send_followup(interaction_token, content=f"Task `{task_id}` not found.")
                return

            worker_id, state = row
            if state in ("done", "failed", "cancelled"):
                await _send_followup(
                    interaction_token, content=f"Task `{task_id}` is already `{state}`."
                )
                return

            deleted_at = datetime.now(UTC) + _CANCEL_TTL
            await db.exec(
                update(Task)
                .where(col(Task.id) == task_id)
                .values(state="cancelled", deleted_at=deleted_at)
            )
            await db.commit()

        if worker_id:
            await broadcast_msg(guild_slug, TaskCancelMsg(workerId=worker_id, taskId=task_id))
        await broadcast_msg(
            guild_slug,
            TaskUpdateMsg(taskId=task_id, state="cancelled", deletedAt=deleted_at.isoformat()),
        )

        embeds = [
            {
                "title": f"Task Cancelled: {task_id}",
                "description": f"Task `{task_id}` has been cancelled.",
                "color": 0xE74C3C,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ]
        await _send_followup(interaction_token, embeds=embeds)
    except Exception:
        logger.exception("discord: /ps cancel failed for task %s", task_id)
        await _send_followup(interaction_token, content="Failed to cancel task.")


# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------


async def _dispatch_command(interaction: dict) -> None:
    """Parse the interaction data and dispatch to the appropriate command handler."""
    token = interaction.get("token", "")
    data = interaction.get("data", {})
    guild_slug = _pioneer_guild_slug() or ""

    options: list[dict] = data.get("options", [])
    if not options:
        await _send_followup(token, content="Unknown subcommand.")
        return

    sub = options[0]
    sub_name = sub.get("name", "")
    sub_opts = {o["name"]: o["value"] for o in sub.get("options", [])}

    if sub_name == "status":
        await _cmd_status(token, guild_slug)
    elif sub_name == "workers":
        await _cmd_workers(token, guild_slug)
    elif sub_name == "pickup":
        issue_url = sub_opts.get("issue-url", "")
        await _cmd_pickup(token, guild_slug, issue_url)
    elif sub_name == "review":
        pr_url = sub_opts.get("pr-url", "")
        await _cmd_review(token, guild_slug, pr_url)
    elif sub_name == "cancel":
        task_id_val = sub_opts.get("task-id", "")
        await _cmd_cancel(token, guild_slug, task_id_val)
    else:
        await _send_followup(token, content=f"Unknown subcommand: `{sub_name}`")


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/discord/interactions")
async def discord_interactions(request: Request, background_tasks: BackgroundTasks) -> Response:
    """Receive and handle Discord interaction payloads.

    Signature verification is always performed first. PING responses are
    immediate. Slash commands return a deferred ephemeral acknowledgement
    within the 3-second Discord window and then resolve the response in a
    background task.
    """
    pub_key = _public_key()
    if not pub_key:
        logger.error("discord: DISCORD_PUBLIC_KEY is not configured; refusing request")
        return Response(content="Server misconfiguration: DISCORD_PUBLIC_KEY not set", status_code=500)

    body = await request.body()
    sig = request.headers.get("x-signature-ed25519", "")
    ts = request.headers.get("x-signature-timestamp", "")
    if not sig or not ts or not _verify_signature(pub_key, sig, ts, body):
        return Response(content="Invalid signature", status_code=401)

    try:
        interaction = json.loads(body)
    except Exception:
        return Response(content="Bad JSON", status_code=400)

    itype = interaction.get("type")

    # Discord PING health check — must respond with PONG immediately
    if itype == _PING:
        return Response(
            content=json.dumps({"type": _PONG}),
            media_type="application/json",
        )

    if itype == _APPLICATION_COMMAND:
        # Authorization check — return an immediate non-deferred ephemeral error
        if not _is_authorized(interaction):
            payload = {
                "type": _CHANNEL_MESSAGE,
                "data": {
                    "content": "You are not authorized to use Pioneer Square commands.",
                    "flags": _EPHEMERAL,
                },
            }
            return Response(content=json.dumps(payload), media_type="application/json")

        # Acknowledge immediately with a deferred ephemeral response
        ack = {
            "type": _DEFERRED_CHANNEL_MESSAGE,
            "data": {"flags": _EPHEMERAL},
        }

        # Schedule command execution in the background
        background_tasks.add_task(_dispatch_command, interaction)

        return Response(content=json.dumps(ack), media_type="application/json")

    return Response(content=json.dumps({"error": "unhandled interaction type"}), status_code=400)
