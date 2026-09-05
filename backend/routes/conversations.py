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

from auth_deps import get_guild_pk, require_member
from database import get_db_dep
from fastapi import APIRouter, Depends, HTTPException
from foreman.conversation_service import close_conversation, rename_conversation
from models import Conversation
from pydantic import BaseModel
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter()


class ConversationOut(BaseModel):
    id: int
    guild_id: int
    user_id: str | None
    name: str | None
    status: str
    discord_thread_id: str | None


def _to_out(conversation: Conversation) -> ConversationOut:
    return ConversationOut(
        id=conversation.id,
        guild_id=conversation.guild_id,
        user_id=conversation.user_id,
        name=conversation.name,
        status=conversation.status,
        discord_thread_id=conversation.discord_thread_id,
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
