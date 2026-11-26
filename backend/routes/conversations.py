"""Conversation-level lifecycle routes: rename, close (issue #1278).

``Conversation.discord_thread_id`` is the source of truth for a
conversation's Discord binding (see its docstring in ``models.py``) — these
routes are the human/bot-triggered entry point for driving that binding
*from* the conversation, mirroring a rename/close straight onto the Discord
thread via ``foreman.conversation_service.rename_conversation``/
``close_conversation``, with no need to look up the conversation's current
``Thread`` row first. This complements ``routes/threads.py``'s
archive/close endpoints, which remain the Thread-instance-scoped
counterpart (a conversation can outlive many threads — see ``Thread``'s
docstring).
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import UTC, datetime

import discord_notifier
from auth_deps import get_guild_pk, require_member
from database import get_db_dep
from events import broadcast_msg
from fastapi import APIRouter, Depends, HTTPException
from foreman import triggers
from foreman.conversation_service import (
    close_conversation,
    reactivate_conversation,
    rename_conversation,
    touch_conversation,
)
from foreman.runner import reset_foreman_poll
from message_artifacts import message_payloads
from message_authors import user_authors
from models import THREAD_STATUSES, Conversation, Message
from pydantic import BaseModel
from sqlalchemy import func, literal_column
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from ws_types import ChatMsg

router = APIRouter()


class ConversationOut(BaseModel):
    id: int
    guild_id: int
    user_id: str | None
    name: str | None
    status: str
    discord_thread_id: str | None
    created_at: datetime
    updated_at: datetime
    # List endpoint only: latest plain Foreman reply and distinct plain-chat senders.
    preview: str | None = None
    participants: list[str] = []


def _to_out(conversation: Conversation) -> ConversationOut:
    return ConversationOut(
        id=conversation.id,
        guild_id=conversation.guild_id,
        user_id=conversation.user_id,
        name=conversation.name,
        status=conversation.status,
        discord_thread_id=conversation.discord_thread_id,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


class ConversationRename(BaseModel):
    name: str


async def _get_conversation_in_guild(
    db: AsyncSession, guild_id: str, conversation_id: int
) -> Conversation:
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(Conversation).where(
            col(Conversation.id) == conversation_id, col(Conversation.guild_id) == guild_pk
        )
    )
    conversation = result.first()
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.patch(
    "/api/guilds/{guild_id}/conversations/{conversation_id}", response_model=ConversationOut
)
async def rename_conversation_route(
    guild_id: str,
    conversation_id: int,
    body: ConversationRename,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Rename a conversation, renaming its mirrored Discord thread to match."""
    conversation = await _get_conversation_in_guild(db, guild_id, conversation_id)
    await rename_conversation(db, conversation, body.name)
    await db.refresh(conversation)
    return _to_out(conversation)


