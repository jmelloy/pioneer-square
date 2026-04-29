"""Shared pytest fixtures for backend tests.

Each test gets a fresh temporary SQLite database so tests are fully isolated.
The DB is created synchronously via Alembic before the TestClient starts, which
avoids the anyio/asyncio.to_thread interaction that can deadlock in pytest.
AsyncSessionLocal is patched in both the `database` module (used by get_db)
and in `main` (used directly inside init_db's startup query).
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

import database as database_module  # noqa: E402
import main as main_module  # noqa: E402
from helpers import create_db as _create_db  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by a fresh temporary SQLite database."""
    db_path = str(tmp_path / "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"

    # Create DB tables synchronously before the TestClient starts.
    # This avoids the asyncio.to_thread(run_migrations) call inside init_db()
    # which can deadlock under pytest's anyio event loop.
    os.environ["DATABASE_URL"] = db_url
    _create_db(db_path)

    new_engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    new_session = async_sessionmaker(new_engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", new_session)
    monkeypatch.setattr(main_module, "AsyncSessionLocal", new_session)

    # Stub out init_db: tables already exist (we just created them above) and
    # there are no workers to reset in a fresh DB, so the stub is a no-op.
    async def _stubbed_init_db() -> None:
        pass

    monkeypatch.setattr(main_module, "init_db", _stubbed_init_db)
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("DB_PATH", db_path)

    with TestClient(main_module.app) as c:
        yield c, db_path

    # Restore DATABASE_URL after test
    os.environ.pop("DATABASE_URL", None)
