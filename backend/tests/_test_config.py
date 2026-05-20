"""Centralised test-database URL — single source of truth for all test modules.

Set TEST_DATABASE_URL (or DATABASE_URL as a fallback) before running the
backend test suite, e.g.:

    TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/pioneer_test pytest tests/
"""

import os

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not TEST_DATABASE_URL:
    raise RuntimeError(
        "Set TEST_DATABASE_URL (or DATABASE_URL) to run backend tests. "
        "Example: postgresql+asyncpg://user:pass@localhost/pioneer_test"
    )