@router.patch(
    "/api/guilds/{guild_id}/conversations/{conversation_id}/close", response_model=ConversationOut
)
async def close_conversation_route(
    guild_id: str,
    conversation_id: int,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Close a conversation, archiving its mirrored Discord thread to match."""
    conversation = await _get_conversation_in_guild(db, guild_id, conversation_id)
    await close_conversation(db, conversation)
    await db.refresh(conversation)
    return _to_out(conversation)


@router.get(
    "/api/guilds/{guild_id}/conversations/{conversation_id}", response_model=ConversationOut
)
async def get_conversation(
    guild_id: str,
    conversation_id: int,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    return _to_out(await _get_conversation_in_guild(db, guild_id, conversation_id))


# Basic per-user rate limit for reply posting (#1311): blunts accidental
# double-submits and naive spam. Per-process, in-memory only — fine for the
# single-backend-instance deployment today; move to a shared store if the
# backend ever runs replicated.
_RATE_LIMIT_WINDOW_SECONDS = 10.0
_RATE_LIMIT_MAX_MESSAGES = 5
_recent_message_times: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(user_id: str) -> None:
    now = time.monotonic()
    window_start = now - _RATE_LIMIT_WINDOW_SECONDS
    recent = [t for t in _recent_message_times[user_id] if t > window_start]
    if len(recent) >= _RATE_LIMIT_MAX_MESSAGES:
        _recent_message_times[user_id] = recent
        raise HTTPException(status_code=429, detail="Too many messages — please slow down")
    recent.append(now)
    _recent_message_times[user_id] = recent


class ConversationMessageCreate(BaseModel):
    content: str


class ConversationMessageOut(BaseModel):
    id: int
    conversationId: int
    content: str
    createdAt: str


@router.post(
    "/api/guilds/{guild_id}/conversations/{conversation_id}/messages",
    response_model=ConversationMessageOut,
)
async def post_conversation_message(
    guild_id: str,
    conversation_id: int,
    body: ConversationMessageCreate,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Post a user message into an existing conversation and trigger the Foreman (#1297).

    Scoped explicitly by ``conversation_id`` rather than the (guild, user)
    heuristic ``ensure_conversation_thread`` uses — a user can have more than
    one open ``Conversation`` (#1296), so this always lands the message (and
    the Foreman run it triggers) in the exact conversation the caller picked,
    never a different one for the same user. The triggered run sees this
    conversation's own tasks/events via the "This conversation" state block
    (``foreman.runner._load_conversation_context``) and decides whether to
    call ``send_followup`` on one of them or reply directly — the "followups
    inside a conversation" pattern from Epic #1271.
    """
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content must not be empty")

    _check_rate_limit(github_user_id)

    conversation = await _get_conversation_in_guild(db, guild_id, conversation_id)
    if conversation.user_id is not None and conversation.user_id != github_user_id:
        raise HTTPException(status_code=403, detail="Not the owner of this conversation")

    created_at = datetime.now(UTC)
    message = Message(
        guild_id=conversation.guild_id,
        from_agent="user",
        to_agent="foreman",
        content=content,
        message_type="chat",
        created_at=created_at,
        user_id=github_user_id,
        conversation_id=conversation.id,
        source="api",
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)

    # A new message revives an archived/closed conversation (also bumps updated_at).
    if conversation.status != "active":
        await reactivate_conversation(db, conversation)
    else:
        await touch_conversation(db, conversation)

    await broadcast_msg(
        guild_id,
        ChatMsg.model_validate(
            {
                "from": "user",
                "to": "foreman",
                "content": content,
                "createdAt": created_at.isoformat(),
                "userId": github_user_id,
                "conversationId": conversation.id,
                **(await user_authors(db, [github_user_id])).get(github_user_id, {}),
            }
        ),
    )

    await discord_notifier.notify_foreman_chat(
        guild_id, f"**User:** {content}", conversation_id=conversation.id
    )
    await triggers.trigger_foreman(
        guild_id,
        "conversation-message",
        triggers.format_conversation_message(conversation.id, content),
        user_id=github_user_id,
        conversation_id=conversation.id,
        task_name=f"foreman.conversation-message:{conversation.id}",
    )
    reset_foreman_poll(guild_id)

    return ConversationMessageOut(
        id=message.id,
        conversationId=conversation.id,
        content=content,
        createdAt=created_at.isoformat(),
    )


@router.get("/api/guilds/{guild_id}/conversations", response_model=list[ConversationOut])
async def list_conversations(
    guild_id: str,
    status: str | None = None,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
    q: str | None = None,
):
    """List a guild's conversations, newest created first (#1298, #1380).

    The Conversations UI/API surface's list endpoint — mirrors
    ``routes.threads.list_threads`` (same ``status`` filter, same 200-row
    cap) but queries :class:`models.Conversation` directly rather than
    joining through ``Thread``, since ``name``/``status`` already live on
    the conversation itself (see its docstring in ``models.py``).
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    latest_reply = (
        select(func.left(col(Message.content), 200))
        .where(
            col(Message.conversation_id) == col(Conversation.id),
            col(Message.guild_id) == guild_pk,
            col(Message.from_agent) == "foreman",
            col(Message.role).is_(None),
        )
        .order_by(col(Message.created_at).desc(), col(Message.id).desc())
        .limit(1)
        .correlate(Conversation)
        .scalar_subquery()
    )
    stmt = (
        select(Conversation, latest_reply.label("preview"))
        .where(col(Conversation.guild_id) == guild_pk)
        .order_by(col(Conversation.created_at).desc())
    )
    if status is not None:
        if status not in THREAD_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status!r}")
        stmt = stmt.where(col(Conversation.status) == status)
    if q and (query := q.strip()):
        # A literal configuration keeps these expressions identical to the GIN indexes.
        config = literal_column("'english'::regconfig")
        terms = func.plainto_tsquery(config, query)
        matching_conversations = select(Message.conversation_id).where(
            col(Message.guild_id) == guild_pk,
            col(Message.conversation_id).is_not(None),
            func.to_tsvector(config, col(Message.content)).bool_op("@@")(terms),
        )
        matching_names = select(Conversation.id).where(
            col(Conversation.guild_id) == guild_pk,
            func.to_tsvector(
                config, func.coalesce(col(Conversation.name), literal_column("''"))
            ).bool_op("@@")(terms),
        )
        stmt = stmt.where(col(Conversation.id).in_(matching_names.union(matching_conversations)))
    out = []
    for conversation, preview in (await db.exec(stmt.limit(200))).all():
        item = _to_out(conversation)
        item.preview = preview
        out.append(item)
    by_id = {c.id: c for c in out}
    if by_id:
        # Keep first-seen participant order without transferring message histories.
        first_senders = (
            select(
                Message.conversation_id,
                Message.from_agent,
                Message.user_id,
                Message.created_at,
                Message.id,
            )
            .where(
                col(Message.conversation_id).in_(by_id),
                col(Message.guild_id) == guild_pk,
                col(Message.role).is_(None),
            )
            .distinct(Message.conversation_id, Message.from_agent, Message.user_id)
            .order_by(
                Message.conversation_id,
                Message.from_agent,
                Message.user_id,
                Message.created_at,
                Message.id,
            )
            .subquery()
        )
        rows = await db.exec(
            select(
                first_senders.c.conversation_id,
                first_senders.c.from_agent,
                first_senders.c.user_id,
            ).order_by(first_senders.c.created_at, first_senders.c.id)
        )
        for conversation_id, from_agent, user_id in rows.all():
            conv = by_id[conversation_id]
            sender = from_agent or user_id
            if sender and sender not in conv.participants:
                conv.participants.append(sender)
    return out


@router.get("/api/guilds/{guild_id}/conversations/{conversation_id}/messages")
async def list_conversation_messages(
    guild_id: str,
    conversation_id: int,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
    *,
    before_id: int | None = None,
):
    """Return this conversation's message history, oldest first (#1298).

    Pages contain up to 100 rows, scoped directly by conversation. Pass the
    oldest row's id as ``before_id`` to read earlier activity.
    """
    conversation = await _get_conversation_in_guild(db, guild_id, conversation_id)
    query = select(Message).where(col(Message.conversation_id) == conversation.id)
    if before_id is not None:
        cursor = (await db.exec(query.where(col(Message.id) == before_id))).one_or_none()
        if cursor is None:
            raise HTTPException(status_code=404, detail="Message not found in conversation")
        query = query.where(
            (col(Message.created_at) < cursor.created_at)
            | ((col(Message.created_at) == cursor.created_at) & (col(Message.id) < cursor.id))
        )
    result = await db.exec(
        query.order_by(col(Message.created_at).desc(), col(Message.id).desc()).limit(100)
    )
    messages = result.all()
    return await message_payloads(reversed(messages), db, guild_id, conversation.guild_id)
