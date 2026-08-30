"""Discord slash command interaction handler (Phase 3).

POST /discord/interactions — receives and validates Discord interaction payloads.

Required env vars:
    DISCORD_PUBLIC_KEY          Ed25519 public key (hex) from Discord developer portal
    DISCORD_APPLICATION_ID      Discord application/bot ID (used for followup URLs)
    DISCORD_BOT_TOKEN           Discord bot token (reuses Phase 2 var)

Optional env vars:
    DISCORD_PIONEER_GUILD_SLUG  Pins every /ps command to one Pioneer Square guild.
                                Deprecated: when unset, a command resolves its own
                                target the way an inbound bot @-mention does —
                                channel binding, then Discord-server binding, then
                                GUILD_ID (see discord.router.resolve_guild_slug).
    GUILD_ID                    Instance default guild, used as the last resort above.
    DISCORD_ALLOWED_ROLE_IDS    Comma-separated Discord role IDs; empty = allow all
    DISCORD_OPERATOR_ROLE_NAME  Discord role name allowed to run /join-channel and
                                /leave-channel (in addition to Manage Channels
                                permission); default "Pioneer Square Operator"

Registered slash commands (see scripts/register_discord_commands.py):
    /ps status                          — formatted embed with worker states and active task counts
    /ps workers                         — list all workers with state, repos, and agent count
    /ps pickup <issue-url>              — claim an issue and assign to an idle worker
    /ps review <pr-url>                 — trigger a PR review task
    /ps cancel <task-id>                — cancel a running task
    /join-channel <channel> [guild]     — wire a Discord channel to a Pioneer Square guild
    /leave-channel [channel]            — remove a channel's Pioneer Square guild binding
    /connect-account                    — link your Discord account to Pioneer Square
    /worker-spawn [repos] [tools]       — spawn a new worker (requires a connected account)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import string
import uuid
from datetime import UTC, datetime, timedelta

import discord_notifier
from database import AsyncSessionLocal
from discord.auth import is_member_authorized
from events import broadcast_msg
from fastapi import APIRouter, BackgroundTasks, Request, Response
from foreman.github_url_parser import parse_github_urls
from foreman.tools import spawn_worker
from models import (
    Agent,
    DiscordAccountLink,
    DiscordChannelGuild,
    DiscordPendingConnect,
    Guild,
    Task,
    Worker,
    live_tasks_filter,
)
from oauth import FRONTEND_URL
from sqlalchemy import func, update
from sqlmodel import col, select
from task_lifecycle import TERMINAL_STATES
from util.tasks import spawn
from ws_types import TaskCancelMsg, TaskCreatedMsg, TaskUpdateMsg

logger = logging.getLogger(__name__)

router = APIRouter()

# Discord interaction types
_PING = 1
_APPLICATION_COMMAND = 2

# Discord response types
_PONG = 1
_CHANNEL_MESSAGE = 4
_DEFERRED_CHANNEL_MESSAGE = 5

# Ephemeral message flag
_EPHEMERAL = 64

# Lifetime of a /connect-account one-time token
_CONNECT_TOKEN_TTL = timedelta(minutes=15)

# Discord permission bit for MANAGE_CHANNELS (see Discord's permissions bitfield docs)
_MANAGE_CHANNELS_BIT = 0x10

_DEFAULT_OPERATOR_ROLE_NAME = "Pioneer Square Operator"

# How long to keep polling a freshly-spawned worker's state before giving up
# on updating the Discord popup in place. 5 minutes comfortably covers a cold
# ECS container start (image pull + boot + gateway handshake) — see issue #991.
_WORKER_ONLINE_TIMEOUT = timedelta(minutes=5)

# Interval between worker-state polls while awaiting the online transition.
_WORKER_ONLINE_POLL_INTERVAL = 5.0


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _public_key() -> str | None:
    return os.environ.get("DISCORD_PUBLIC_KEY") or None


def _application_id() -> str | None:
    return os.environ.get("DISCORD_APPLICATION_ID") or None


def _pioneer_guild_slug() -> str | None:
    """Explicit, instance-wide override for the guild ``/ps`` commands target.

    Deprecated in favour of letting the command resolve its own context — see
    ``_resolve_command_guild_slug``. Kept because it is the documented way to
    pin an instance to one project, and unsetting it silently would change
    behaviour for deployments relying on it.
    """
    return os.environ.get("DISCORD_PIONEER_GUILD_SLUG") or None


async def _resolve_command_guild_slug(interaction: dict) -> str:
    """Return the Pioneer Square guild slug a slash command should operate on.

    Resolves the same way an inbound bot @-mention does — channel binding,
    then Discord-server binding, then the instance default (``GUILD_ID``) —
    so ``/ps status`` in a channel wired to a project reports on that project
    rather than on whatever one env var names. ``DISCORD_PIONEER_GUILD_SLUG``
    still wins when explicitly set, preserving the pinned-instance setup.

    Returns ``""`` when nothing resolves, matching what the call sites
    previously produced for an unset env var.
    """
    explicit = _pioneer_guild_slug()
    if explicit:
        return explicit
    from discord.router import resolve_guild_slug  # noqa: PLC0415

    slug = await resolve_guild_slug(interaction.get("channel_id"), interaction.get("guild_id"))
    return slug or ""


def _operator_role_name() -> str:
    return os.environ.get("DISCORD_OPERATOR_ROLE_NAME") or _DEFAULT_OPERATOR_ROLE_NAME


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
    return is_member_authorized(interaction.get("member"))


def _has_manage_channels(member: dict) -> bool:
    """Return True if the member's computed permission bitfield includes MANAGE_CHANNELS."""
    perms = member.get("permissions")
    if not perms:
        return False
    try:
        return (int(perms) & _MANAGE_CHANNELS_BIT) != 0
    except (TypeError, ValueError):
        return False


