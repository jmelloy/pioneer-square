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

from datetime import UTC, datetime

from auth_deps import get_guild_pk, require_member
from database import get_db_dep
from events import broadcast_msg
from fastapi import APIRouter, Depends, HTTPException
from foreman import triggers
from foreman.conversation_service import close_conversation, rename_conversation, touch_conversation
from foreman.runner import reset_foreman_poll
from models import THREAD_STATUSES, Conversation, Message
from pydantic import BaseModel
from routes.guilds import _message_dict
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
            }
        ),
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
):
    """List a guild's conversations, most recently active first (#1298).

    The Conversations UI/API surface's list endpoint — mirrors
    ``routes.threads.list_threads`` (same ``status`` filter, same 200-row
    cap) but queries :class:`models.Conversation` directly rather than
    joining through ``Thread``, since ``name``/``status`` already live on
    the conversation itself (see its docstring in ``models.py``).
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    stmt = (
        select(Conversation)
        .where(col(Conversation.guild_id) == guild_pk)
        .order_by(col(Conversation.updated_at).desc())
    )
    if status is not None:
        if status not in THREAD_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status!r}")
        stmt = stmt.where(col(Conversation.status) == status)
    result = await db.exec(stmt.limit(200))
    return [_to_out(c) for c in result.all()]


@router.get("/api/guilds/{guild_id}/conversations/{conversation_id}/messages")
async def list_conversation_messages(
    guild_id: str,
    conversation_id: int,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Return this conversation's message history, oldest first (#1298).

    Mirrors ``routes.threads.list_thread_messages`` — same ``_message_dict``
    serialization, same 100-row cap — but scoped directly by
    ``conversation_id`` instead of resolving it via a ``Thread`` row first.
    """
    conversation = await _get_conversation_in_guild(db, guild_id, conversation_id)
    result = await db.exec(
        select(Message)
        .where(col(Message.conversation_id) == conversation.id)
        .order_by(col(Message.created_at).desc(), col(Message.id).desc())
        .limit(100)
    )
    messages = result.all()
    return [_message_dict(m) for m in reversed(messages)]
