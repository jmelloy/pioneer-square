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


async def get_conversation_by_discord_thread_id(
    db: AsyncSession, guild_pk: int, discord_thread_id: str
) -> Conversation | None:
    """Find the conversation bound to a Discord thread (issue #1278).

    ``Conversation.discord_thread_id`` is the source of truth for a
    conversation's Discord binding (see its docstring in ``models.py``) —
    this is the direct lookup path a reply posted into an existing Discord
    thread uses to find its conversation, instead of re-deriving it from the
    (guild, user) active-thread heuristic (see ``discord.router``). Still
    returns a conversation whose mirrored thread has since gone
    archived/closed — see ``foreman.thread_service.reactivate_conversation_thread``,
    which callers use to un-archive it when a user replies there anyway.
    """
    result = await db.exec(
        select(Conversation).where(
            col(Conversation.guild_id) == guild_pk,
            col(Conversation.discord_thread_id) == discord_thread_id,
        )
    )
    return result.first()


async def rename_conversation(db: AsyncSession, conversation: Conversation, name: str) -> None:
    """Rename *conversation* and mirror the new name onto its Discord thread (#1278).

    Also renames the conversation's current active ``Thread`` row, if any, so
    a later Thread-driven sync (``thread_service.sync_conversation_after_thread_update``)
    can't clobber this rename back to the old name. Commits *db* itself.
    """
    from foreman.thread_service import _active_thread_for_conversation  # noqa: PLC0415

    now = datetime.now(UTC)
    conversation.name = name
    conversation.updated_at = now
    db.add(conversation)

    thread = await _active_thread_for_conversation(db, conversation.id)
    if thread is not None:
        thread.name = name
        thread.updated_at = now
        db.add(thread)

    await db.commit()

    if conversation.discord_thread_id:
        from discord.thread_mirror import rename_conversation_thread  # noqa: PLC0415

        await rename_conversation_thread(conversation.discord_thread_id, name)


async def close_conversation(db: AsyncSession, conversation: Conversation) -> None:
    """Close *conversation* and archive its mirrored Discord thread (#1278).

    Mirrors ``rename_conversation``'s pattern: writes both ``Conversation``
    and its current active ``Thread`` (if any) so the two can't drift, then
    archives Discord directly from ``Conversation.discord_thread_id`` — no
    Thread lookup needed on the Discord side, since ``discord_thread_id`` is
    the source of truth for the binding. Commits *db* itself.
    """
    from foreman.thread_service import _active_thread_for_conversation  # noqa: PLC0415

    now = datetime.now(UTC)
    conversation.status = "closed"
    conversation.updated_at = now
    db.add(conversation)

    thread = await _active_thread_for_conversation(db, conversation.id)
    if thread is not None:
        thread.status = "closed"
        thread.updated_at = now
        db.add(thread)

    await db.commit()

    if conversation.discord_thread_id:
        from discord.thread_mirror import archive_conversation_thread_by_id  # noqa: PLC0415

        await archive_conversation_thread_by_id(conversation.discord_thread_id)
