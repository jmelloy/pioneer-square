"""Conversation resolution (issue #1271: make Conversation the core Foreman
thread model).

The previous attempt at this issue (#1272) went the wrong direction: it added
a *new* ``thread_id`` FK to ``ForemanTurn`` and switched history loading to be
``Thread``-scoped — the opposite of #1271's stated goal, which makes
``Thread`` more load-bearing rather than less. Review flagged that as an
architectural mismatch (see #1272's review comment).

This module is the small, additive first step #1271 actually asks for
("Suggested first PR"): a ``conversation_service`` wrapper that resolves/
creates :class:`models.Conversation` rows, so call sites can start stamping
``conversation_id`` on ``messages``/``tasks``/``foreman_turns``/
``github_events`` *alongside* the existing ``thread_id`` — additive, not a
replacement. It deliberately does not touch history loading (still windowed
by ``(guild_id, user_id)``, see ``foreman.history``) or delete/rename
anything ``Thread``-related; that's later-phase work per the issue.

Discord mirroring stays exactly where it already lives
(``foreman.thread_service`` / ``discord.thread_mirror``) — this module only
resolves/creates the ``Conversation`` anchor row, it does not own any
Discord side effects.
"""

from __future__ import annotations

from datetime import UTC, datetime

from models import Conversation, Task
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession


async def get_or_create_conversation(db: AsyncSession, guild_pk: int, user_id: str) -> Conversation:
    """Get-or-create the one :class:`Conversation` for this (guild, user) pair.

    ``Conversation`` is 1:1 with (guild_id, user_id) — see its docstring in
    models.py — unlike ``Thread``, of which many may exist for the same
    conversation over time. This is the canonical definition;
    ``foreman.thread_service`` imports it from here rather than duplicating it.
    """
    result = await db.exec(
        select(Conversation).where(
            col(Conversation.guild_id) == guild_pk, col(Conversation.user_id) == user_id
        )
    )
    conversation = result.first()
    if conversation is not None:
        return conversation
    now = datetime.now(UTC)
    conversation = Conversation(guild_id=guild_pk, user_id=user_id, created_at=now, updated_at=now)
    db.add(conversation)
    await db.flush()
    return conversation


async def resolve_conversation_id(
    db: AsyncSession, guild_pk: int, *, task_id: str | None = None, user_id: str | None = None
) -> int | None:
    """Resolve the :class:`Conversation` id a new message/turn/task belongs to.

    Mirrors ``thread_service.resolve_thread_id``'s precedence: prefers
    *task_id*'s already-stamped conversation (an explicit, stable binding set
    at task-creation time) over *user_id*'s conversation. Unlike
    ``resolve_thread_id`` — which is read-only because a ``Thread`` is a
    side-effecting, explicitly-created object — this *does* create a
    ``Conversation`` when only *user_id* is given, because ``Conversation``
    is a plain 1:1-with-(guild, user) anchor row with no side effects
    (no Discord mirror, no WS broadcast) of its own.

    Commits *db* itself when it creates a new conversation — most callers
    resolve this from a short-lived, read-only session (see
    ``foreman.runner._run_foreman_ai``) that gets ``close()``'d, not
    ``commit()``'d, once history/thread lookups are done; without an explicit
    commit here, ``get_or_create_conversation``'s flush-only insert would be
    silently rolled back on close, leaving every row this run stamps with a
    ``conversation_id`` that was never actually persisted (a dangling FK that
    fails at the *next* session's commit, not this one — easy to miss).
    """
    if task_id:
        result = await db.exec(select(col(Task.conversation_id)).where(col(Task.id) == task_id))
        conversation_id = result.first()
        if conversation_id is not None:
            return conversation_id
    if user_id:
        conversation = await get_or_create_conversation(db, guild_pk, user_id)
        await db.commit()
        return conversation.id
    return None


async def touch_conversation(db: AsyncSession, conversation: Conversation) -> None:
    """Bump a conversation's ``updated_at`` to now and commit."""
    conversation.updated_at = datetime.now(UTC)
    db.add(conversation)
    await db.commit()
