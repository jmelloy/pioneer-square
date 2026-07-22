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
from models import DiscordChannelGuild, DiscordThreadBinding, User  # noqa: E402
from sqlmodel import col, select  # noqa: E402


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


def _insert_channel_guild(
    db_url: str, channel_id: str, guild_id: str, discord_guild_id: str = "discord-guild-1"
) -> None:
    with _sync_session(db_url) as session:
        session.add(
            DiscordChannelGuild(
                discord_guild_id=discord_guild_id,
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
    """A reply in a thread bound to a task is tagged [discord-thread-reply] task_id=... (#906)."""
    _test_client, db_url = client
    insert_guild(db_url, "g-router5")
    insert_task(db_url, "g-router5", "t-router5", state="working")
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


# ---------------------------------------------------------------------------
# Bot-user @mentions — respond anywhere, channel-scoped when the channel binds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_mention_in_bound_channel_scopes_to_that_guild(client):
    """A bot @mention in a bound channel routes to that channel's guild, with
    the mention token stripped and the reply pinned back to the channel."""
    _test_client, db_url = client
    _insert_channel_guild(
        db_url, "channel-mention-1", "g-router8", discord_guild_id="discord-guild-mention-2"
    )

    with (
        patch.dict(os.environ, {"DISCORD_APPLICATION_ID": "bot-id-1"}),
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "<@bot-id-1> what's the status of the release?",
                "channel_id": "channel-mention-1",
                "author": {"id": "d-user-4", "username": "dave"},
                "mentions": [{"id": "bot-id-1", "username": "pioneer-bot"}],
            }
        )

    assert mock_trigger.await_count == 1
    args, kwargs = mock_trigger.await_args
    assert args[0] == "g-router8"
    assert kwargs["task_id"] is None
    assert kwargs["reply_channel_id"] == "channel-mention-1"
    human_message = args[2]
    assert "<@bot-id-1>" not in human_message
    assert "what's the status of the release?" in human_message


@pytest.mark.asyncio
async def test_bot_mention_in_unbound_channel_falls_back_to_author_projects(client):
    """A bot @mention in a channel bound to nothing (or a DM) routes by author:
    their most recently created project, with the rest listed as context."""
    _test_client, _db_url = client

    with (
        patch.dict(os.environ, {"DISCORD_APPLICATION_ID": "bot-id-1"}),
        patch.object(
            router, "_resolve_identity", new=AsyncMock(return_value=("ps-user-1", "@frankie"))
        ),
        patch.object(
            router,
            "_resolve_user_guild_slugs",
            new=AsyncMock(return_value=["g-newest", "g-older"]),
        ),
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "<@bot-id-1> how's it going?",
                "channel_id": "dm-channel-1",
                "author": {"id": "d-user-6", "username": "frankie"},
                "mentions": [{"id": "bot-id-1"}],
            }
        )

    assert mock_trigger.await_count == 1
    args, kwargs = mock_trigger.await_args
    assert args[0] == "g-newest"
    assert kwargs["user_id"] == "ps-user-1"
    assert kwargs["task_id"] is None
    assert kwargs["reply_channel_id"] == "dm-channel-1"
    human_message = args[2]
    assert human_message.startswith("[discord-mention] project=g-newest")
    assert "also has access to: g-older" in human_message
    assert "how's it going?" in human_message


@pytest.mark.asyncio
async def test_bot_mention_prefers_projects_bound_to_the_same_discord_server(client):
    """A mention in an unbound channel of a server whose *other* channels are
    wired prefers those projects over the author's newest unrelated one."""
    _test_client, db_url = client
    insert_guild(db_url, "g-server-1")
    _insert_channel_guild(
        db_url, "channel-bound-elsewhere", "g-server-1", discord_guild_id="discord-server-1"
    )

    with (
        patch.dict(os.environ, {"DISCORD_APPLICATION_ID": "bot-id-1"}),
        patch.object(
            router, "_resolve_identity", new=AsyncMock(return_value=("ps-user-1", "@frankie"))
        ),
        # Author's newest project (g-unrelated) is *not* bound to this server.
        patch.object(
            router,
            "_resolve_user_guild_slugs",
            new=AsyncMock(return_value=["g-unrelated", "g-server-1"]),
        ),
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "<@bot-id-1> what's up?",
                "channel_id": "channel-unbound-same-server",
                "guild_id": "discord-server-1",
                "author": {"id": "d-user-9", "username": "frankie"},
                "mentions": [{"id": "bot-id-1"}],
            }
        )

    assert mock_trigger.await_count == 1
    args, _kwargs = mock_trigger.await_args
    assert args[0] == "g-server-1"


