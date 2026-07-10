"""Unit tests for _guild_github_token's user_id-aware lookup and owner fallback."""

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
from foreman.tools import _guild_github_token
from helpers import create_db, insert_guild, insert_member, make_auth_token, truncate_all


@pytest.fixture()
def db_session(monkeypatch):
    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)
    db_url = TEST_DATABASE_URL
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)
    yield db_url


@pytest.mark.asyncio
async def test_no_user_id_uses_owner_token(db_session):
    db_url = db_session
    make_auth_token(db_url, user_id="owner-1", username="owner-login")
    insert_guild(db_url, "g1", owner_user_id="owner-1")

    creds = await _guild_github_token("g1")

    assert creds == ("gh_tok_fake", "owner-login")


@pytest.mark.asyncio
async def test_user_id_with_own_token_prefers_it_over_owner(db_session):
    db_url = db_session
    make_auth_token(db_url, user_id="owner-1", username="owner-login")
    insert_guild(db_url, "g1", owner_user_id="owner-1")
    make_auth_token(db_url, user_id="member-1", username="member-login")
    insert_member(db_url, "g1", "member-1")

    creds = await _guild_github_token("g1", user_id="member-1")

    assert creds == ("gh_tok_fake", "member-login")


@pytest.mark.asyncio
async def test_user_id_without_token_falls_back_to_owner(db_session):
    db_url = db_session
    make_auth_token(db_url, user_id="owner-1", username="owner-login")
    insert_guild(db_url, "g1", owner_user_id="owner-1")
    insert_member(db_url, "g1", "member-no-token")

    creds = await _guild_github_token("g1", user_id="member-no-token")

    assert creds == ("gh_tok_fake", "owner-login")


@pytest.mark.asyncio
async def test_unknown_guild_returns_none(db_session):
    creds = await _guild_github_token("no-such-guild", user_id="whoever")

    assert creds is None
