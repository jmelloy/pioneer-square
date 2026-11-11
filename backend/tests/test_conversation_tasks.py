"""Conversation <-> Task is many-to-many via conversation_tasks (#1431)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from alembic import command
from alembic.config import Config
from foreman import tools
from helpers import _sync_session, insert_conversation, insert_guild, insert_task, insert_worker
from models import (
    Conversation,
    ConversationTask,
    Task,
    link_conversation_task,
    task_conversation_id,
    task_in_conversation,
)
from sqlalchemy import delete, insert, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select


def _links(db_url, task_id):
    with _sync_session(db_url) as session:
        return set(
            session.scalars(
                select(ConversationTask.conversation_id).where(
                    col(ConversationTask.task_id) == task_id
                )
            )
        )


def _tasks_in(db_url, conversation_id):
    with _sync_session(db_url) as session:
        return set(session.scalars(select(Task.id).where(task_in_conversation(conversation_id))))


def test_task_belongs_to_many_conversations(client):
    _, db_url = client
    insert_guild(db_url, "g-n2n")
    conv_a = insert_conversation(db_url, "g-n2n")
    conv_b = insert_conversation(db_url, "g-n2n")
    insert_task(db_url, "g-n2n", "t-shared", conversation_id=conv_a)
    insert_task(db_url, "g-n2n", "t-only-b", conversation_id=conv_b)
    with _sync_session(db_url) as session:
        session.execute(link_conversation_task(conv_b, "t-shared"))
        session.commit()

    assert _links(db_url, "t-shared") == {conv_a, conv_b}
    assert _tasks_in(db_url, conv_a) == {"t-shared"}
    assert _tasks_in(db_url, conv_b) == {"t-shared", "t-only-b"}
    # Primary conversation is the earliest link.
    with _sync_session(db_url) as session:
        primary = session.scalar(select(task_conversation_id()).where(col(Task.id) == "t-shared"))
        unlinked = session.scalar(select(task_conversation_id()).where(col(Task.id) == "t-nope"))
    assert primary == conv_a
    assert unlinked is None


def test_link_is_unique_and_idempotent(client):
    _, db_url = client
    insert_guild(db_url, "g-n2n-uniq")
    conv = insert_conversation(db_url, "g-n2n-uniq")
    insert_task(db_url, "g-n2n-uniq", "t-uniq", conversation_id=conv)
    with _sync_session(db_url) as session:
        session.execute(link_conversation_task(conv, "t-uniq"))  # no-op
        session.commit()
        with pytest.raises(IntegrityError):
            session.execute(insert(ConversationTask).values(conversation_id=conv, task_id="t-uniq"))
    assert _links(db_url, "t-uniq") == {conv}


def test_deletes_cascade_to_links_only(client):
    _, db_url = client
    insert_guild(db_url, "g-n2n-cascade")
    conv_a = insert_conversation(db_url, "g-n2n-cascade")
    conv_b = insert_conversation(db_url, "g-n2n-cascade")
    insert_task(db_url, "g-n2n-cascade", "t-casc", conversation_id=conv_a)
    insert_task(db_url, "g-n2n-cascade", "t-gone", conversation_id=conv_b)
    with _sync_session(db_url) as session:
        session.execute(link_conversation_task(conv_b, "t-casc"))
        session.execute(delete(Conversation).where(col(Conversation.id) == conv_a))
        session.execute(delete(Task).where(col(Task.id) == "t-gone"))
        session.commit()
        assert session.get(Task, "t-casc") is not None
    assert _links(db_url, "t-casc") == {conv_b}
    assert _tasks_in(db_url, conv_b) == {"t-casc"}


async def test_assign_existing_task_links_calling_conversation(client):
    """Re-assigning a task from another conversation adds a link; the old one stays."""
    _, db_url = client
    insert_guild(db_url, "g-n2n-assign")
    insert_worker(db_url, "g-n2n-assign", "w-n2n")
    conv_a = insert_conversation(db_url, "g-n2n-assign", "user")
    conv_b = insert_conversation(db_url, "g-n2n-assign", "user")
    insert_task(db_url, "g-n2n-assign", "t-reuse", conversation_id=conv_a)

    tool = SimpleNamespace(
        id="reuse",
        name="assign_task",
        input={"task_id": "t-reuse", "worker_id": "w-n2n", "description": "continue"},
    )
    with patch("foreman.tools.broadcast", AsyncMock()):
        result = await tools.exec_tools(
            "g-n2n-assign", [tool], user_id="user", conversation_id=conv_b
        )
    assert not result[0].get("is_error"), result
    assert _links(db_url, "t-reuse") == {conv_a, conv_b}


def test_migration_round_trip_preserves_links(client):
    _, db_url = client
    insert_guild(db_url, "g-n2n-mig")
    conv_a = insert_conversation(db_url, "g-n2n-mig")
    conv_b = insert_conversation(db_url, "g-n2n-mig")
    insert_task(db_url, "g-n2n-mig", "t-mig", conversation_id=conv_a)
    insert_task(db_url, "g-n2n-mig", "t-mig-none")
    with _sync_session(db_url) as session:
        session.execute(link_conversation_task(conv_b, "t-mig"))
        session.commit()

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    try:
        command.downgrade(cfg, "20261111_000000")
        with _sync_session(db_url) as session:
            rows = dict(
                session.execute(
                    text("SELECT id, conversation_id FROM tasks WHERE id LIKE 't-mig%'")
                ).all()
            )
        # Downgrade keeps each task's earliest (primary) conversation.
        assert rows == {"t-mig": conv_a, "t-mig-none": None}
    finally:
        command.upgrade(cfg, "head")
    assert _links(db_url, "t-mig") == {conv_a}
    assert _links(db_url, "t-mig-none") == set()
