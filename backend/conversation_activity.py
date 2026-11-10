"""Publish worker and GitHub activity into the owning conversation, without waking the Foreman."""

from datetime import UTC, datetime

import discord_notifier
from events import broadcast_msg
from models import Message
from sqlmodel.ext.asyncio.session import AsyncSession
from util.tasks import spawn
from ws_types import ChatMsg


def github_activity(event_type: str, action: str | None, payload: dict) -> str | None:
    """Human-facing PR activity, independent of the Foreman's trigger policy."""
    node = payload.get(event_type) or {}
    if event_type in {"check_run", "check_suite"}:
        if node.get("status") != "completed":
            return None
        name = node.get("name") or (node.get("app") or {}).get("slug") or "CI"
        output = node.get("output") or {}
        text = f"**{name}: {node.get('conclusion') or 'completed'}**"
        for key in ("title", "summary", "text"):
            if output.get(key):
                text += f"\n\n{output[key]}"
        url = node.get("html_url") or node.get("details_url")
    elif event_type == "status":
        text = f"**{payload.get('context') or 'CI'}: {payload.get('state')}**"
        if payload.get("description"):
            text += f"\n{payload['description']}"
        url = payload.get("target_url")
    elif event_type in {"pull_request", "pull_request_review", "pull_request_review_comment"}:
        if event_type == "pull_request_review_comment":
            node = payload.get("comment") or {}
        label = "PR merged" if node.get("merged") else event_type.replace("_", " ")
        text = f"**{label}: {node.get('state') or action or 'updated'}**"
        if node.get("title"):
            text += f"\n{node['title']}"
        if node.get("body") and event_type != "pull_request":
            text += f"\n\n{node['body']}"
        url = node.get("html_url")
    elif event_type == "issue_comment" and action == "created":
        node = payload.get("comment") or {}
        text = f"**GitHub comment**\n{node.get('body') or ''}"
        url = node.get("html_url")
    else:
        return None
    # Keep check reports bounded; the source link retains the full output.
    if len(text) > 8000:
        text = text[:8000] + "\n…[truncated]"
    if url and str(url).startswith("https://"):
        text += f"\n\n[View on GitHub](<{url}>)"
    return text


async def publish_activity(
    db: AsyncSession,
    *,
    guild_id: str,
    guild_pk: int,
    conversation_id: int | None,
    task_id: str | None,
    sender: str,
    content: str,
    source: str,
    created_at: datetime | None = None,
    mirror: bool = True,
) -> None:
    """Persist before broadcasting; callers supply a guild-validated conversation."""
    if conversation_id is None or not content.strip():
        return
    created_at = created_at or datetime.now(UTC)
    db.add(
        Message(
            guild_id=guild_pk,
            conversation_id=conversation_id,
            task_id=task_id,
            from_agent=sender,
            to_agent="user",
            content=content,
            message_type="activity",
            source=source,
            created_at=created_at,
        )
    )
    await db.commit()
    await broadcast_msg(
        guild_id,
        ChatMsg(
            from_=sender,
            to="user",
            content=content,
            taskId=task_id,
            conversationId=conversation_id,
            source=source,
            createdAt=created_at.isoformat(),
        ),
    )
    if mirror:
        spawn(
            discord_notifier.notify_conversation_activity(
                guild_id,
                conversation_id,
                f"**{sender}**" + (f" · `{task_id}`" if task_id else "") + f"\n{content}",
            ),
            name=f"discord.conversation-activity:{conversation_id}",
        )
