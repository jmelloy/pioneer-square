"""Foreman runs with the *full* conversation history (issue #1294).

Before this, a conversation-scoped run still went through the pre-#1271
funnel: matched `conversation_id OR (guild_id, user_id)`, kept only the last
`_HUMAN_TURN_WINDOW` human turns, and then pruned to `MAX_HISTORY_MESSAGES`
before every API call. These tests pin the replacement:

  - conversation-scoped loads see *only* that conversation, and *all* of it;
  - the one thing that removes turns is the explicit token budget;
  - the conversation's own tasks/GitHub events ride along in the prompt.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from _test_config import TEST_DATABASE_URL
from foreman.constants import _HUMAN_TURN_WINDOW, MAX_HISTORY_MESSAGES
from foreman.history import ConversationHistory
from foreman.message_utils import estimate_tokens, fit_token_budget
from foreman.run import ForemanRun, RunConfig
from foreman.runner import _load_conversation_context, _load_history, _save_turn
from helpers import _sync_session, create_db, insert_conversation, insert_guild, insert_task
from models import GithubEvent, Guild
from sqlmodel import col, select
from test_foreman_run import FakeHistory, FakeLLM, RecordingJournal, _text_block


@pytest.fixture()
def db_session(monkeypatch):
    from helpers import truncate_all

    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)

    yield TEST_DATABASE_URL


# --- history scoping ------------------------------------------------------


async def test_other_conversations_by_the_same_user_are_excluded(db_session):
    """Acceptance criterion: turns from another conversation of the same user
    must not leak in. #1296 lets one (guild, user) pair own many
    conversations, so the old `OR (guild_id, user_id)` fallback would have
    merged them all into one context."""
    insert_guild(db_session, "g-full-1")
    conv_a = insert_conversation(db_session, "g-full-1", "u-1")
    conv_b = insert_conversation(db_session, "g-full-1", "u-1")

    await _save_turn("g-full-1", "u-1", "user", "about conv A", conversation_id=conv_a)
    await _save_turn("g-full-1", "u-1", "assistant", "reply A", conversation_id=conv_a)
    await _save_turn("g-full-1", "u-1", "user", "about conv B", conversation_id=conv_b)
    # A legacy turn from before the conversation_id backfill: same guild+user,
    # no conversation. It belongs to neither conversation and must stay out.
    await _save_turn("g-full-1", "u-1", "user", "unstamped legacy turn")

    messages = await _load_history("g-full-1", "u-1", conv_a)

    assert [m["content"] for m in messages] == ["about conv A", "reply A"]


async def test_full_history_beyond_the_human_turn_window(db_session):
    """Acceptance criterion: more than `_HUMAN_TURN_WINDOW` human turns are
    included for a conversation."""
    insert_guild(db_session, "g-full-2")
    conv = insert_conversation(db_session, "g-full-2", "u-1")
    exchanges = _HUMAN_TURN_WINDOW * 5
    for i in range(exchanges):
        await _save_turn("g-full-2", "u-1", "user", f"human {i}", conversation_id=conv)
        await _save_turn("g-full-2", "u-1", "assistant", f"reply {i}", conversation_id=conv)

    messages = await _load_history("g-full-2", "u-1", conv)

    assert len(messages) == exchanges * 2
    assert messages[0]["content"] == "human 0"
    assert messages[-1]["content"] == f"reply {exchanges - 1}"


async def test_no_conversation_id_keeps_the_legacy_window(db_session):
    """Callers with no conversation resolved still get the old
    `_HUMAN_TURN_WINDOW` slice — #1294 widened the conversation path only."""
    insert_guild(db_session, "g-full-3")
    for i in range(_HUMAN_TURN_WINDOW * 3):
        await _save_turn("g-full-3", "u-1", "user", f"human {i}")
        await _save_turn("g-full-3", "u-1", "assistant", f"reply {i}")

    messages = await _load_history("g-full-3", "u-1")

    humans = [m for m in messages if m["role"] == "user"]
    assert len(humans) == _HUMAN_TURN_WINDOW


async def test_system_turns_stay_out_of_the_full_history(db_session):
    """Full history is still an *API-shaped* history: system turns are audit
    rows, not messages, however many of them the conversation accumulated."""
    insert_guild(db_session, "g-full-4")
    conv = insert_conversation(db_session, "g-full-4", "u-1")
    for i in range(5):
        await _save_turn("g-full-4", "u-1", "system", f"system {i}", conversation_id=conv)
        await _save_turn("g-full-4", "u-1", "user", f"human {i}", conversation_id=conv)

    messages = await _load_history("g-full-4", "u-1", conv)

    assert [m["role"] for m in messages] == ["user"] * 5


async def test_debug_view_shows_the_same_full_history(db_session):
    """The debug pane and the LLM must agree on what a conversation-scoped
    run would send — `load_for_debug` is no longer clamped to
    MAX_HISTORY_MESSAGES either."""
    insert_guild(db_session, "g-full-5")
    conv = insert_conversation(db_session, "g-full-5", "u-1")
    for i in range(MAX_HISTORY_MESSAGES):
        await _save_turn("g-full-5", "u-1", "user", f"human {i}", conversation_id=conv)
        await _save_turn("g-full-5", "u-1", "assistant", f"reply {i}", conversation_id=conv)

    llm_messages = await ConversationHistory().load_for_llm("g-full-5", "u-1", conv)
    debug = await ConversationHistory().load_for_debug("g-full-5", "u-1", conv)

    assert len(llm_messages) == MAX_HISTORY_MESSAGES * 2
    assert [m["content"] for m in llm_messages] == [m["content"] for m in debug["messages"]]
    assert debug["total"] == MAX_HISTORY_MESSAGES * 2