async def _has_operator_role(interaction: dict) -> bool:
    """Return True if the invoking member holds the configured Operator role.

    Resolves the role name to Discord role IDs via a live API call (interaction
    payloads only carry role IDs, not names) and checks against the member's roles.
    """
    member = interaction.get("member") or {}
    discord_guild_id = interaction.get("guild_id")
    user_role_ids: set[str] = set(member.get("roles", []))
    if not discord_guild_id or not user_role_ids:
        return False

    roles = await discord_notifier.get(f"/guilds/{discord_guild_id}/roles")
    if not roles:
        return False
    role_name = _operator_role_name()
    operator_role_ids = {
        r["id"] for r in roles if isinstance(r, dict) and r.get("name") == role_name
    }
    return bool(user_role_ids & operator_role_ids)


async def _can_manage_channel_bindings(interaction: dict) -> bool:
    """Return True if the invoking user may run /join-channel or /leave-channel.

    Requires Discord's Manage Channels permission or the configured Operator role.
    """
    member = interaction.get("member") or {}
    if _has_manage_channels(member):
        return True
    return await _has_operator_role(interaction)


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
    await discord_notifier.patch(
        f"/webhooks/{app_id}/{interaction_token}/messages/@original", payload
    )


# ---------------------------------------------------------------------------
# GitHub URL parsers
# ---------------------------------------------------------------------------


def _parse_issue_url(url: str) -> tuple[str, int] | None:
    refs = [ref for ref in parse_github_urls(url) if ref.ref_type == "issues"]
    return (refs[0].slug, refs[0].number) if refs else None


def _parse_pr_url(url: str) -> tuple[str, int] | None:
    refs = [ref for ref in parse_github_urls(url) if ref.ref_type == "pull"]
    return (refs[0].slug, refs[0].number) if refs else None


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
            if state in TERMINAL_STATES:
                await _send_followup(
                    interaction_token, content=f"Task `{task_id}` is already `{state}`."
                )
                return

            deleted_at = datetime.now(UTC)  # cancellations are stamped immediately
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


def _interaction_user(interaction: dict) -> tuple[str, str] | None:
    """Return (discord_user_id, display_name) for the invoking user, or None.

    Guild interactions carry the user under ``member.user``; DM interactions
    carry it directly under ``user``.
    """
    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    user_id = user.get("id")
    if not user_id:
        return None
    username = user.get("global_name") or user.get("username") or str(user_id)
    return str(user_id), str(username)