@pytest.mark.asyncio
async def test_bot_mention_ignores_server_binding_author_cannot_access(client):
    """Server bindings only narrow the author's own list — a project the author
    isn't a member of is never routed to, even when bound to this server."""
    _test_client, db_url = client
    insert_guild(db_url, "g-private")
    _insert_channel_guild(
        db_url, "channel-bound-private", "g-private", discord_guild_id="discord-server-2"
    )

    with (
        patch.dict(os.environ, {"DISCORD_APPLICATION_ID": "bot-id-1"}),
        patch.object(
            router, "_resolve_identity", new=AsyncMock(return_value=("ps-user-2", "@gale"))
        ),
        patch.object(router, "_resolve_user_guild_slugs", new=AsyncMock(return_value=["g-mine"])),
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "<@bot-id-1> hello",
                "channel_id": "channel-unbound-server-2",
                "guild_id": "discord-server-2",
                "author": {"id": "d-user-10", "username": "gale"},
                "mentions": [{"id": "bot-id-1"}],
            }
        )

    assert mock_trigger.await_count == 1
    args, _kwargs = mock_trigger.await_args
    assert args[0] == "g-mine"


@pytest.mark.asyncio
async def test_bot_mention_from_unlinked_author_in_unbound_channel_ignored(client):
    """The author-scoped fallback needs a linked Pioneer Square user — without
    one there is no project to route to, so the mention is dropped."""
    _test_client, _db_url = client

    with (
        patch.dict(os.environ, {"DISCORD_APPLICATION_ID": "bot-id-1"}),
        patch.object(
            router, "_resolve_identity", new=AsyncMock(return_value=(None, "a Discord user"))
        ),
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "<@bot-id-1> hello?",
                "channel_id": "dm-channel-2",
                "author": {"id": "d-user-7", "username": "gale"},
                "mentions": [{"id": "bot-id-1"}],
            }
        )

    assert mock_trigger.await_count == 0


@pytest.mark.asyncio
async def test_unbound_channel_without_mention_still_ignored(client):
    """The author-scoped fallback is a mention-only path — a plain message in
    an unbound channel still has nowhere to route to."""
    _test_client, _db_url = client

    with (
        patch.dict(os.environ, {"DISCORD_APPLICATION_ID": "bot-id-1"}),
        patch.object(router, "_resolve_user_guild_slugs", new=AsyncMock(return_value=["g-newest"])),
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "just chatting, not pinging anyone",
                "channel_id": "unbound-channel-1",
                "author": {"id": "d-user-8", "username": "harper"},
                "mentions": [],
            }
        )

    assert mock_trigger.await_count == 0


# ---------------------------------------------------------------------------
# Bot auto-provisioning (parent_user_id)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_inbound_message_auto_provisions_bot_author(client):
    """An unmapped bot author gets its own users row, parented to the guild owner,
    instead of resolving to no ps_user_id at all."""
    _test_client, db_url = client
    insert_guild(db_url, "g-router-bot1", owner_user_id="gh-owner-bot1")
    _insert_channel_guild(db_url, "channel-bot-1", "g-router-bot1")

    with (
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "build finished",
                "channel_id": "channel-bot-1",
                "author": {"id": "d-bot-1", "username": "ci-bot", "bot": True},
            }
        )

    assert mock_trigger.await_count == 1
    _args, kwargs = mock_trigger.await_args
    ps_user_id = kwargs["user_id"]
    assert ps_user_id == "discordbot:d-bot-1"

    with _sync_session(db_url) as session:
        bot_user = session.execute(select(User).where(col(User.id) == ps_user_id)).scalars().one()
        assert bot_user.is_bot is True
        assert bot_user.parent_user_id == "gh-owner-bot1"