# --- explicit token-budget truncation -------------------------------------


def _msgs(count: int, filler: str = "x") -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"{filler} {i}"}
        for i in range(count)
    ]


def test_fit_token_budget_keeps_everything_that_fits():
    messages = _msgs(50)
    assert fit_token_budget(messages, budget=100_000) == messages


def test_fit_token_budget_drops_oldest_first_and_starts_with_user():
    messages = _msgs(50, filler="y" * 200)
    budget = estimate_tokens(messages) // 4

    kept = fit_token_budget(messages, budget=budget)

    assert 0 < len(kept) < len(messages)
    assert kept[-1] == messages[-1]  # newest survives
    assert kept[0]["role"] == "user"  # API requires a user turn first
    assert estimate_tokens(kept) <= budget
    # A contiguous tail, not a sampled subset.
    assert kept == messages[len(messages) - len(kept) :]


def test_fit_token_budget_keeps_the_newest_message_even_when_it_alone_is_over():
    messages = [{"role": "user", "content": "z" * 10_000}]
    assert fit_token_budget(messages, budget=1) == messages


def test_fit_token_budget_on_empty_history():
    assert fit_token_budget([], budget=10) == []


async def test_conversation_run_sends_more_than_the_legacy_message_window():
    """The other half of #1294: loading everything is pointless if the round
    loop still prunes to MAX_HISTORY_MESSAGES before each call. A
    conversation-scoped run sends the whole array."""
    history = _msgs(MAX_HISTORY_MESSAGES * 3)
    llm = FakeLLM([[_text_block("ok")]])
    run = ForemanRun(
        RunConfig(
            guild_id="g1",
            user_id="u-1",
            task_id=None,
            trigger=None,
            max_rounds=3,
            conversation_id=42,
        ),
        llm=llm,
        tools=None,
        journal=RecordingJournal(),
        history=FakeHistory(history),
    )

    await run.execute("hello", system_blocks=[], state_preamble="<state/>", audit_system="sys")

    assert len(llm.calls[0]["messages"]) == len(history)


async def test_run_without_conversation_id_still_prunes_to_the_legacy_window():
    history = _msgs(MAX_HISTORY_MESSAGES * 3)
    llm = FakeLLM([[_text_block("ok")]])
    run = ForemanRun(
        RunConfig(guild_id="g1", user_id="u-1", task_id=None, trigger=None, max_rounds=3),
        llm=llm,
        tools=None,
        journal=RecordingJournal(),
        history=FakeHistory(history),
    )

    await run.execute("hello", system_blocks=[], state_preamble="<state/>", audit_system="sys")

    assert len(llm.calls[0]["messages"]) <= MAX_HISTORY_MESSAGES


async def test_conversation_run_truncates_explicitly_when_over_budget(monkeypatch):
    """Over-budget histories lose their oldest turns — and only then."""
    monkeypatch.setattr("foreman.message_utils.FOREMAN_CONTEXT_TOKEN_BUDGET", 500)
    history = _msgs(100, filler="w" * 200)
    llm = FakeLLM([[_text_block("ok")]])
    run = ForemanRun(
        RunConfig(
            guild_id="g1",
            user_id="u-1",
            task_id=None,
            trigger=None,
            max_rounds=3,
            conversation_id=7,
        ),
        llm=llm,
        tools=None,
        journal=RecordingJournal(),
        history=FakeHistory(history),
    )

    await run.execute("hello", system_blocks=[], state_preamble="<state/>", audit_system="sys")

    sent = llm.calls[0]["messages"]
    assert 0 < len(sent) < len(history)
    # (the newest turn is block-wrapped by the cache breakpoint stamper)
    assert history[-1]["content"] in json.dumps(sent[-1])


# --- conversation-linked context ------------------------------------------


async def test_conversation_context_block_carries_tasks_and_github_events(db_session):
    insert_guild(db_session, "g-full-6")
    conv = insert_conversation(db_session, "g-full-6", "u-1")
    other = insert_conversation(db_session, "g-full-6", "u-1")
    insert_task(
        db_session,
        "g-full-6",
        "t-mine",
        state="done",
        pr_url="https://github.com/o/r/pull/7",
        conversation_id=conv,
    )
    insert_task(db_session, "g-full-6", "t-theirs", conversation_id=other)
    with _sync_session(db_session) as session:
        guild_pk = session.scalar(select(col(Guild.id)).where(col(Guild.slug) == "g-full-6"))
        session.add(
            GithubEvent(
                guild_id=guild_pk,
                conversation_id=conv,
                delivery_id="d-1",
                event_type="pull_request",
                action="closed",
                repo="o/r",
                pr_number=7,
                pr_url="https://github.com/o/r/pull/7",
                payload_json="{}",
                created_at=datetime.now(UTC),
            )
        )
        session.commit()

    from database import get_db

    db = await get_db()
    try:
        block = json.loads(await _load_conversation_context(db, conv))
        empty = await _load_conversation_context(db, None)
    finally:
        await db.close()

    assert [t["id"] for t in block["tasks"]] == ["t-mine"]
    assert block["github_events"][0]["pr_number"] == 7
    assert empty == ""