async def _cmd_connect_account(interaction: dict) -> None:
    """Mint a one-time token and reply with a link to connect this Discord account.

    The link points the user at ``/auth/discord/connect?token=<token>``, which
    (once they're logged in to Pioneer Square) redeems the token via
    ``POST /api/discord/connect`` and creates the account link.
    """
    interaction_token = interaction.get("token", "")
    identity = _interaction_user(interaction)
    if not identity:
        await _send_followup(interaction_token, content="Could not determine your Discord account.")
        return
    discord_user_id, discord_username = identity

    connect_token = str(uuid.uuid4())
    now = datetime.now(UTC)

    try:
        async with AsyncSessionLocal() as db:
            db.add(
                DiscordPendingConnect(
                    token=connect_token,
                    discord_user_id=discord_user_id,
                    discord_username=discord_username,
                    expires_at=now + _CONNECT_TOKEN_TTL,
                    created_at=now,
                )
            )
            await db.commit()
    except Exception:
        logger.exception("discord: /connect-account failed to mint token for %s", discord_user_id)
        await _send_followup(
            interaction_token, content="Failed to generate a connect link. Try again later."
        )
        return

    link = f"{FRONTEND_URL.rstrip('/')}/auth/discord/connect?token={connect_token}"
    await _send_followup(
        interaction_token,
        content=(
            f"Connect your Pioneer Square account: {link}\n"
            "This link expires in 15 minutes and can only be used once."
        ),
    )


async def _cmd_worker_spawn(interaction: dict) -> None:
    """Spawn a new worker via the same tool the Foreman AI uses to spawn workers.

    Requires the invoking Discord user to have linked their account with
    ``/connect-account`` first — spawning a worker starts a container and
    consumes credentials, so it's gated on the same account link used to
    resolve identity elsewhere (see ``discord/router.py::_resolve_identity``).

    Also rejects the request if the guild spawned a worker within the last
    ``worker_lifecycle.WORKER_SPAWN_COOLDOWN`` (default 5 min), to guard
    against accidental spam and the resource waste of repeated container
    starts — see ``worker_lifecycle.check_worker_spawn_cooldown``.
    """
    token = interaction.get("token", "")
    data = interaction.get("data", {})

    identity = _interaction_user(interaction)
    if not identity:
        await _send_followup(token, content="Could not determine your Discord account.")
        return
    discord_user_id, _ = identity

    options = {o["name"]: o for o in data.get("options", [])}
    repos_opt = options.get("repos")
    tools_opt = options.get("tools")
    repos = (
        [r.strip() for r in str(repos_opt["value"]).split(",") if r.strip()] if repos_opt else []
    )
    tools_list = (
        [t.strip() for t in str(tools_opt["value"]).split(",") if t.strip()] if tools_opt else []
    )

    guild_slug = await _resolve_command_guild_slug(interaction)

    try:
        async with AsyncSessionLocal() as db:
            link_result = await db.exec(
                select(col(DiscordAccountLink.ps_user_id)).where(
                    col(DiscordAccountLink.discord_user_id) == discord_user_id
                )
            )
            ps_user_id = link_result.one_or_none()
            if not ps_user_id:
                await _send_followup(
                    token,
                    content=(
                        "Your Discord account isn't linked to Pioneer Square yet. "
                        "Run `/connect-account` first, then try again."
                    ),
                )
                return

            guild_result = await db.exec(select(Guild).where(col(Guild.slug) == guild_slug))
            guild = guild_result.one_or_none()
            if not guild:
                await _send_followup(token, content=f"Pioneer guild `{guild_slug}` not found.")
                return

            from worker_lifecycle import check_worker_spawn_cooldown  # noqa: PLC0415

            remaining = await check_worker_spawn_cooldown(db, guild.id)
            if remaining is not None:
                minutes = max(1, math.ceil(remaining.total_seconds() / 60))
                await _send_followup(
                    token,
                    content=(
                        "Worker spawn cooldown active. Try again in "
                        f"{minutes} minute{'s' if minutes != 1 else ''}."
                    ),
                )
                return

            result_text, is_error = await spawn_worker(
                inp={"repos": repos or None, "tools": tools_list or None},
                guild_id=guild_slug,
                guild_pk=guild.id,
                db=db,
                user_id=ps_user_id,
            )

            if is_error:
                await _send_followup(token, content=f"Failed to spawn worker: {result_text}")
                return

            info = json.loads(result_text)
            worker_id = info.get("worker_id", "unknown")

            state_result = await db.exec(
                select(col(Worker.state)).where(col(Worker.id) == worker_id)
            )
            state = state_result.one_or_none() or "offline"
    except Exception:
        logger.exception("discord: /worker-spawn failed")
        await _send_followup(token, content="Failed to spawn worker.")
        return

    status = state if state == "online" else "queued"
    await _send_followup(token, embeds=_worker_spawn_embed(worker_id, status, repos, tools_list))

    # The popup above reflects a point-in-time check; the worker is typically
    # still mid-ECS-startup at this point. Keep polling in the background and
    # edit the same message in place once the outcome is known, instead of
    # leaving the popup stuck on "queued" — see issue #991.
    if status != "online":
        spawn(
            _await_worker_online(token, worker_id, repos, tools_list),
            name=f"discord-await-worker-online-{worker_id}",
        )


