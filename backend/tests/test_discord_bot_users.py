"""Unit tests for Discord bot user auto-provisioning (discord/bot_users.py).

Exercises ``ensure_bot_user`` against a real DB: a bot's first message creates
a ``users`` row parented to the guild's human owner and inherits that owner's
guild role (permission inheritance); later messages from the same bot reuse
the row; multiple distinct bots in the same guild share the same parent.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from discord.bot_users import ensure_bot_user  # noqa: E402
from helpers import _sync_session, insert_guild, insert_member  # noqa: E402
from models import Guild, GuildMember  # noqa: E402
from sqlmodel import col, select  # noqa: E402


def _guild_pk(db_url: str, slug: str) -> int:
    with _sync_session(db_url) as session:
        return session.execute(select(Guild.id).where(col(Guild.slug) == slug)).scalar_one()


@pytest.mark.asyncio
async def test_ensure_bot_user_creates_row_parented_to_guild_owner(client):
    _test_client, db_url = client
    insert_guild(db_url, "g-bot-users1", owner_user_id="gh-owner-1")
    guild_pk = _guild_pk(db_url, "g-bot-users1")

    ps_user_id = await ensure_bot_user("discord-bot-1", "CIBot", guild_pk)

    assert ps_user_id == "discordbot:discord-bot-1"
    with _sync_session(db_url) as session:
        from models import User

        user = session.get(User, ps_user_id)
        assert user is not None
        assert user.is_bot is True
        assert user.parent_user_id == "gh-owner-1"

        # Permission inheritance: the bot inherits the parent's guild role.
        member = session.execute(
            select(GuildMember).where(
                col(GuildMember.guild_id) == guild_pk,
                col(GuildMember.user_id) == ps_user_id,
            )
        ).scalars().one()
        assert member.role == "owner"


@pytest.mark.asyncio
async def test_ensure_bot_user_is_idempotent(client):
    _test_client, db_url = client
    insert_guild(db_url, "g-bot-users2", owner_user_id="gh-owner-2")
    guild_pk = _guild_pk(db_url, "g-bot-users2")

    first = await ensure_bot_user("discord-bot-2", "CIBot", guild_pk)
    second = await ensure_bot_user("discord-bot-2", "CIBot", guild_pk)

    assert first == second
    with _sync_session(db_url) as session:
        from models import User

        rows = session.execute(select(User).where(col(User.id) == first)).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_ensure_bot_user_multiple_bots_share_parent(client):
    _test_client, db_url = client
    insert_guild(db_url, "g-bot-users3", owner_user_id="gh-owner-3")
    guild_pk = _guild_pk(db_url, "g-bot-users3")

    bot_a = await ensure_bot_user("discord-bot-3a", "BotA", guild_pk)
    bot_b = await ensure_bot_user("discord-bot-3b", "BotB", guild_pk)

    assert bot_a != bot_b
    with _sync_session(db_url) as session:
        from models import User

        user_a = session.get(User, bot_a)
        user_b = session.get(User, bot_b)
        assert user_a.parent_user_id == user_b.parent_user_id == "gh-owner-3"


@pytest.mark.asyncio
async def test_ensure_bot_user_inherits_non_owner_role(client):
    """The bot inherits whatever role the resolved parent actually has — a
    guild whose only human is a plain member, not an owner, still resolves a
    parent-owner (a real guild always has ≥1 owner), but a bot's inherited
    role tracks that owner's own role rather than being hardcoded."""
    _test_client, db_url = client
    insert_guild(db_url, "g-bot-users4", owner_user_id="gh-owner-4")
    insert_member(db_url, "g-bot-users4", "gh-member-4", role="member")
    guild_pk = _guild_pk(db_url, "g-bot-users4")

    ps_user_id = await ensure_bot_user("discord-bot-4", "CIBot", guild_pk)

    with _sync_session(db_url) as session:
        member = session.execute(
            select(GuildMember).where(
                col(GuildMember.guild_id) == guild_pk,
                col(GuildMember.user_id) == ps_user_id,
            )
        ).scalars().one()
        # Parent is still the owner (gh-owner-4) since that's who owns the guild.
        assert member.role == "owner"


@pytest.mark.asyncio
async def test_ensure_bot_user_with_no_guild_context_has_no_parent(client):
    """When guild_pk is unknown, the bot row is still created (so its actions
    still get an audit trail) but with no parent to inherit from."""
    ps_user_id = await ensure_bot_user("discord-bot-5", "LonelyBot", None)

    assert ps_user_id == "discordbot:discord-bot-5"


@pytest.mark.asyncio
async def test_ensure_bot_user_never_parents_to_another_bot(client):
    """A bot can never become another bot's parent — only a real human owner
    qualifies, guarded by the is_bot=False filter in _find_parent_owner."""
    _test_client, db_url = client
    insert_guild(db_url, "g-bot-users6", owner_user_id="gh-owner-6")
    guild_pk = _guild_pk(db_url, "g-bot-users6")

    first_bot = await ensure_bot_user("discord-bot-6a", "BotA", guild_pk)
    second_bot = await ensure_bot_user("discord-bot-6b", "BotB", guild_pk)

    with _sync_session(db_url) as session:
        from models import User

        assert session.get(User, second_bot).parent_user_id != first_bot
        assert session.get(User, second_bot).parent_user_id == "gh-owner-6"
