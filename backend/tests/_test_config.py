"""Centralised test-database URL — single source of truth for all test modules.

Set TEST_DATABASE_URL (or DATABASE_URL as a fallback) before running the
backend test suite, e.g.:

    TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/pioneer_test pytest tests/

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

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not TEST_DATABASE_URL:
    raise RuntimeError(
        "Set TEST_DATABASE_URL (or DATABASE_URL) to run backend tests. "
        "Example: postgresql+asyncpg://user:pass@localhost/pioneer_test"
    )
