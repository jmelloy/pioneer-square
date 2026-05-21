"""Centralised test-database URL — single source of truth for all test modules.

Defaults to the postgres-test container from docker-compose.yml
(localhost:5433, DB=pioneer_test).  Start it with:

    docker compose up -d postgres-test

Override the URL when needed:

    TEST_DATABASE_URL=postgresql+asyncpg://user:pass@host/db pytest tests/

Or place the variable in backend/.env and it will be loaded automatically.
"""

import os
from pathlib import Path

# Load backend/.env if present so `pytest tests/` works without exporting env vars.
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(_env_file, override=False)
    except ImportError:
        pass

# Default points at the postgres-test container from docker-compose.yml
# (postgres:17 on localhost:5433, pioneer_test DB).  Override with env vars.
_DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://pioneer:pioneer_password@localhost:5433/pioneer_test"
)

TEST_DATABASE_URL = (
    os.environ.get("TEST_DATABASE_URL")
    or os.environ.get("DATABASE_URL")
    or _DEFAULT_TEST_DATABASE_URL
)
