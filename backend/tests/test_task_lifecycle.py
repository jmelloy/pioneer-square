"""Tests for backend/task_lifecycle.py — the one finalize implementation.

Five call sites used to have their own copy (foreman finalize_task tool, the
closed-issue sweep, the PR-merged and PR-closed webhooks, and the UI button) and
they had drifted apart. These tests pin the behaviour they now all share: lock
release, queued-event purge, TOCTOU safety, and the phase='issue' cascade.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module  # noqa: E402
from _test_config import TEST_DATABASE_URL  # noqa: E402
from auth_deps import get_guild_pk  # noqa: E402
from database import get_db  # noqa: E402
from helpers import (  # noqa: E402
    _sync_session,
    create_db,
    insert_guild,
    insert_task,
    insert_worker,
)
from models import Lock, Task, TaskEvent  # noqa: E402
from sqlalchemy import insert, select  # noqa: E402
from sqlmodel import col  # noqa: E402
from task_lifecycle import TERMINAL_STATES, finalize_task  # noqa: E402


@pytest.fixture()
def db_url(monkeypatch):
    """Fresh PostgreSQL test database, same shape as test_foreman.db_session."""
    from helpers import truncate_all

    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)
    yield TEST_DATABASE_URL


def _seed(db_url: str, guild: str, task_id: str, **task_kwargs) -> None:
    insert_guild(db_url, guild)
    insert_worker(db_url, guild, "w-1", state="idle")
    insert_task(db_url, guild, task_id, worker_id="w-1", state="working", **task_kwargs)


def _hold_lock(db_url: str, task_id: str) -> None:
    with _sync_session(db_url) as session:
        session.execute(
            insert(Lock).values(key=f"task:{task_id}", owner=task_id, acquired_at=datetime.now(UTC))
        )
        session.commit()


def _queue_event(db_url: str, task_id: str) -> None:
    with _sync_session(db_url) as session:
        session.execute(
            insert(TaskEvent).values(
                task_id=task_id,
                event_type="pending-followup",
                payload_json=json.dumps({"instructions": "later"}),
                created_at=datetime.now(UTC),
            )
        )
        session.commit()


def _read(db_url: str, task_id: str) -> tuple[str, datetime | None]:
    with _sync_session(db_url) as session:
        return session.execute(
            select(col(Task.state), col(Task.deleted_at)).where(col(Task.id) == task_id)
        ).one()


def _counts(db_url: str, task_id: str) -> tuple[int, int]:
    """(lock rows, queued event rows) for *task_id*."""
    with _sync_session(db_url) as session:
        locks = session.execute(
            select(col(Lock.id)).where(col(Lock.key) == f"task:{task_id}")
        ).all()
        events = session.execute(
            select(col(TaskEvent.id)).where(col(TaskEvent.task_id) == task_id)
        ).all()
    return len(locks), len(events)


async def _finalize(db_url: str, guild: str, task_id: str, outcome: str = "done"):
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild)
        return await finalize_task(
            db, guild_pk=guild_pk, guild_id=guild, task_id=task_id, outcome=outcome
        )
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Terminal-state vocabulary
# ---------------------------------------------------------------------------


def test_terminal_states_include_error():
    """Three of the old inline copies were ("done", "failed", "cancelled") — a task
    a worker reported as 'error' looked non-terminal to them."""
    assert TERMINAL_STATES == frozenset({"done", "failed", "cancelled", "error"})


def test_terminal_state_vocabulary_is_not_respelled():
    """No module may re-spell the tuple inline; import TERMINAL_STATES instead."""
    import pathlib

    backend = pathlib.Path(__file__).resolve().parent.parent
    offenders = [
        str(p.relative_to(backend))
        for p in backend.rglob("*.py")
        if "tests" not in p.parts
        and p.name != "task_lifecycle.py"  # the one definition lives here
        and '"done", "failed", "cancelled"' in p.read_text()
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# finalize_task
# ---------------------------------------------------------------------------


class TestFinalizeTask:
    async def test_missing_task_is_not_found(self, db_url):
        insert_guild(db_url, "g-tl-missing")
        res = await _finalize(db_url, "g-tl-missing", "t-nope")
        assert res.status == "not_found"
        assert not res.finalized

    async def test_other_guilds_task_is_not_found(self, db_url):
        _seed(db_url, "g-tl-owner", "t-owned")
        insert_guild(db_url, "g-tl-other")
        res = await _finalize(db_url, "g-tl-other", "t-owned")
        assert res.status == "not_found"
        assert _read(db_url, "t-owned")[0] == "working"

    async def test_releases_lock_and_purges_queued_events(self, db_url):
        """The UI finalize used to skip both of these, leaving a stuck task lock
        and a queued follow-up that would fire against a closed task."""
        _seed(db_url, "g-tl-clean", "t-clean")
        _hold_lock(db_url, "t-clean")
        _queue_event(db_url, "t-clean")
        assert _counts(db_url, "t-clean") == (1, 1)

        res = await _finalize(db_url, "g-tl-clean", "t-clean")

        assert res.finalized
        assert _counts(db_url, "t-clean") == (0, 0)
        state, deleted_at = _read(db_url, "t-clean")
        assert state == "done"
        assert deleted_at is not None

    async def test_failed_outcome_stamps_immediately(self, db_url):
        _seed(
            db_url,
            "g-tl-failed",
            "t-failed",
            issue_repo="o/r",
            issue_number=7,
            issue_state="open",
        )
        res = await _finalize(db_url, "g-tl-failed", "t-failed", outcome="failed")
        assert res.finalized
        assert _read(db_url, "t-failed") == ("failed", res.deleted_at)
        assert res.deleted_at is not None

    async def test_done_with_open_issue_stays_live(self, db_url):
        _seed(db_url, "g-tl-open", "t-open", issue_repo="o/r", issue_number=9, issue_state="open")
        res = await _finalize(db_url, "g-tl-open", "t-open")
        assert res.finalized
        assert res.deleted_at is None
        assert _read(db_url, "t-open") == ("done", None)

    @pytest.mark.parametrize("prior", sorted(TERMINAL_STATES))
    async def test_already_terminal_is_not_overwritten(self, db_url, prior):
        guild = f"g-tl-{prior}"
        insert_guild(db_url, guild)
        insert_worker(db_url, guild, "w-1", state="idle")
        insert_task(db_url, guild, "t-term", worker_id="w-1", state=prior)

        res = await _finalize(db_url, guild, "t-term", outcome="failed")

        assert res.status == "already_terminal"
        assert not res.finalized
        assert _read(db_url, "t-term")[0] == prior

    async def test_concurrent_finalize_only_one_wins(self, db_url):
        """TOCTOU: the terminal-state guard lives in the UPDATE's WHERE clause, so
        two callers racing on the same task cannot both report a transition (the
        bug routes/tasks.py had — read state, then write unconditionally)."""
        _seed(db_url, "g-tl-race", "t-race")

        db_a, db_b = await get_db(), await get_db()
        try:
            guild_pk = await get_guild_pk(db_a, "g-tl-race")
            results = await asyncio.gather(
                finalize_task(
                    db_a, guild_pk=guild_pk, guild_id="g-tl-race", task_id="t-race", outcome="done"
                ),
                finalize_task(
                    db_b,
                    guild_pk=guild_pk,
                    guild_id="g-tl-race",
                    task_id="t-race",
                    outcome="failed",
                ),
            )
        finally:
            await db_a.close()
            await db_b.close()

        assert sorted(r.status for r in results) == ["already_terminal", "finalized"]
        state, deleted_at = _read(db_url, "t-race")
        assert state in TERMINAL_STATES
        assert deleted_at is not None

    async def test_issue_root_cascades_to_terminal_descendants_only(self, db_url):
        """A phase='issue' root owns the whole issue: stamp its finished children,
        never force-close the ones still running."""
        insert_guild(db_url, "g-tl-cascade")
        insert_worker(db_url, "g-tl-cascade", "w-1", state="idle")
        insert_task(
            db_url,
            "g-tl-cascade",
            "t-root",
            worker_id="w-1",
            state="working",
            phase="issue",
            issue_repo="o/r",
            issue_number=1,
            issue_state="open",
        )
        insert_task(
            db_url,
            "g-tl-cascade",
            "t-child-done",
            worker_id="w-1",
            state="done",
            parent_task_id="t-root",
        )
        insert_task(
            db_url,
            "g-tl-cascade",
            "t-child-error",
            worker_id="w-1",
            state="error",
            parent_task_id="t-child-done",  # grandchild — the walk is recursive
        )
        insert_task(
            db_url,
            "g-tl-cascade",
            "t-child-working",
            worker_id="w-1",
            state="working",
            parent_task_id="t-root",
        )

        res = await _finalize(db_url, "g-tl-cascade", "t-root")

        assert res.finalized
        # The issue root itself is the issue proxy — stamped now even though the
        # issue is still open.
        assert res.deleted_at is not None
        assert {t.id for t in res.descendants} == {
            "t-child-done",
            "t-child-error",
            "t-child-working",
        }
        assert _read(db_url, "t-child-done")[1] is not None
        assert _read(db_url, "t-child-error")[1] is not None
        assert _read(db_url, "t-child-working") == ("working", None)

    async def test_non_issue_task_does_not_cascade(self, db_url):
        insert_guild(db_url, "g-tl-nocascade")
        insert_worker(db_url, "g-tl-nocascade", "w-1", state="idle")
        insert_task(db_url, "g-tl-nocascade", "t-parent", worker_id="w-1", state="working")
        insert_task(
            db_url,
            "g-tl-nocascade",
            "t-kid",
            worker_id="w-1",
            state="done",
            parent_task_id="t-parent",
        )

        res = await _finalize(db_url, "g-tl-nocascade", "t-parent")

        assert res.finalized
        assert res.descendants == []
        assert _read(db_url, "t-kid")[1] is None


# ---------------------------------------------------------------------------
# Call-site parity — the UI button must be as safe as the foreman tool
# ---------------------------------------------------------------------------


def test_ui_finalize_releases_lock_and_purges_events(client):
    """routes/tasks.py used to do a bare UPDATE: no lock release, no event purge."""
    from helpers import make_auth_token

    test_client, url = client
    insert_guild(url, "g-ui-fin")
    insert_worker(url, "g-ui-fin", "w-ui", state="idle")
    insert_task(url, "g-ui-fin", "t-ui", worker_id="w-ui", state="working")
    _hold_lock(url, "t-ui")
    _queue_event(url, "t-ui")

    headers = {"Authorization": f"Bearer {make_auth_token(url)}"}
    resp = test_client.post("/guilds/g-ui-fin/tasks/t-ui/finalize", headers=headers)

    assert resp.status_code == 200, resp.text
    assert _counts(url, "t-ui") == (0, 0)
    assert _read(url, "t-ui")[0] == "done"

    # Second click loses the conditional UPDATE instead of re-finalizing.
    again = test_client.post("/guilds/g-ui-fin/tasks/t-ui/finalize", headers=headers)
    assert again.status_code == 409
