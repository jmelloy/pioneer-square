"""Tests for foreman.journal.TurnJournal — the single write-per-block seam.

Before #1241 the same event was written up to three times by hand (a WS
broadcast, a Message row, and — for text — a *different-granularity*
concatenated Message row) inside foreman.runner._run_foreman_ai. These tests
assert TurnJournal.text()/tool_use()/tool_result() each do exactly one WS
broadcast + one Message insert, at the same granularity, and that
system()/human()/assistant_turn()/tool_response_turn() persist one
ForemanTurn row per call.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from _test_config import TEST_DATABASE_URL  # noqa: E402
from auth_deps import get_guild_pk
from database import get_db
from foreman.journal import ForemanReply, TurnJournal
from foreman.ports import SystemClock
from helpers import create_db, insert_guild
from models import ForemanTurn, Message


@pytest.fixture()
def db_session(monkeypatch):
    from helpers import truncate_all

    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)
    db_url = TEST_DATABASE_URL

    monkeypatch.setenv("DATABASE_URL", db_url)

    engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)

    yield db_url


class _RecordingEvents:
    def __init__(self) -> None:
        self.broadcast_calls: list = []
        self.broadcast_msg_calls: list = []

    async def broadcast(self, guild_id, message):
        self.broadcast_calls.append((guild_id, message))

    async def broadcast_msg(self, guild_id, message):
        self.broadcast_msg_calls.append((guild_id, message))

    async def emit_terminal_line(self, guild_id, agent_id, line):
        pass


class _InlineScheduler:
    """Runs spawned coroutines inline instead of scheduling them, so the
    Discord-mirror side effect never escapes into the test's own event loop."""

    def spawn(self, coro, *, name=None):
        coro.close()
        return None


async def _make_journal(db_session, guild_id: str, *, task_id=None, thread_id=None):
    insert_guild(db_session, guild_id)
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
    finally:
        await db.close()

    events = _RecordingEvents()
    journal = TurnJournal(
        guild_id=guild_id,
        guild_pk=guild_pk,
        user_id="u-1",
        reply=ForemanReply(
            guild_id=guild_id,
            user_id="u-1",
            task_id=task_id,
            thread_id=thread_id,
            discord_task_id=task_id,
            discord_channel_id=None,
        ),
        events=events,
        scheduler=_InlineScheduler(),
        clock=SystemClock(),
    )
    return journal, events, guild_pk


async def _fetch_messages(guild_pk: int) -> list[Message]:
    db = await get_db()
    try:
        result = await db.exec(
            select(Message).where(col(Message.guild_id) == guild_pk).order_by(col(Message.id))
        )
        return list(result.all())
    finally:
        await db.close()


async def _fetch_turns(guild_pk: int) -> list[ForemanTurn]:
    db = await get_db()
    try:
        result = await db.exec(
            select(ForemanTurn)
            .where(col(ForemanTurn.guild_id) == guild_pk)
            .order_by(col(ForemanTurn.id))
        )
        return list(result.all())
    finally:
        await db.close()


async def test_text_does_one_broadcast_and_one_message_insert(db_session):
    journal, events, guild_pk = await _make_journal(db_session, "g-journal-text")

    await journal.text("hello there")

    assert len(events.broadcast_msg_calls) == 1
    assert events.broadcast_msg_calls[0][1].content == "hello there"

    messages = await _fetch_messages(guild_pk)
    assert len(messages) == 1
    assert messages[0].content == "hello there"
    assert messages[0].role is None


async def test_tool_use_does_one_broadcast_and_one_message_insert(db_session):
    journal, events, guild_pk = await _make_journal(db_session, "g-journal-tool-use")
    tu = SimpleNamespace(name="get_task_status", id="tu-1", input={"task_id": "t-1"})

    await journal.tool_use(tu)

    assert len(events.broadcast_msg_calls) == 1
    assert events.broadcast_msg_calls[0][1].toolName == "get_task_status"

    messages = await _fetch_messages(guild_pk)
    assert len(messages) == 1
    assert messages[0].role == "tool_use"
    assert "get_task_status" in messages[0].content


async def test_tool_result_does_one_broadcast_and_one_message_insert(db_session):
    journal, events, guild_pk = await _make_journal(db_session, "g-journal-tool-result")

    await journal.tool_result({"tool_use_id": "tu-1", "content": "ok", "is_error": False})

    assert len(events.broadcast_msg_calls) == 1
    assert events.broadcast_msg_calls[0][1].toolOutput == "ok"

    messages = await _fetch_messages(guild_pk)
    assert len(messages) == 1
    assert messages[0].role == "tool_result"
    assert messages[0].content == "ok"


async def test_narration_matches_ws_stream_one_for_one(db_session):
    """The fix for the pre-#1241 asymmetry: N narration calls -> N Message
    rows, matching the N WS broadcasts exactly (not one concatenated row)."""
    journal, events, guild_pk = await _make_journal(db_session, "g-journal-shape")

    await journal.text("first line")
    await journal.text("second line")

    assert len(events.broadcast_msg_calls) == 2
    messages = await _fetch_messages(guild_pk)
    assert [m.content for m in messages] == ["first line", "second line"]


async def test_turn_persistence_writes_no_broadcast(db_session):
    journal, events, guild_pk = await _make_journal(db_session, "g-journal-turns")

    sys_id = await journal.system("system prompt")
    human_id = await journal.human("hello")
    asst_id = await journal.assistant_turn([{"type": "text", "text": "hi"}], api_log_id=None)
    await journal.tool_response_turn(
        [{"type": "tool_result", "tool_use_id": "tu-1", "content": "ok"}], parent_id=asst_id
    )

    assert not events.broadcast_msg_calls
    assert not events.broadcast_calls

    turns = await _fetch_turns(guild_pk)
    assert [t.role for t in turns] == ["system", "user", "assistant", "user"]
    assert turns[0].id == sys_id
    assert turns[1].id == human_id
    assert turns[3].is_tool_response == 1
    assert turns[3].parent_id == asst_id