@pytest.mark.asyncio
async def test_route_inbound_message_reuses_existing_bot_user(client):
    """A second message from the same bot reuses its already-provisioned row."""
    _test_client, db_url = client
    insert_guild(db_url, "g-router-bot2", owner_user_id="gh-owner-bot2")
    _insert_channel_guild(db_url, "channel-bot-2", "g-router-bot2")

    async def _send():
        with (
            patch.object(router, "_persist_inbound_message", new=AsyncMock()),
            patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
            patch("foreman.runner.reset_foreman_poll"),
        ):
            await router._route_inbound_message(
                {
                    "content": "status update",
                    "channel_id": "channel-bot-2",
                    "author": {"id": "d-bot-2", "username": "ci-bot", "bot": True},
                }
            )
            return mock_trigger.await_args[1]["user_id"]

    first_id = await _send()
    second_id = await _send()
    assert first_id == second_id == "discordbot:d-bot-2"

    with _sync_session(db_url) as session:
        count = len(session.execute(select(User).where(col(User.id) == first_id)).scalars().all())
        assert count == 1


@pytest.mark.asyncio
async def test_bot_mention_in_unwired_channel_provisions_via_server_binding(client):
    """Regression: a *bot* @-mention reaching the author-scoped path was resolved
    without ``is_bot``, so it never auto-provisioned and was dropped as "unlinked".

    The mention bypass (#969) is what lets a bot reach this path at all, from a
    channel that is wired to nothing. The parent comes from the Discord
    *server's* other bindings, since there is no channel binding to read.

    Deliberately does not patch ``_resolve_identity`` — every other mention test
    stubs it out, which is exactly how the missing flag went unnoticed.
    """
    _test_client, db_url = client
    insert_guild(db_url, "g-router-bot3", owner_user_id="gh-owner-bot3")
    _insert_channel_guild(
        db_url, "channel-wired-bot3", "g-router-bot3", discord_guild_id="discord-server-bot3"
    )

    with (
        patch.dict(os.environ, {"DISCORD_APPLICATION_ID": "bot-id-1"}),
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "<@bot-id-1> deploy finished",
                # Wired to nothing — only the *server* has a binding, elsewhere.
                "channel_id": "channel-unwired-bot3",
                "guild_id": "discord-server-bot3",
                "author": {"id": "d-bot-3", "username": "mecha", "bot": True},
                "mentions": [{"id": "bot-id-1"}],
            }
        )

    assert mock_trigger.await_count == 1
    args, kwargs = mock_trigger.await_args
    assert args[0] == "g-router-bot3"
    assert kwargs["user_id"] == "discordbot:d-bot-3"

    with _sync_session(db_url) as session:
        bot_user = (
            session.execute(select(User).where(col(User.id) == "discordbot:d-bot-3"))
            .scalars()
            .one()
        )
        assert bot_user.is_bot is True
        assert bot_user.parent_user_id == "gh-owner-bot3"


@pytest.mark.asyncio
async def test_bot_mention_with_no_server_binding_falls_back_to_default_guild(client):
    """With nothing bound to the mention's server (or in a DM), the bot parents
    into the instance default (``GUILD_ID``) rather than being dropped."""
    _test_client, db_url = client
    insert_guild(db_url, "g-defalt", owner_user_id="gh-owner-bot4")

    with (
        patch.dict(os.environ, {"DISCORD_APPLICATION_ID": "bot-id-1", "GUILD_ID": "g-defalt"}),
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "<@bot-id-1> anyone home?",
                "channel_id": "dm-channel-bot4",
                "author": {"id": "d-bot-4", "username": "mecha", "bot": True},
                "mentions": [{"id": "bot-id-1"}],
            }
        )

    assert mock_trigger.await_count == 1
    args, _kwargs = mock_trigger.await_args
    assert args[0] == "g-defalt"

    with _sync_session(db_url) as session:
        bot_user = (
            session.execute(select(User).where(col(User.id) == "discordbot:d-bot-4"))
            .scalars()
            .one()
        )
        assert bot_user.parent_user_id == "gh-owner-bot4"


