"""Tests for scripts/backfill_github_cache.py conversation_id resolution (#1300).

Loads the script directly (not installed as a package) since it lives
outside backend/ and inserts its own sys.path entry for backend imports.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _test_config import TEST_DATABASE_URL  # noqa: E402
from auth_deps import get_guild_pk  # noqa: E402
from helpers import (  # noqa: E402
    create_db,
    insert_conversation,
    insert_guild,
    insert_task,
    truncate_all,
)
from models import GithubIssue, GithubPullRequest  # noqa: E402
from sqlmodel import col, select  # noqa: E402

_SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "backfill_github_cache.py"
)
_spec = importlib.util.spec_from_file_location("backfill_github_cache", _SCRIPT_PATH)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


@pytest.fixture()
def db_session(monkeypatch):
    """Provide the PostgreSQL test database, isolated per test.

    Patches ``AsyncSessionLocal`` on the loaded script module directly —
    the script does ``from database import AsyncSessionLocal``, a name
    binding that a patch on ``database.AsyncSessionLocal`` alone wouldn't
    reach.
    """
    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(backfill, "AsyncSessionLocal", session_factory)

    yield TEST_DATABASE_URL


async def _guild_pk(guild_id: str) -> int:
    async with backfill.AsyncSessionLocal() as db:
        pk = await get_guild_pk(db, guild_id)
    assert pk is not None
    return pk


class TestIssueConversationIdResolution:
    async def test_finds_conversation_id_from_matching_task(self, db_session):
        insert_guild(db_session, "g-bf1")
        conversation_id = insert_conversation(db_session, "g-bf1", "user-1")
        insert_task(
            db_session,
            "g-bf1",
            "t-bf1",
            issue_repo="owner/repo",
            issue_number=42,
            conversation_id=conversation_id,
        )

        async with backfill.AsyncSessionLocal() as db:
            resolved = await backfill._issue_conversation_id(db, "owner/repo", 42)
        assert resolved == conversation_id

    async def test_returns_none_when_no_task_matches(self, db_session):
        async with backfill.AsyncSessionLocal() as db:
            resolved = await backfill._issue_conversation_id(db, "owner/repo", 999)
        assert resolved is None

    async def test_ignores_tasks_with_no_conversation_id(self, db_session):
        insert_guild(db_session, "g-bf2")
        insert_task(
            db_session,
            "g-bf2",
            "t-bf2",
            issue_repo="owner/repo",
            issue_number=43,
        )

        async with backfill.AsyncSessionLocal() as db:
            resolved = await backfill._issue_conversation_id(db, "owner/repo", 43)
        assert resolved is None


class TestPrConversationIdResolution:
    async def test_finds_conversation_id_from_matching_task(self, db_session):
        insert_guild(db_session, "g-bf3")
        conversation_id = insert_conversation(db_session, "g-bf3", "user-1")
        insert_task(
            db_session,
            "g-bf3",
            "t-bf3",
            pr_repo="owner/repo",
            pr_number=7,
            conversation_id=conversation_id,
        )

        async with backfill.AsyncSessionLocal() as db:
            resolved = await backfill._pr_conversation_id(db, "owner/repo", 7)
        assert resolved == conversation_id

    async def test_returns_none_when_no_task_matches(self, db_session):
        async with backfill.AsyncSessionLocal() as db:
            resolved = await backfill._pr_conversation_id(db, "owner/repo", 999)
        assert resolved is None


class TestBackfillRepoEndToEnd:
    async def test_upserts_issue_and_pr_with_resolved_conversation_ids(
        self, db_session, monkeypatch
    ):
        """backfill_repo passes each resolved conversation_id through to
        upsert_issue/upsert_pr (#1300)."""
        insert_guild(db_session, "g-bf4")
        issue_conv_id = insert_conversation(db_session, "g-bf4", "user-issue")
        pr_conv_id = insert_conversation(db_session, "g-bf4", "user-pr")
        insert_task(
            db_session,
            "g-bf4",
            "t-bf4-issue",
            issue_repo="owner/repo",
            issue_number=5,
            conversation_id=issue_conv_id,
        )
        insert_task(
            db_session,
            "g-bf4",
            "t-bf4-pr",
            pr_repo="owner/repo",
            pr_number=6,
            conversation_id=pr_conv_id,
        )

        async def fake_paginate(client, url, params):
            if url.endswith("/issues"):
                return [{"number": 5, "title": "Issue five", "state": "open"}]
            if url.endswith("/pulls"):
                return [{"number": 6, "title": "PR six", "state": "open"}]
            return []

        monkeypatch.setattr(backfill, "_paginate", fake_paginate)

        await backfill.backfill_repo(client=None, repo="owner/repo")

        async with backfill.AsyncSessionLocal() as db:
            issue = (
                await db.exec(
                    select(GithubIssue).where(
                        col(GithubIssue.repo) == "owner/repo", col(GithubIssue.number) == 5
                    )
                )
            ).one()
            pr = (
                await db.exec(
                    select(GithubPullRequest).where(
                        col(GithubPullRequest.repo) == "owner/repo",
                        col(GithubPullRequest.number) == 6,
                    )
                )
            ).one()
        assert issue.conversation_id == issue_conv_id
        assert pr.conversation_id == pr_conv_id
