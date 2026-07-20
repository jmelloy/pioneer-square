"""Unit tests for inbound Discord message routing (#885).

Exercises ``discord.router.resolve_session`` against a real DB — the single
``discord_thread_bindings`` table plus ``discord_channel_guilds`` — for every
subject type it dispatches on, including the regression case: a message
posted in a task's live-stream thread (``"task_stream"`` binding) used to have
nowhere to route to and was silently dropped.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from discord import router  # noqa: E402
from helpers import _sync_session, insert_guild, insert_task  # noqa: E402
from models import DiscordChannelGuild, DiscordThreadBinding  # noqa: E402


def _insert_binding(db_url: str, subject_type: str, subject_key: str, thread_id: str) -> None:
    with _sync_session(db_url) as session:
        session.add(
            DiscordThreadBinding(
                subject_type=subject_type,
                subject_key=subject_key,
                thread_id=thread_id,
                created_at=datetime.now(UTC),
            )
        )
        session.commit()


def _insert_channel_guild(db_url: str, channel_id: str, guild_id: str) -> None:
    with _sync_session(db_url) as session:
        session.add(
            DiscordChannelGuild(
                discord_guild_id="discord-guild-1",
                discord_channel_id=channel_id,
                ps_guild_id=guild_id,
                created_at=datetime.now(UTC),
            )
        )
        session.commit()


@pytest.mark.asyncio
async def test_resolve_session_routes_task_stream_thread(client):
    """Regression for #885: a task_stream binding resolves to that task — no longer dropped."""
    _test_client, db_url = client
    insert_guild(db_url, "g-router1")
    insert_task(db_url, "g-router1", "t-router1", state="working")
    _insert_binding(db_url, "task_stream", "t-router1", "thread-stream-1")

    session = await router.resolve_session("thread-stream-1")

    assert session == ("g-router1", "t-router1")


@pytest.mark.asyncio
async def test_resolve_session_routes_issue_thread_to_most_recent_task(client):
    """An issue binding routes to the most recently created task linked to that issue/PR."""
    _test_client, db_url = client
    insert_guild(db_url, "g-router2")
    insert_task(
        db_url,
        "g-router2",
        "t-router2-old",
        state="merged",
        issue_repo="org/repo",
        issue_number=99,
    )
    insert_task(
        db_url,
        "g-router2",
        "t-router2-new",
        state="working",
        issue_repo="org/repo",
        issue_number=99,
    )
    _insert_binding(db_url, "issue", "org/repo#99", "thread-issue-1")

    session = await router.resolve_session("thread-issue-1")

    assert session == ("g-router2", "t-router2-new")


@pytest.mark.asyncio
async def test_resolve_session_foreman_daily_thread_has_no_task(client):
    """A legacy foreman_daily binding routes to the guild with no task scoping."""
    _test_client, db_url = client
    _insert_binding(db_url, "foreman_daily", "g-router3:2026-07-04", "thread-daily-1")

    session = await router.resolve_session("thread-daily-1")

    assert session == ("g-router3", None)


@pytest.mark.asyncio
async def test_resolve_session_falls_back_to_wired_channel(client):
    """A plain wired channel (no thread binding at all) routes as general chat."""
    _test_client, db_url = client
    _insert_channel_guild(db_url, "channel-plain-1", "g-router4")

    session = await router.resolve_session("channel-plain-1")

    assert session == ("g-router4", None)


@pytest.mark.asyncio
async def test_resolve_session_unresolvable_channel_returns_none(client):
    """A channel with no binding and no /join-channel wiring has nowhere to route."""
    _test_client, _db_url = client

    session = await router.resolve_session("channel-unknown-1")

    assert session is None


@pytest.mark.asyncio
async def test_route_inbound_message_tags_task_thread_reply(client):
    """A reply in a thread bound to a task is tagged [discord-thread-reply] task_id=... (#906).

    Uses state="pending" so the message falls through to Foreman chat instead of
    being auto-routed (#959) — that behavior is covered separately below.
    """
    _test_client, db_url = client
    insert_guild(db_url, "g-router5")
    insert_task(db_url, "g-router5", "t-router5", state="pending")
    _insert_binding(db_url, "task_stream", "t-router5", "thread-stream-5")

    with (
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "please rename the variable",
                "channel_id": "thread-stream-5",
                "author": {"id": "d-user-1", "username": "alice"},
            }
        )

    assert mock_trigger.await_count == 1
    _args, kwargs = mock_trigger.await_args
    assert kwargs["task_id"] == "t-router5"
    human_message = _args[2]
    assert human_message.startswith("[discord-thread-reply] task_id=t-router5\n")
    assert "please rename the variable" in human_message


