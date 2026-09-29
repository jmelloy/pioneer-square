"""Regression tests for ws_handlers.handle_chat's Conversation plumbing.

Investigation into "the first AI response doesn't appear in the conversation"
found two related gaps in the main web-chat path (there was no test coverage
of ``handle_chat`` at all before this file):

  1. The outbound ``chat`` WS broadcast for the human's own message never
     carried ``conversationId`` (unlike foreman.journal.TurnJournal's replies
     and routes/conversations.py's REST endpoint, added by #1298) — so a
     Conversations-panel viewer's live feed
     (``ConversationDetailPanel.vue``'s ``liveMessages``, filtered by
     ``conversationId``) could never show a human's own just-sent message
     until the next full reload.
  2. ``trigger_foreman`` was called with no ``conversation_id``, leaving
     ``run_foreman_ai`` to re-derive "the" conversation for
     ``(guild_id, user_id)`` from scratch via
     ``conversation_service.resolve_conversation_id``'s generic
     "most recently updated" fallback, instead of being pinned to the exact
     Conversation ``ensure_conversation_thread`` had just resolved/created a
     few lines above. Harmless while a Conversation stays 1:1 with
     (guild_id, user_id), but fragile now that #1296 lets one user have many.
"""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import foreman.runner as runner_module  # noqa: E402
import util.tasks as tasks_module  # noqa: E402
from foreman import triggers  # noqa: E402
from helpers import insert_guild, make_auth_token  # noqa: E402


def _fake_llm_result():
    text_block = SimpleNamespace(
        type="text",
        text="Hello! I am the foreman's reply.",
        model_dump=lambda: {"type": "text", "text": "Hello! I am the foreman's reply."},
    )
    resp = SimpleNamespace(
        content=[text_block],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    return runner_module._LLMCallResult(
        response=resp,
        response_dict={"fake": True},
        request_id="req-fake-1",
        provider="anthropic",
        model="fake-model",
    )


def _wait_for_background_tasks(timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while tasks_module.pending_count() > 0 and time.monotonic() < deadline:
        time.sleep(0.05)


def test_chat_broadcast_carries_conversation_id(client):
    """The human's own message broadcast must include conversationId so a
    live-open Conversations panel can show it immediately (#1298 parity)."""
    test_client, db_url = client
    guild_id = "g-ws-chat-1"
    insert_guild(db_url, guild_id)
    token = make_auth_token(db_url)

    with patch.object(triggers, "trigger_foreman", new=AsyncMock()):
        with test_client.websocket_connect(f"/ws/{guild_id}?token={token}") as ws:
            ws.send_json(
                {"type": "chat", "from": "user", "to": "foreman", "content": "hello there"}
            )
            for _ in range(10):
                frame = ws.receive_json()
                if frame.get("type") == "chat" and frame.get("from") == "user":
                    break
            else:
                raise AssertionError("never saw the human chat echo")

    assert frame["conversationId"] is not None


def test_first_ai_response_appears_when_fetching_the_conversation(client, monkeypatch):
    """End-to-end: WS chat -> trigger_foreman -> _run_foreman_ai -> TurnJournal
    -> GET .../conversations/{id}/messages must all agree on one Conversation.

    Only the LLM network call is stubbed; conversation/thread resolution and
    DB persistence run for real against the Postgres test DB.
    """
    test_client, db_url = client
    guild_id = "g-ws-chat-2"
    insert_guild(db_url, guild_id)
    token = make_auth_token(db_url)  # user_id defaults to "gh-user-test"

    # foreman.runner did `from database import AsyncSessionLocal`, binding its
    # own module-level name at import time -- the `client` fixture only
    # re-patches `database.AsyncSessionLocal`/`main.AsyncSessionLocal` (see
    # conftest.py), so foreman.runner._load_foreman_config would otherwise
    # keep using the stale placeholder engine bound at process start, shared
    # (and racy) across every test in the suite. This is the first test to
    # drive a real (non-mocked) _run_foreman_ai far enough to hit that call.
    import database as database_module

    monkeypatch.setattr(runner_module, "AsyncSessionLocal", database_module.AsyncSessionLocal)

    with (
        patch.object(
            runner_module,
            "resolve_foreman_client",
            new=AsyncMock(return_value=(object(), "anthropic", "fake-model")),
        ),
        patch.object(runner_module, "_call_llm", new=AsyncMock(return_value=_fake_llm_result())),
    ):
        with test_client.websocket_connect(f"/ws/{guild_id}?token={token}") as ws:
            ws.send_json(
                {
                    "type": "chat",
                    "from": "user",
                    "to": "foreman",
                    "content": "hello foreman, this is my first message",
                }
            )
            for _ in range(10):
                frame = ws.receive_json()
                if frame.get("type") == "chat" and frame.get("from") == "user":
                    break
            else:
                raise AssertionError("never saw the human chat echo")

            _wait_for_background_tasks()

    resp = test_client.get(
        f"/api/guilds/{guild_id}/conversations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    conversations = resp.json()
    assert len(conversations) == 1
    conv_id = conversations[0]["id"]

    resp = test_client.get(
        f"/api/guilds/{guild_id}/conversations/{conv_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    contents = [m["content"] for m in resp.json()]

    assert "hello foreman, this is my first message" in contents
    assert "Hello! I am the foreman's reply." in contents


def test_trigger_foreman_receives_the_conversation_id_handle_chat_already_resolved(client):
    """Direct check on the fix: handle_chat must pin trigger_foreman to the
    exact conversation_id it resolved via ensure_conversation_thread, not
    leave it for run_foreman_ai to re-derive from (guild_id, user_id)."""
    test_client, db_url = client
    guild_id = "g-ws-chat-3"
    insert_guild(db_url, guild_id)
    token = make_auth_token(db_url)

    captured = {}

    async def fake_trigger_foreman(guild_id_arg, event, message, **kwargs):
        captured["kwargs"] = kwargs

    with patch.object(triggers, "trigger_foreman", new=fake_trigger_foreman):
        with test_client.websocket_connect(f"/ws/{guild_id}?token={token}") as ws:
            ws.send_json(
                {"type": "chat", "from": "user", "to": "foreman", "content": "hello there"}
            )
            for _ in range(10):
                frame = ws.receive_json()
                if frame.get("type") == "chat" and frame.get("from") == "user":
                    break
            else:
                raise AssertionError("never saw the human chat echo")

    assert captured["kwargs"].get("conversation_id") is not None
    assert captured["kwargs"]["conversation_id"] == frame["conversationId"]


def test_each_top_level_web_message_starts_its_own_conversation(client):
    """#1323: a top-level WS chat message spawns a new Conversation (like a
    top-level Discord message, #1296) instead of reusing the user's latest."""
    test_client, db_url = client
    guild_id = "g-ws-chat-4"
    insert_guild(db_url, guild_id)
    token = make_auth_token(db_url)

    async def fake_trigger_foreman(*args, **kwargs):
        return None

    conversation_ids = []
    with patch.object(triggers, "trigger_foreman", new=fake_trigger_foreman):
        with test_client.websocket_connect(f"/ws/{guild_id}?token={token}") as ws:
            for text in ("first topic", "second topic"):
                ws.send_json({"type": "chat", "from": "user", "to": "foreman", "content": text})
                for _ in range(10):
                    frame = ws.receive_json()
                    if frame.get("type") == "chat" and frame.get("from") == "user":
                        conversation_ids.append(frame["conversationId"])
                        break
                else:
                    raise AssertionError("never saw the human chat echo")

    assert conversation_ids[0] != conversation_ids[1]
