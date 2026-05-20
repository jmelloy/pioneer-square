"""Shared pytest fixtures for backend tests.

Each test gets a fresh PostgreSQL database (tables truncated) so tests are
fully isolated. The DB schema is created once per session via Alembic
migrations. AsyncSessionLocal is patched in both the `database` module (used
by get_db) and in `main` (used directly inside reset_connection_state's
startup query).
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module  # noqa: E402
import main as main_module  # noqa: E402

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://pioneer:pioneer_password@localhost/pioneer_test",
)


@pytest.fixture(scope="session", autouse=True)
def _setup_schema():
    """Run migrations once for the whole test session."""
    from helpers import create_db

    create_db(TEST_DATABASE_URL)


@pytest.fixture()
def client(monkeypatch, _setup_schema):
    """Fresh test DB (tables truncated) + TestClient for each test."""
    from helpers import truncate_all

    truncate_all(TEST_DATABASE_URL)
    db_url = TEST_DATABASE_URL

    new_engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    new_session = async_sessionmaker(new_engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", new_session)
    monkeypatch.setattr(main_module, "AsyncSessionLocal", new_session)

    # Stub out reset_connection_state: tables already exist (we just truncated
    # them above) and there are no workers to reset in a fresh DB, so the stub
    # is a no-op.
    async def _stubbed_reset_connection_state() -> None:
        pass

    monkeypatch.setattr(main_module, "reset_connection_state", _stubbed_reset_connection_state)
    monkeypatch.setenv("DATABASE_URL", db_url)

    with TestClient(main_module.app) as c:
        yield c, db_url

    # NullPool means connections are not pooled; disposal is a no-op but kept
    # for clarity.
    asyncio.run(new_engine.dispose())