@pytest.mark.asyncio
async def test_route_inbound_message_no_tag_for_general_chat(client):
    """General chat with no task binding keeps the plain [Discord] prefix, untagged."""
    _test_client, db_url = client
    _insert_channel_guild(db_url, "channel-plain-2", "g-router6")

    with (
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "what's the status?",
                "channel_id": "channel-plain-2",
                "author": {"id": "d-user-2", "username": "bob"},
            }
        )

    assert mock_trigger.await_count == 1
    _args, kwargs = mock_trigger.await_args
    assert kwargs["task_id"] is None
    human_message = _args[2]
    assert not human_message.startswith("[discord-thread-reply]")
    assert human_message.startswith("[Discord]")


@pytest.mark.asyncio
async def test_route_inbound_message_auto_redirects_working_task(client):
    """A reply in a thread bound to a 'working' task calls redirect_task directly (#959),
    bypassing Foreman chat entirely."""
    _test_client, db_url = client
    insert_guild(db_url, "g-router7")
    insert_task(db_url, "g-router7", "t-router7", state="working")
    _insert_binding(db_url, "task_stream", "t-router7", "thread-stream-7")

    with (
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch(
            "foreman.tools.exec_tools", new=AsyncMock(return_value=[{"content": "ok"}])
        ) as mock_exec,
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "actually use a different approach",
                "channel_id": "thread-stream-7",
                "author": {"id": "d-user-7", "username": "carol"},
            }
        )

    assert mock_exec.await_count == 1
    _args, _kwargs = mock_exec.await_args
    guild_slug, tool_uses = _args[0], _args[1]
    assert guild_slug == "g-router7"
    assert len(tool_uses) == 1
    assert tool_uses[0].name == "redirect_task"
    assert tool_uses[0].input == {
        "task_id": "t-router7",
        "instructions": "actually use a different approach",
    }
    assert mock_trigger.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["awaiting-review", "done", "parked"])
async def test_route_inbound_message_auto_followups_paused_task(client, state):
    """A reply in a thread bound to an awaiting-review/done/parked task calls
    send_followup directly (#959), bypassing Foreman chat entirely."""
    _test_client, db_url = client
    guild_id = f"g-router8-{state}"
    task_id = f"t-router8-{state}"
    thread_id = f"thread-stream-8-{state}"
    insert_guild(db_url, guild_id)
    insert_task(db_url, guild_id, task_id, state=state)
    _insert_binding(db_url, "task_stream", task_id, thread_id)

    with (
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch(
            "foreman.tools.exec_tools", new=AsyncMock(return_value=[{"content": "ok"}])
        ) as mock_exec,
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "keep going with one more pass",
                "channel_id": thread_id,
                "author": {"id": "d-user-8", "username": "dave"},
            }
        )

    assert mock_exec.await_count == 1
    _args, _kwargs = mock_exec.await_args
    tool_uses = _args[1]
    assert tool_uses[0].name == "send_followup"
    assert tool_uses[0].input == {
        "task_id": task_id,
        "instructions": "keep going with one more pass",
    }
    assert mock_trigger.await_count == 0


@pytest.mark.asyncio
async def test_route_inbound_message_falls_back_for_non_routable_state(client):
    """A reply in a thread bound to a task in a state that isn't auto-routed (e.g.
    'pending') falls back to normal Foreman chat, still tagged [discord-thread-reply]."""
    _test_client, db_url = client
    insert_guild(db_url, "g-router9")
    insert_task(db_url, "g-router9", "t-router9", state="pending")
    _insert_binding(db_url, "task_stream", "t-router9", "thread-stream-9")

    with (
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch("foreman.tools.exec_tools", new=AsyncMock()) as mock_exec,
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "any update?",
                "channel_id": "thread-stream-9",
                "author": {"id": "d-user-9", "username": "erin"},
            }
        )

    assert mock_exec.await_count == 0
    assert mock_trigger.await_count == 1
