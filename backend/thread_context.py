"""Thread-local (task-local) storage for the current thread/conversation.

Part of the thread-per-conversation architecture (epic #1160, issue #1163).
Deep call chains — a route handler calling into a foreman tool calling into
``discord_notifier`` — need to know which :class:`models.Thread` /
``Conversation`` a piece of work belongs to without threading ``thread_id``
through every function signature in between. ``ThreadContextManager`` is that
side channel.

Built on :class:`contextvars.ContextVar`, not ``threading.local``: this
codebase is asyncio-based (a single OS thread runs many concurrent requests),
so a real thread-local would leak context across unrelated concurrent
requests sharing that thread. ``ContextVar`` is copy-on-task-creation, so each
``asyncio.Task`` (i.e. each request, each background job) gets its own
isolated value — the async-native equivalent of thread-local storage.

Usage::

    with ThreadContextManager.bind(guild_id=1, conversation_id=42, thread_id="th-abc123"):
        ...  # anything called in here can read ThreadContextManager.current()

FastAPI integration: ``request_scoped`` is a dependency that resolves the
current thread context from the ``thread_id`` path/query parameter (if any)
and binds it for the lifetime of the request via a yield-dependency, mirroring
how ``request.state`` is conventionally used for per-request data — see
``routes/threads.py`` and ``routes/tasks.py`` for callers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import Request


@dataclass(frozen=True, slots=True)
class ThreadContext:
    """The currently active (guild, conversation, thread) triple."""

    guild_id: int
    conversation_id: int
    thread_id: str | None


class ThreadContextManager:
    """Namespace for binding/reading the current :class:`ThreadContext`.

    Not instantiated — every method is a classmethod/staticmethod operating
    on one module-level ``ContextVar``, so callers don't need a reference to
    a shared instance (there's nothing to share; the context var itself is
    the shared, isolated-per-task state).
    """

    _var: ContextVar[ThreadContext | None] = ContextVar(
        "thread_context_manager_current", default=None
    )

    @classmethod
    def current(cls) -> ThreadContext | None:
        """Return the active :class:`ThreadContext`, or None outside any binding."""
        return cls._var.get()

    @classmethod
    @contextmanager
    def bind(
        cls, guild_id: int, conversation_id: int, thread_id: str | None
    ) -> Iterator[ThreadContext]:
        """Bind a :class:`ThreadContext` for the duration of the ``with`` block.

        Restores whatever was active before on exit (supports nesting — e.g. a
        request-scoped binding wrapping a narrower per-tool-call binding).
        """
        ctx = ThreadContext(guild_id=guild_id, conversation_id=conversation_id, thread_id=thread_id)
        token = cls._var.set(ctx)
        try:
            yield ctx
        finally:
            cls._var.reset(token)


async def request_scoped(request: Request) -> AsyncIterator[ThreadContext | None]:
    """FastAPI dependency: bind the request's thread context for its lifetime.

    Resolves ``thread_id`` from the path (``{thread_id}``) or query string
    when present, looks up its owning conversation, and exposes both on
    ``request.state.thread_context`` in addition to the contextvar binding —
    so handlers can read it either way (``request.state`` for the
    request-object-carrying code paths already used throughout ``routes/``,
    the contextvar for code — foreman tools, ``discord_notifier`` — that only
    ever sees a ``guild_id``/``task_id``, never the ``Request``).

    A no-op passthrough (binds nothing, yields None) when no thread_id is
    resolvable on this request — most routes aren't thread-scoped at all.
    """
    thread_id = request.path_params.get("thread_id") or request.query_params.get("thread_id")
    if not thread_id:
        request.state.thread_context = None
        yield None
        return

    from database import get_db  # noqa: PLC0415 — avoid import cycles at module load
    from models import Conversation, Thread  # noqa: PLC0415
    from sqlmodel import col, select  # noqa: PLC0415

    db = await get_db()
    try:
        result = await db.exec(
            select(col(Thread.id), col(Thread.conversation_id), col(Conversation.guild_id))
            .join(Conversation, col(Thread.conversation_id) == col(Conversation.id))
            .where(col(Thread.id) == thread_id, col(Thread.deleted_at).is_(None))
        )
        row = result.one_or_none()
    finally:
        await db.close()

    if row is None:
        request.state.thread_context = None
        yield None
        return

    resolved_thread_id, conversation_id, guild_id = row

    with ThreadContextManager.bind(
        guild_id=guild_id, conversation_id=conversation_id, thread_id=resolved_thread_id
    ) as ctx:
        request.state.thread_context = ctx
        yield ctx
