"""Migration 20260904_020000: backfill Conversation UI/lifecycle fields from
Thread (issue #1274).

Covers the PR #1286 review finding: the backfill must prefer a conversation's
*active* thread over merely its most-recently-updated one, matching
``thread_service.get_or_create_active_thread``'s own definition of "the"
thread for a conversation. Runs the real migration up/down against the test
database (rather than reimplementing its SQL) so the assertions cover the
migration itself.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from alembic import command
from alembic.config import Config as AlembicConfig
from helpers import _sync_session, insert_conversation, insert_guild
from models import Conversation, Thread

_REVISION = "20260904_020000_add_ui_lifecycle_fields_to_conversations"
_PARENT_REVISION = "20260904_000001_add_foreman_turns_conversation_id_index"


def _alembic_config(db_url: str) -> AlembicConfig:
    alembic_ini = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    cfg = AlembicConfig(alembic_ini)
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _rerun_migration(db_url: str) -> None:
    """Undo and redo just this migration, re-running its backfill against
    whatever thread rows currently exist."""
    cfg = _alembic_config(db_url)
    command.downgrade(cfg, _PARENT_REVISION)
    command.upgrade(cfg, "head")


def test_backfill_prefers_active_thread_over_more_recently_touched_one(client):
    _, db_url = client
    insert_guild(db_url, "g-mig-1274a")
    conversation_id = insert_conversation(db_url, "g-mig-1274a")

    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        # The conversation's real active thread -- but it hasn't been
        # touched in two days.
        session.add(
            Thread(
                id="thread-1274-active",
                conversation_id=conversation_id,
                discord_thread_id="discord-active",
                name="active session",
                status="active",
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(days=2),
            )
        )
        # A closed thread that was touched an hour ago -- more recent than
        # the active thread, but not the one the app considers current.
        session.add(
            Thread(
                id="thread-1274-closed",
                conversation_id=conversation_id,
                discord_thread_id="discord-closed",
                name="closed session",
                status="closed",
                created_at=now - timedelta(days=3),
                updated_at=now - timedelta(hours=1),
            )
        )
        session.commit()

    _rerun_migration(db_url)

    with _sync_session(db_url) as session:
        conversation = session.get(Conversation, conversation_id)

    assert conversation.status == "active"
    assert conversation.name == "active session"
    assert conversation.discord_thread_id == "discord-active"


def test_backfill_falls_back_to_most_recently_updated_when_none_active(client):
    _, db_url = client
    insert_guild(db_url, "g-mig-1274b")
    conversation_id = insert_conversation(db_url, "g-mig-1274b")

    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.add(
            Thread(
                id="thread-1274-archived-old",
                conversation_id=conversation_id,
                discord_thread_id="discord-archived-old",
                name="older archived session",
                status="archived",
                created_at=now - timedelta(days=3),
                updated_at=now - timedelta(days=3),
            )
        )
        session.add(
            Thread(
                id="thread-1274-closed-new",
                conversation_id=conversation_id,
                discord_thread_id="discord-closed-new",
                name="newer closed session",
                status="closed",
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(hours=1),
            )
        )
        session.commit()

    _rerun_migration(db_url)

    with _sync_session(db_url) as session:
        conversation = session.get(Conversation, conversation_id)

    assert conversation.status == "closed"
    assert conversation.name == "newer closed session"
    assert conversation.discord_thread_id == "discord-closed-new"
