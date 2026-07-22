"""Unit tests for the message_discord_bot foreman tool.

All external calls (Discord bot API, DB) are mocked/real-test-db — no real
network usage.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from _test_config import TEST_DATABASE_URL  # noqa: E402
from foreman.tools import exec_tools
from helpers import _sync_session, create_db, insert_guild, truncate_all
from models import User

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(monkeypatch):
    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)
    db_url = TEST_DATABASE_URL
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)
    yield db_url


def _fake_tu(name: str, inputs: dict, tool_id: str = "tool-discord1") -> SimpleNamespace:
    return SimpleNamespace(name=name, input=inputs, id=tool_id)


def _insert_bot_user(
    db_url: str,
    user_id: str,
    *,
    is_bot: bool = True,
    bot_provider: str | None = "discord",
    discord_channel_id: str | None = "chan-123",
    bot_metadata: dict | None = None,
) -> None:
    if bot_metadata is None:
        bot_metadata = {"discord_user_id": "999888777", "discord_username": "CIBot"}
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.execute(
            pg_insert(User)
            .values(
                id=user_id,
                github_id=user_id,
                github_login=f"discordbot:{user_id}",
                display_name="CIBot",
                is_bot=is_bot,
                bot_provider=bot_provider,
                discord_channel_id=discord_channel_id,
                bot_metadata=bot_metadata,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.commit()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestMessageDiscordBotValidation:
    @pytest.mark.asyncio
    async def test_missing_bot_user_id_returns_error(self, db_session):
        insert_guild(db_session, "g-mdb-nouser")
        results = await exec_tools(
            "g-mdb-nouser",
            [_fake_tu("message_discord_bot", {"message": "hi"})],
        )
        assert results[0].get("is_error") is True
        assert "bot_user_id" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_missing_message_returns_error(self, db_session):
        insert_guild(db_session, "g-mdb-nomsg")
        results = await exec_tools(
            "g-mdb-nomsg",
            [_fake_tu("message_discord_bot", {"bot_user_id": "discordbot:1"})],
        )
        assert results[0].get("is_error") is True
        assert "message" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_unknown_bot_user_id_returns_error(self, db_session):
        insert_guild(db_session, "g-mdb-unknown")
        results = await exec_tools(
            "g-mdb-unknown",
            [
                _fake_tu(
                    "message_discord_bot",
                    {"bot_user_id": "discordbot:missing", "message": "hi"},
                )
            ],
        )
        assert results[0].get("is_error") is True
        assert "discordbot:missing" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_non_bot_user_returns_error(self, db_session):
        insert_guild(db_session, "g-mdb-human")
        _insert_bot_user(db_session, "gh-human-1", is_bot=False, bot_provider=None)
        results = await exec_tools(
            "g-mdb-human",
            [_fake_tu("message_discord_bot", {"bot_user_id": "gh-human-1", "message": "hi"})],
        )
        assert results[0].get("is_error") is True
        assert "not a Discord bot" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_non_discord_bot_provider_returns_error(self, db_session):
        insert_guild(db_session, "g-mdb-otherprovider")
        _insert_bot_user(db_session, "bot:slack-1", bot_provider="slack")
        results = await exec_tools(
            "g-mdb-otherprovider",
            [_fake_tu("message_discord_bot", {"bot_user_id": "bot:slack-1", "message": "hi"})],
        )
        assert results[0].get("is_error") is True
        assert "not a Discord bot" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_missing_channel_returns_error(self, db_session):
        insert_guild(db_session, "g-mdb-nochannel")
        _insert_bot_user(db_session, "discordbot:nochan", discord_channel_id=None)
        results = await exec_tools(
            "g-mdb-nochannel",
            [
                _fake_tu(
                    "message_discord_bot", {"bot_user_id": "discordbot:nochan", "message": "hi"}
                )
            ],
        )
        assert results[0].get("is_error") is True
        assert "discord_channel_id" in results[0]["content"]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestMessageDiscordBotHappyPath:
    @pytest.mark.asyncio
    async def test_channel_delivery_posts_message_content(self, db_session):
        insert_guild(db_session, "g-mdb-channel")
        _insert_bot_user(db_session, "discordbot:chan1", discord_channel_id="chan-abc")

        with patch(
            "foreman.tools.discord_notifier.post", new=AsyncMock(return_value={"id": "msg-1"})
        ) as mock_post:
            results = await exec_tools(
                "g-mdb-channel",
                [
                    _fake_tu(
                        "message_discord_bot",
                        {"bot_user_id": "discordbot:chan1", "message": "hello bot"},
                    )
                ],
            )

        assert results[0].get("is_error") is not True
        mock_post.assert_awaited_once_with("/channels/chan-abc/messages", {"content": "hello bot"})
        parsed = json.loads(results[0]["content"])
        assert parsed["channel_id"] == "chan-abc"
        assert parsed["delivery_method"] == "channel"
        assert parsed["message_id"] == "msg-1"

    @pytest.mark.asyncio
    async def test_mention_delivery_prefixes_content_with_mention(self, db_session):
        insert_guild(db_session, "g-mdb-mention")
        _insert_bot_user(
            db_session,
            "discordbot:mention1",
            discord_channel_id="chan-xyz",
            bot_metadata={"discord_user_id": "555444333"},
        )

        with patch(
            "foreman.tools.discord_notifier.post", new=AsyncMock(return_value={"id": "msg-2"})
        ) as mock_post:
            results = await exec_tools(
                "g-mdb-mention",
                [
                    _fake_tu(
                        "message_discord_bot",
                        {
                            "bot_user_id": "discordbot:mention1",
                            "message": "please respond",
                            "delivery_method": "mention",
                        },
                    )
                ],
            )

        assert results[0].get("is_error") is not True
        mock_post.assert_awaited_once_with(
            "/channels/chan-xyz/messages",
            {"content": "<@555444333> please respond"},
        )
        parsed = json.loads(results[0]["content"])
        assert parsed["delivery_method"] == "mention"

    @pytest.mark.asyncio
    async def test_mention_delivery_without_discord_user_id_errors(self, db_session):
        insert_guild(db_session, "g-mdb-mentionerr")
        _insert_bot_user(
            db_session,
            "discordbot:mentionerr1",
            discord_channel_id="chan-xyz",
            bot_metadata={},
        )

        results = await exec_tools(
            "g-mdb-mentionerr",
            [
                _fake_tu(
                    "message_discord_bot",
                    {
                        "bot_user_id": "discordbot:mentionerr1",
                        "message": "hi",
                        "delivery_method": "mention",
                    },
                )
            ],
        )

        assert results[0].get("is_error") is True
        assert "discord_user_id" in results[0]["content"]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestMessageDiscordBotErrors:
    @pytest.mark.asyncio
    async def test_discord_api_failure_surfaces_as_error(self, db_session):
        insert_guild(db_session, "g-mdb-apierr")
        _insert_bot_user(db_session, "discordbot:apierr1", discord_channel_id="chan-err")

        with patch("foreman.tools.discord_notifier.post", new=AsyncMock(return_value=None)):
            results = await exec_tools(
                "g-mdb-apierr",
                [
                    _fake_tu(
                        "message_discord_bot",
                        {"bot_user_id": "discordbot:apierr1", "message": "hi"},
                    )
                ],
            )

        assert results[0].get("is_error") is True
        assert "Failed to send" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_result_has_correct_tool_use_id(self, db_session):
        insert_guild(db_session, "g-mdb-tid")
        _insert_bot_user(db_session, "discordbot:tid1", discord_channel_id="chan-tid")

        with patch(
            "foreman.tools.discord_notifier.post", new=AsyncMock(return_value={"id": "msg-3"})
        ):
            results = await exec_tools(
                "g-mdb-tid",
                [
                    _fake_tu(
                        "message_discord_bot",
                        {"bot_user_id": "discordbot:tid1", "message": "hi"},
                        "my-discord-tool-id",
                    )
                ],
            )

        assert results[0]["tool_use_id"] == "my-discord-tool-id"
        assert results[0]["type"] == "tool_result"
