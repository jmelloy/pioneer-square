"""Tests for conversation_id stamping/queries in db/github_cache.py and
db/github_events.py (#1277: link GitHub events, issues, and PRs to
Conversation).

#1280 already added github_events.conversation_id; this covers the second
half — github_issues/github_pull_requests conversation_id, and the
conversation-scoped GithubEvent query helper.
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
from _test_config import TEST_DATABASE_URL
from auth_deps import get_guild_pk
from db import github_cache
from db.github_events import list_events_by_conversation
from foreman.conversation_service import get_or_create_conversation
from helpers import create_db, insert_guild, truncate_all
from models import GithubEvent
from sqlalchemy.dialects.postgresql import insert as pg_insert


@pytest.fixture()
def db_session(monkeypatch):
    """Provide the PostgreSQL test database, isolated per test."""
    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)

    yield TEST_DATABASE_URL


async def _guild_pk(guild_id: str) -> int:
    async with database_module.AsyncSessionLocal() as db:
        pk = await get_guild_pk(db, guild_id)
    assert pk is not None
    return pk


def _issue_payload(number: int, title: str = "Do the thing") -> dict:
    return {"number": number, "title": title, "state": "open"}


def _pr_payload(number: int, title: str = "Fix the thing") -> dict:
    return {"number": number, "title": title, "state": "open"}


class TestUpsertIssueConversationId:
    async def test_stamps_conversation_id_on_insert(self, db_session):
        insert_guild(db_session, "g-gc1")
        guild_pk = await _guild_pk("g-gc1")

        async with database_module.AsyncSessionLocal() as db:
            conv = await get_or_create_conversation(db, guild_pk, "user-1")
            await db.commit()
            conv_id = conv.id

        async with database_module.AsyncSessionLocal() as db:
            issue = await github_cache.upsert_issue(
                db, "owner/repo", _issue_payload(1), conversation_id=conv_id
            )

        assert issue.conversation_id == conv_id

    async def test_conflict_update_does_not_overwrite_existing_conversation_id(self, db_session):
        """A later upsert with conversation_id=None (e.g. from a fetch_issue_state
        poll with no task context) must not clear a conversation_id set earlier."""
        insert_guild(db_session, "g-gc2")
        guild_pk = await _guild_pk("g-gc2")

        async with database_module.AsyncSessionLocal() as db:
            conv = await get_or_create_conversation(db, guild_pk, "user-1")
            await db.commit()
            conv_id = conv.id

        async with database_module.AsyncSessionLocal() as db:
            await github_cache.upsert_issue(
                db, "owner/repo", _issue_payload(2), conversation_id=conv_id
            )

        async with database_module.AsyncSessionLocal() as db:
            issue = await github_cache.upsert_issue(
                db, "owner/repo", _issue_payload(2, title="Updated title"), conversation_id=None
            )

        assert issue.title == "Updated title"
        assert issue.conversation_id == conv_id

    async def test_conflict_update_does_not_downgrade_to_a_different_conversation(self, db_session):
        """First writer wins: once set, a differing conversation_id from a later
        upsert must not overwrite the original."""
        insert_guild(db_session, "g-gc3")
        guild_pk = await _guild_pk("g-gc3")

        async with database_module.AsyncSessionLocal() as db:
            conv1 = await get_or_create_conversation(db, guild_pk, "user-1")
            conv2 = await get_or_create_conversation(db, guild_pk, "user-2")
            await db.commit()
            conv1_id, conv2_id = conv1.id, conv2.id

        async with database_module.AsyncSessionLocal() as db:
            await github_cache.upsert_issue(
                db, "owner/repo", _issue_payload(3), conversation_id=conv1_id
            )

        async with database_module.AsyncSessionLocal() as db:
            issue = await github_cache.upsert_issue(
                db, "owner/repo", _issue_payload(3), conversation_id=conv2_id
            )

        assert issue.conversation_id == conv1_id


class TestUpsertPrConversationId:
    async def test_stamps_conversation_id_on_insert(self, db_session):
        insert_guild(db_session, "g-gc4")
        guild_pk = await _guild_pk("g-gc4")

        async with database_module.AsyncSessionLocal() as db:
            conv = await get_or_create_conversation(db, guild_pk, "user-1")
            await db.commit()
            conv_id = conv.id

        async with database_module.AsyncSessionLocal() as db:
            pr = await github_cache.upsert_pr(
                db, "owner/repo", _pr_payload(10), conversation_id=conv_id
            )

        assert pr.conversation_id == conv_id

    async def test_conflict_update_does_not_overwrite_existing_conversation_id(self, db_session):
        insert_guild(db_session, "g-gc5")
        guild_pk = await _guild_pk("g-gc5")

        async with database_module.AsyncSessionLocal() as db:
            conv = await get_or_create_conversation(db, guild_pk, "user-1")
            await db.commit()
            conv_id = conv.id

        async with database_module.AsyncSessionLocal() as db:
            await github_cache.upsert_pr(db, "owner/repo", _pr_payload(11), conversation_id=conv_id)

        async with database_module.AsyncSessionLocal() as db:
            pr = await github_cache.upsert_pr(
                db, "owner/repo", _pr_payload(11, title="Updated title"), conversation_id=None
            )

        assert pr.title == "Updated title"
        assert pr.conversation_id == conv_id


class TestListEventsByConversation:
    async def test_filters_to_matching_conversation(self, db_session):
        insert_guild(db_session, "g-gc6")
        guild_pk = await _guild_pk("g-gc6")

        async with database_module.AsyncSessionLocal() as db:
            conv1 = await get_or_create_conversation(db, guild_pk, "user-1")
            conv2 = await get_or_create_conversation(db, guild_pk, "user-2")
            await db.commit()
            conv1_id, conv2_id = conv1.id, conv2.id

            from datetime import UTC, datetime

            await db.exec(
                pg_insert(GithubEvent).values(
                    guild_id=guild_pk,
                    conversation_id=conv1_id,
                    delivery_id="d-1",
                    event_type="pull_request",
                    action="opened",
                    repo="owner/repo",
                    payload_json="{}",
                    created_at=datetime.now(UTC),
                )
            )
            await db.exec(
                pg_insert(GithubEvent).values(
                    guild_id=guild_pk,
                    conversation_id=conv2_id,
                    delivery_id="d-2",
                    event_type="pull_request",
                    action="opened",
                    repo="owner/repo",
                    payload_json="{}",
                    created_at=datetime.now(UTC),
                )
            )
            await db.commit()

        async with database_module.AsyncSessionLocal() as db:
            events = await list_events_by_conversation(db, conv1_id)

        assert [e.delivery_id for e in events] == ["d-1"]
