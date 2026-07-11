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