@pytest.mark.asyncio
async def test_server_binding_beats_default_guild_for_bot_mention(client):
    """``GUILD_ID`` is a last resort, not a preference — a server wired to a
    project parents the bot there even when the default points elsewhere."""
    _test_client, db_url = client
    insert_guild(db_url, "g-defalt", owner_user_id="gh-owner-default")
    insert_guild(db_url, "g-wired5", owner_user_id="gh-owner-bot5")
    _insert_channel_guild(
        db_url, "channel-wired-bot5", "g-wired5", discord_guild_id="discord-server-bot5"
    )

    with (
        patch.dict(os.environ, {"DISCORD_APPLICATION_ID": "bot-id-1", "GUILD_ID": "g-defalt"}),
        patch.object(router, "_persist_inbound_message", new=AsyncMock()),
        patch("ws_handlers._trigger_foreman", new=AsyncMock()) as mock_trigger,
        patch("foreman.runner.reset_foreman_poll"),
    ):
        await router._route_inbound_message(
            {
                "content": "<@bot-id-1> status?",
                "channel_id": "channel-unwired-bot5",
                "guild_id": "discord-server-bot5",
                "author": {"id": "d-bot-5", "username": "mecha", "bot": True},
                "mentions": [{"id": "bot-id-1"}],
            }
        )

    assert mock_trigger.await_count == 1
    args, _kwargs = mock_trigger.await_args
    assert args[0] == "g-wired5"

    with _sync_session(db_url) as session:
        bot_user = (
            session.execute(select(User).where(col(User.id) == "discordbot:d-bot-5"))
            .scalars()
            .one()
        )
        assert bot_user.parent_user_id == "gh-owner-bot5"


# ---------------------------------------------------------------------------
# resolve_guild_slug — shared by slash commands (routes/discord.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_guild_slug_prefers_the_channel_binding(client):
    """The channel a command was typed in wins over the server and the default."""
    _test_client, db_url = client
    insert_guild(db_url, "g-channel")
    insert_guild(db_url, "g-server")
    _insert_channel_guild(db_url, "channel-cmd-1", "g-channel", discord_guild_id="discord-cmd-1")
    _insert_channel_guild(db_url, "channel-cmd-other", "g-server", discord_guild_id="discord-cmd-1")

    with patch.dict(os.environ, {"GUILD_ID": "g-defalt"}):
        slug = await router.resolve_guild_slug("channel-cmd-1", "discord-cmd-1")

    assert slug == "g-channel"


@pytest.mark.asyncio
async def test_resolve_guild_slug_falls_back_to_server_binding(client):
    """An unbound channel falls back to a project the Discord server is wired to."""
    _test_client, db_url = client
    # The server-binding lookup joins Guild, so the project row must exist —
    # unlike the channel-binding lookup, which reads ps_guild_id directly.
    insert_guild(db_url, "g-server2")
    _insert_channel_guild(db_url, "channel-cmd-2", "g-server2", discord_guild_id="discord-cmd-2")

    with patch.dict(os.environ, {"GUILD_ID": "g-defalt"}):
        slug = await router.resolve_guild_slug("channel-cmd-unbound", "discord-cmd-2")

    assert slug == "g-server2"


@pytest.mark.asyncio
async def test_resolve_guild_slug_falls_back_to_instance_default(client):
    """Nothing bound anywhere — the instance default is the last resort."""
    _test_client, _db_url = client

    with patch.dict(os.environ, {"GUILD_ID": "g-defalt"}):
        slug = await router.resolve_guild_slug("channel-cmd-nowhere", "discord-cmd-nowhere")

    assert slug == "g-defalt"


@pytest.mark.asyncio
async def test_resolve_guild_slug_returns_none_when_nothing_resolves(client):
    """No binding and no GUILD_ID — the caller decides what to do with None."""
    _test_client, _db_url = client

    with patch.dict(os.environ, {"GUILD_ID": ""}):
        slug = await router.resolve_guild_slug("channel-cmd-nowhere", "discord-cmd-nowhere")

    assert slug is None


@pytest.mark.asyncio
async def test_resolve_guild_slug_routes_a_thread_to_its_task_guild(client):
    """A command typed in a task's thread resolves to that task's guild — the
    thread bindings the chat path uses, not just /join-channel wiring."""
    _test_client, db_url = client
    insert_guild(db_url, "g-thread")
    insert_task(db_url, "g-thread", "t-thread-cmd", state="working")
    _insert_binding(db_url, "task_stream", "t-thread-cmd", "thread-cmd-1")

    with patch.dict(os.environ, {"GUILD_ID": "g-defalt"}):
        slug = await router.resolve_guild_slug("thread-cmd-1", "discord-cmd-3")

    assert slug == "g-thread"
