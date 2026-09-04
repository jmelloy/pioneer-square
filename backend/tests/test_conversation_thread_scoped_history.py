"""Tests for thread-scoped ForemanTurn history (#1271).

Extends test_conversation_history.py with tests that exercise the new
thread_id FK on ForemanTurn and the thread-scoped load methods in
ConversationHistory.
"""

from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from _test_config import TEST_DATABASE_URL  # noqa: E402
from foreman.history import ConversationHistory
from foreman.runner import _save_turn
from helpers import create_db, insert_guild, insert_thread, truncate_all


@pytest.fixture()
def db_session(monkeypatch):
    """Provide the PostgreSQL test database, isolated per test."""
    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)
    db_url = TEST_DATABASE_URL

    monkeypatch.setenv("DATABASE_URL", db_url)

    engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)

    yield db_url


async def test_save_turn_accepts_thread_id(db_session):
    """_save_turn can stamp thread_id on new turns."""
    insert_guild(db_session, "g-thread-save")
    insert_thread(db_session, "g-thread-save", "th-save1", user_id="u-1")

    turn_id = await _save_turn("g-thread-save", "u-1", "user", "hello", thread_id="th-save1")
    assert turn_id > 0

    # Verify the thread_id was persisted
    async with database_module.AsyncSessionLocal() as db:
        from models import ForemanTurn
        from sqlmodel import col, select

        result = await db.exec(select(ForemanTurn).where(col(ForemanTurn.id) == turn_id))
        turn = result.first()
        assert turn is not None
        assert turn.thread_id == "th-save1"


async def test_load_for_llm_by_thread_returns_turns(db_session):
    """Thread-scoped history loading returns only turns for that thread."""
    insert_guild(db_session, "g-thread-llm")
    insert_thread(db_session, "g-thread-llm", "th-llm1", user_id="u-1")
    insert_thread(db_session, "g-thread-llm", "th-llm2", user_id="u-1")

    # Save turns to thread 1
    await _save_turn("g-thread-llm", "u-1", "user", "hello thread", thread_id="th-llm1")
    await _save_turn("g-thread-llm", "u-1", "assistant", "hi there", thread_id="th-llm1")

    # Save a turn to thread 2 (same guild/user)
    await _save_turn("g-thread-llm", "u-1", "user", "hello other", thread_id="th-llm2")

    # Thread-scoped load should only return the first two turns
    messages = await ConversationHistory().load_for_llm_by_thread("th-llm1")
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "hello thread"


async def test_load_for_debug_by_thread_returns_turns(db_session):
    """Thread-scoped debug loading returns only turns for that thread."""
    insert_guild(db_session, "g-thread-debug")
    insert_thread(db_session, "g-thread-debug", "th-debug1", user_id="u-1")

    await _save_turn("g-thread-debug", "u-1", "system", "sys prompt")
    await _save_turn("g-thread-debug", "u-1", "user", "debug msg", thread_id="th-debug1")

    debug = await ConversationHistory().load_for_debug_by_thread("th-debug1")
    # System turns excluded from messages
    assert len(debug["messages"]) == 1
    assert debug["messages"][0]["content"] == "debug msg"


async def test_empty_thread_returns_empty_history(db_session):
    """An unknown thread_id returns an empty history."""
    messages = await ConversationHistory().load_for_llm_by_thread("th-nonexistent")
    assert messages == []

    debug = await ConversationHistory().load_for_debug_by_thread("th-nonexistent")
    assert debug == {"system": None, "messages": [], "total": 0}


async def test_threads_isolate_history(db_session):
    """Different threads for the same guild/user have isolated histories."""
    insert_guild(db_session, "g-thread-isolate")
    insert_thread(db_session, "g-thread-isolate", "th-iso-a", user_id="u-1")
    insert_thread(db_session, "g-thread-isolate", "th-iso-b", user_id="u-1")

    await _save_turn("g-thread-isolate", "u-1", "user", "msg for A", thread_id="th-iso-a")
    await _save_turn("g-thread-isolate", "u-1", "user", "msg for B", thread_id="th-iso-b")

    msgs_a = await ConversationHistory().load_for_llm_by_thread("th-iso-a")
    msgs_b = await ConversationHistory().load_for_llm_by_thread("th-iso-b")

    assert len(msgs_a) == 1
    assert len(msgs_b) == 1
    assert msgs_a[0]["content"] == "msg for A"
    assert msgs_b[0]["content"] == "msg for B"


async def test_legacy_load_for_llm_still_works(db_session):
    """Legacy (guild_id, user_id) load still works for backwards compatibility."""
    insert_guild(db_session, "g-legacy")
    insert_thread(db_session, "g-legacy", "th-legacy", user_id="u-1")

    await _save_turn("g-legacy", "u-1", "user", "legacy msg", thread_id="th-legacy")

    # Legacy path still loads by (guild, user)
    messages = await ConversationHistory().load_for_llm("g-legacy", "u-1")
    assert len(messages) == 1
    assert messages[0]["content"] == "legacy msg"
