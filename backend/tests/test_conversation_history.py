"""Tests for foreman.history.ConversationHistory — the one windowing implementation.

Before #1241, foreman.runner._load_history and get_foreman_history each
hand-rolled the same human-turn-window backward scan; these tests assert
load_for_llm and load_for_debug agree on the windowed slice for the same
fixture data, turning "copy-paste means they can't diverge" into an actual
assertion.
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
from helpers import create_db, insert_guild


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


async def test_load_for_llm_excludes_system_and_trims_to_user(db_session):
    insert_guild(db_session, "g-hist-llm")
    await _save_turn("g-hist-llm", "u-1", "system", "You are the Foreman AI.")
    await _save_turn("g-hist-llm", "u-1", "user", "Hello foreman")
    await _save_turn("g-hist-llm", "u-1", "assistant", "Hi there")

    messages = await ConversationHistory().load_for_llm("g-hist-llm", "u-1")

    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Hello foreman"


async def test_load_for_debug_surfaces_system_and_metadata(db_session):
    insert_guild(db_session, "g-hist-debug")
    await _save_turn("g-hist-debug", "u-1", "system", "System prompt content")
    await _save_turn("g-hist-debug", "u-1", "user", "Human message")

    debug = await ConversationHistory().load_for_debug("g-hist-debug", "u-1")

    assert debug["system"] == "System prompt content"
    assert debug["total"] == 1
    assert len(debug["messages"]) == 1
    assert debug["messages"][0]["role"] == "user"
    assert "id" in debug["messages"][0]
    assert "created_at" in debug["messages"][0]


async def test_load_for_llm_and_load_for_debug_agree_on_windowed_slice(db_session):
    """The one thing #1241 exists to guarantee: both callers of
    ConversationHistory see the same windowed turns for the same data —
    not two independently-hand-rolled cutoffs that can silently diverge."""
    insert_guild(db_session, "g-hist-agree")
    for i in range(7):
        await _save_turn("g-hist-agree", "u-1", "system", f"System prompt {i}")
        await _save_turn("g-hist-agree", "u-1", "user", f"Human message {i}")
        await _save_turn("g-hist-agree", "u-1", "assistant", f"Reply {i}")

    llm_messages = await ConversationHistory().load_for_llm("g-hist-agree", "u-1")
    debug = await ConversationHistory().load_for_debug("g-hist-agree", "u-1")

    assert [m["role"] for m in llm_messages] == [m["role"] for m in debug["messages"]]
    assert [m["content"] for m in llm_messages] == [m["content"] for m in debug["messages"]]


async def test_empty_history_returns_empty(db_session):
    insert_guild(db_session, "g-hist-empty")
    assert await ConversationHistory().load_for_llm("g-hist-empty", "u-1") == []
    debug = await ConversationHistory().load_for_debug("g-hist-empty", "u-1")
    assert debug == {"system": None, "messages": [], "total": 0}


async def test_isolated_by_user(db_session):
    insert_guild(db_session, "g-hist-iso")
    await _save_turn("g-hist-iso", "u-alice", "user", "Alice message")
    await _save_turn("g-hist-iso", "u-bob", "user", "Bob message")

    alice = await ConversationHistory().load_for_llm("g-hist-iso", "u-alice")
    bob = await ConversationHistory().load_for_llm("g-hist-iso", "u-bob")

    assert alice[0]["content"] == "Alice message"
    assert bob[0]["content"] == "Bob message"
