"""Tests for the thread messages route (issue #1275, epic #1271).

``GET /api/guilds/{guild_id}/threads/{thread_id}/messages`` used to scope
strictly to ``Message.thread_id`` — one Discord thread's own slice. Per #1271
phase 4, a ``Conversation`` may span more than one ``Thread`` over time (a
new Discord thread gets created after the old one archives/closes), so the
route now resolves the thread's owning conversation and scopes to
``Message.conversation_id`` instead, returning that conversation's full
history regardless of which thread each message was originally posted in.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(__file__))
from helpers import _sync_session, insert_guild, make_auth_token
from models import Conversation, Guild, Message, Thread
from sqlmodel import col, select


def _insert_conversation(db_url: str, guild_id: str, user_id: str) -> int:
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(select(col(Guild.id)).where(col(Guild.slug) == guild_id))
        conv = Conversation(guild_id=guild_pk, user_id=user_id, created_at=now, updated_at=now)
        session.add(conv)
        session.commit()
        session.refresh(conv)
        return conv.id


def _insert_thread(db_url: str, thread_id: str, conversation_id: int, status: str) -> None:
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.add(
            Thread(
                id=thread_id,
                conversation_id=conversation_id,
                status=status,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


def _insert_message(
    db_url: str,
    guild_id: str,
    content: str,
    *,
    thread_id: str | None,
    conversation_id: int | None,
) -> None:
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(select(col(Guild.id)).where(col(Guild.slug) == guild_id))
        session.add(
            Message(
                guild_id=guild_pk,
                from_agent="user",
                to_agent="foreman",
                content=content,
                message_type="chat",
                created_at=now,
                thread_id=thread_id,
                conversation_id=conversation_id,
            )
        )
        session.commit()


def test_list_thread_messages_spans_conversation_threads(client):
    """Messages posted in an earlier thread of the same conversation still
    show up when fetching a later thread's message history."""
    test_client, db_url = client
    insert_guild(db_url, "g1")
    token = make_auth_token(db_url)

    conv_id = _insert_conversation(db_url, "g1", "gh-user-test")
    _insert_thread(db_url, "th-old", conv_id, "archived")
    _insert_thread(db_url, "th-new", conv_id, "active")

    _insert_message(
        db_url, "g1", "from the old thread", thread_id="th-old", conversation_id=conv_id
    )
    _insert_message(
        db_url, "g1", "from the new thread", thread_id="th-new", conversation_id=conv_id
    )

    other_conv_id = _insert_conversation(db_url, "g1", "gh-user-other")
    _insert_thread(db_url, "th-other", other_conv_id, "active")
    _insert_message(
        db_url, "g1", "unrelated conversation", thread_id="th-other", conversation_id=other_conv_id
    )

    resp = test_client.get(
        "/api/guilds/g1/threads/th-new/messages",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    contents = [m["content"] for m in resp.json()]
    assert contents == ["from the old thread", "from the new thread"]


def test_list_thread_messages_thread_not_found(client):
    test_client, db_url = client
    insert_guild(db_url, "g1")
    token = make_auth_token(db_url)

    resp = test_client.get(
        "/api/guilds/g1/threads/th-missing/messages",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
