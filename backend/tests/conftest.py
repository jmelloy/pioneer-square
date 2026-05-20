"""Shared pytest fixtures for backend tests.

Each test gets a fresh PostgreSQL database (tables truncated) so tests are
fully isolated. The DB schema is created once per session via Alembic
migrations. AsyncSessionLocal is patched in both the `database` module (used
by get_db) and in `main` (used directly inside reset_connection_state's
startup query).
"""

from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _test_config import TEST_DATABASE_URL  # noqa: E402

# database.py raises if DATABASE_URL is unset; populate it before the import.
# Tests override AsyncSessionLocal after import, so this value is only used as
# a placeholder during module load — never for actual connections in test code.
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)

import database as database_module  # noqa: E402
import main as main_module  # noqa: E402
from helpers import create_db as _create_db  # noqa: E402
from routes.webhooks import shutdown_debouncer  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _setup_schema():
    """Run migrations once and wipe any leftover data from previous runs.

    Per-test isolation comes from unique IDs, not truncation — this single
    truncate at session start is the only one we need.
    """
    from helpers import create_db, truncate_all

    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)


@pytest.fixture()
def client(monkeypatch, _setup_schema):
    """TestClient pointing at the test DB for each test.

    No truncation between tests — tests use unique IDs for isolation.
    """
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


@pytest.fixture(autouse=True)
async def cleanup_debouncer():
    """Ensure the module-level debounce queue is empty before and after each test."""
    await shutdown_debouncer()
    yield
    await shutdown_debouncer()
