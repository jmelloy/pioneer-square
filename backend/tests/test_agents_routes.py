"""Tests for the interactive agent run route (backend/routes/agents.py)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from helpers import _sync_session, insert_agent, insert_guild, insert_worker, make_auth_token
from models import Task
from sqlalchemy import select
from sqlmodel import col  # noqa: E402


def _auth(db_url: str) -> dict:
    return {"Authorization": f"Bearer {make_auth_token(db_url)}"}


def test_start_agent_run_stamps_conversation_id(client):
    """Interactive task creation resolves/creates a conversation for the
    caller instead of leaving Task.conversation_id null (#1300)."""
    test_client, db_url = client
    insert_guild(db_url, "guildagent1")
    insert_worker(db_url, "guildagent1", "w-agent1")
    insert_agent(db_url, "guildagent1", "a-agent1", worker_id="w-agent1", state="idle")

    resp = test_client.post(
        "/guilds/guildagent1/agents/a-agent1/run",
        json={"tool": "pi", "prompt": "inspect this repo"},
        headers=_auth(db_url),
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["taskId"]

    with _sync_session(db_url) as session:
        conversation_id = session.scalar(
            select(col(Task.conversation_id)).where(col(Task.id) == task_id)
        )
    assert conversation_id is not None


def test_start_agent_run_reuses_same_conversation_for_same_user(client):
    """Two interactive runs by the same user attach to the same conversation."""
    test_client, db_url = client
    insert_guild(db_url, "guildagent2")
    insert_worker(db_url, "guildagent2", "w-agent2a")
    insert_worker(db_url, "guildagent2", "w-agent2b")
    insert_agent(db_url, "guildagent2", "a-agent2a", worker_id="w-agent2a", state="idle")
    insert_agent(db_url, "guildagent2", "a-agent2b", worker_id="w-agent2b", state="idle")
    headers = _auth(db_url)

    first = test_client.post(
        "/guilds/guildagent2/agents/a-agent2a/run",
        json={"tool": "pi", "prompt": "first run"},
        headers=headers,
    )
    second = test_client.post(
        "/guilds/guildagent2/agents/a-agent2b/run",
        json={"tool": "pi", "prompt": "second run"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    with _sync_session(db_url) as session:
        conv1 = session.scalar(
            select(col(Task.conversation_id)).where(col(Task.id) == first.json()["taskId"])
        )
        conv2 = session.scalar(
            select(col(Task.conversation_id)).where(col(Task.id) == second.json()["taskId"])
        )
    assert conv1 is not None
    assert conv1 == conv2
