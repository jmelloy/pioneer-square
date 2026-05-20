import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

import sqlalchemy as sa
from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Ensure the backend package is importable from this file's location.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# DATABASE_URL env var is required; no hardcoded fallback.
_db_url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
if not _db_url:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set. Set it before running alembic commands."
    )
config.set_main_option("sqlalchemy.url", _db_url)

# Revision IDs in this project exceed Alembic's default VARCHAR(32) for version_num.
# We pre-create (or widen) the alembic_version table before running any migration.
_ENSURE_VERSION_TABLE_SQL = sa.text("""
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'alembic_version'
  ) THEN
    CREATE TABLE alembic_version (
      version_num VARCHAR(64) NOT NULL,
      CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
    );
  ELSIF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'alembic_version' AND column_name = 'version_num'
    AND character_maximum_length IS NOT NULL AND character_maximum_length < 64
  ) THEN
    ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64);
  END IF;
END $$;
""")


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    # Ensure alembic_version exists with VARCHAR(64) before migrations run.
    # Alembic defaults to VARCHAR(32), which is too short for our revision IDs.
    async with connectable.begin() as conn:
        await conn.execute(_ENSURE_VERSION_TABLE_SQL)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
