import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncSession:
    return AsyncSessionLocal()


async def get_db_dep() -> AsyncGenerator[AsyncSession, None]:
    """Async generator for FastAPI Depends — yields a session and ensures close on exit."""
    db = AsyncSessionLocal()
    try:
        yield db
    finally:
        await db.close()
