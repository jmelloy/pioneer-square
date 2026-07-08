import asyncio, sys, os, traceback
sys.path.insert(0, '.')
sys.path.insert(0, 'tests')
try:
    from _test_config import TEST_DATABASE_URL
    from helpers import create_db, truncate_all, insert_guild, insert_worker, insert_task
    import database as database_module
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from sqlmodel.ext.asyncio.session import AsyncSession

    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)
    os.environ['DATABASE_URL'] = TEST_DATABASE_URL
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    database_module.AsyncSessionLocal = session_factory

    insert_guild(TEST_DATABASE_URL, 'g-cascade')
    insert_worker(TEST_DATABASE_URL, 'g-cascade', 'w-cascade', state='idle')
    insert_task(TEST_DATABASE_URL, 'g-cascade', 't-root', worker_id='w-cascade', state='working', phase='issue', issue_repo='o/r', issue_number=99)
    insert_task(TEST_DATABASE_URL, 'g-cascade', 't-child-done', worker_id='w-cascade', state='done', phase='execute', parent_task_id='t-root')

    from foreman.tools import find_descendant_tasks
    from database import get_db

    async def main():
        db = await get_db()
        try:
            d = await find_descendant_tasks(db, 't-root')
            print('descendants:', [ (t.id, t.state, t.parent_task_id) for t in d])
        finally:
            await db.close()

    asyncio.run(main())
except Exception:
    traceback.print_exc()