def _worker_spawn_embed(
    worker_id: str, status: str, repos: list[str], tools_list: list[str]
) -> list[dict]:
    color = {
        "online": 0x1ABC9C,  # teal, matches discord_notifier's "worker-online"
        "offline": 0xE74C3C,  # red — spawn failed, or the worker dropped before joining
    }.get(status, 0xF39C12)  # gold — still queued/starting
    return [
        {
            "title": f"Worker Spawned: {worker_id}",
            "description": (
                f"Status: **{status}**\n"
                f"Repos: {', '.join(repos) if repos else '—'}\n"
                f"Tools: {', '.join(tools_list) if tools_list else 'default'}"
            ),
            "color": color,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    ]


async def _await_worker_online(
    token: str, worker_id: str, repos: list[str], tools_list: list[str]
) -> None:
    """Poll a freshly-spawned worker's state and update the Discord popup in place.

    Runs as a background task (see ``util.tasks.spawn``) so the interaction
    handler itself doesn't block on ECS container startup. Discord follow-up
    messages have no server-side expiry, so editing ``@original`` once the
    worker actually reports online (or goes offline before joining) replaces
    the transient "queued" status with the real outcome, rather than leaving
    the popup looking stale for the ~minute a cold container takes to start.
    Gives up after ``_WORKER_ONLINE_TIMEOUT`` and reports a timeout message.
    """
    deadline = asyncio.get_event_loop().time() + _WORKER_ONLINE_TIMEOUT.total_seconds()
    try:
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(_WORKER_ONLINE_POLL_INTERVAL)
            async with AsyncSessionLocal() as db:
                state_result = await db.exec(
                    select(col(Worker.state)).where(col(Worker.id) == worker_id)
                )
                state = state_result.one_or_none()
            if state == "online":
                await _send_followup(
                    token, embeds=_worker_spawn_embed(worker_id, "online", repos, tools_list)
                )
                return
            if state == "offline":
                await _send_followup(
                    token, embeds=_worker_spawn_embed(worker_id, "offline", repos, tools_list)
                )
                return
    except Exception:
        logger.exception("discord: _await_worker_online failed for worker_id=%s", worker_id)
        return

    await _send_followup(
        token,
        content=(
            f"Worker `{worker_id}` is taking longer than expected to come online "
            "(ECS container may still be starting). Check `/ps workers` for current status."
        ),
    )


async def _permission_denied_message() -> str:
    return (
        "You need the `Manage Channels` permission or the "
        f"`{_operator_role_name()}` role to run this command."
    )


async def _cmd_join_channel(interaction: dict) -> None:
    """Wire a Discord channel to a Pioneer Square guild (creating or updating the binding)."""
    token = interaction.get("token", "")
    data = interaction.get("data", {})
    discord_guild_id = interaction.get("guild_id")

    if not discord_guild_id:
        await _send_followup(token, content="This command can only be used in a server.")
        return

    if not await _can_manage_channel_bindings(interaction):
        await _send_followup(token, content=await _permission_denied_message())
        return

    options = {o["name"]: o for o in data.get("options", [])}
    channel_opt = options.get("channel")
    if not channel_opt:
        await _send_followup(token, content="A channel must be specified.")
        return
    discord_channel_id = str(channel_opt["value"])

    guild_opt = options.get("guild")
    guild_slug = str(guild_opt["value"]).strip() if guild_opt else None

    try:
        async with AsyncSessionLocal() as db:
            if guild_slug:
                result = await db.exec(select(Guild).where(col(Guild.slug) == guild_slug))
                if result.one_or_none() is None:
                    await _send_followup(token, content=f"Pioneer guild `{guild_slug}` not found.")
                    return
            else:
                all_guilds = (
                    await db.exec(select(Guild).where(col(Guild.deleted_at).is_(None)))
                ).all()
                if len(all_guilds) != 1:
                    await _send_followup(
                        token,
                        content=(
                            "Multiple Pioneer guilds are configured — specify `guild:<slug>`."
                            if len(all_guilds) > 1
                            else "No Pioneer guilds are configured."
                        ),
                    )
                    return
                guild_slug = all_guilds[0].slug

            existing_result = await db.exec(
                select(DiscordChannelGuild).where(
                    col(DiscordChannelGuild.discord_guild_id) == discord_guild_id,
                    col(DiscordChannelGuild.discord_channel_id) == discord_channel_id,
                )
            )
            existing = existing_result.one_or_none()
            if existing:
                existing.ps_guild_id = guild_slug
                db.add(existing)
                await db.commit()
                await _send_followup(
                    token,
                    content=(
                        f"Updated binding: <#{discord_channel_id}> is now wired to "
                        f"Pioneer Square guild `{guild_slug}`."
                    ),
                )
                return

            db.add(
                DiscordChannelGuild(
                    discord_guild_id=discord_guild_id,
                    discord_channel_id=discord_channel_id,
                    ps_guild_id=guild_slug,
                    created_at=datetime.now(UTC),
                )
            )
            await db.commit()
    except Exception:
        logger.exception(
            "discord: /join-channel failed channel=%s guild=%s", discord_channel_id, guild_slug
        )
        await _send_followup(token, content="Failed to wire channel to guild.")
        return

    await _send_followup(
        token,
        content=(
            f"✅ <#{discord_channel_id}> is now wired to Pioneer Square guild `{guild_slug}`.\n"
            "Task events, PR notifications, and CI alerts will be posted here."
        ),
    )


async def _cmd_leave_channel(interaction: dict) -> None:
    """Remove a Discord channel's Pioneer Square guild binding."""
    token = interaction.get("token", "")
    data = interaction.get("data", {})
    discord_guild_id = interaction.get("guild_id")

    if not discord_guild_id:
        await _send_followup(token, content="This command can only be used in a server.")
        return

    if not await _can_manage_channel_bindings(interaction):
        await _send_followup(token, content=await _permission_denied_message())
        return

    options = {o["name"]: o for o in data.get("options", [])}
    channel_opt = options.get("channel")
    if channel_opt:
        discord_channel_id = str(channel_opt["value"])
    else:
        current_channel_id = interaction.get("channel_id") or (
            interaction.get("channel") or {}
        ).get("id")
        if not current_channel_id:
            await _send_followup(token, content="Could not determine the current channel.")
            return
        discord_channel_id = str(current_channel_id)

    try:
        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(DiscordChannelGuild).where(
                    col(DiscordChannelGuild.discord_guild_id) == discord_guild_id,
                    col(DiscordChannelGuild.discord_channel_id) == discord_channel_id,
                )
            )
            existing = result.one_or_none()
            if not existing:
                await _send_followup(token, content="No binding found for this channel.")
                return
            await db.delete(existing)
            await db.commit()
    except Exception:
        logger.exception("discord: /leave-channel failed channel=%s", discord_channel_id)
        await _send_followup(token, content="Failed to remove channel binding.")
        return

    await _send_followup(
        token, content=f"✅ <#{discord_channel_id}> has been unwired from Pioneer Square."
    )


# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------


async def _dispatch_command(interaction: dict) -> None:
    """Parse the interaction data and dispatch to the appropriate command handler."""
    token = interaction.get("token", "")
    data = interaction.get("data", {})
    command_name = data.get("name", "")

    if command_name == "join-channel":
        await _cmd_join_channel(interaction)
        return
    if command_name == "leave-channel":
        await _cmd_leave_channel(interaction)
        return
    if command_name == "connect-account":
        await _cmd_connect_account(interaction)
        return
    if command_name == "worker-spawn":
        await _cmd_worker_spawn(interaction)
        return

    guild_slug = await _resolve_command_guild_slug(interaction)

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
        logger.error("discord: DISCORD_PUBLIC_KEY is not configured; refusing all requests")
        return Response(content="Invalid signature", status_code=401)

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
