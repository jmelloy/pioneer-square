"""Auto-provisioning of ``users`` rows for Discord bot accounts.

Discord bots (other applications' bot users, not this application's own bot —
see ``gateway._handle_message_create``'s self-message filter) can post in wired
channels/threads and today have no Pioneer Square identity: ``router._resolve_identity``
only *looks up* an existing mapping and returns ``None`` for anything unmapped,
which means a bot's messages get attributed to nobody (no audit trail) and
inherit no permissions.

``ensure_bot_user`` fills that gap the first time a given Discord bot is seen in
a guild: it creates a ``users`` row (``is_bot=True``), points ``parent_user_id``
at a real human — the guild's earliest owner, standing in for "whoever is
responsible for what's installed in this guild" since Discord's API does not
expose who invited a given bot — and mirrors the parent's ``guild_members`` role
onto the new bot row so the bot inherits the same guild permissions. A
``discord_account_links`` row is also written so subsequent messages from the
same Discord bot resolve instantly via the existing lookup path in
``router._resolve_identity`` without re-running this provisioning logic.

A bot is never chosen as a parent (guarded via ``User.is_bot == False`` on the
owner query), so bot->bot parent chains cannot form.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from models import DiscordAccountLink, GuildMember, User
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select

logger = logging.getLogger(__name__)


def _bot_ps_user_id(discord_user_id: str) -> str:
    """Deterministic, stable ps_user_id for a Discord bot — never collides with
    a GitHub numeric id (`users.id` for humans), so the two id spaces can share
    the same column safely."""
    return f"discordbot:{discord_user_id}"


async def _find_parent_owner(db, guild_pk: int) -> tuple[str | None, str]:
    """Return ``(user_id, role)`` for the guild's earliest human owner, or
    ``(None, "member")`` if the guild has no human owner on record."""
    result = await db.exec(
        select(GuildMember.user_id, GuildMember.role)
        .join(User, col(User.id) == col(GuildMember.user_id))
        .where(
            col(GuildMember.guild_id) == guild_pk,
            col(GuildMember.role) == "owner",
            col(User.is_bot).is_(False),
        )
        .order_by(col(GuildMember.created_at).asc())
        .limit(1)
    )
    row = result.first()
    if row is None:
        return None, "member"
    return row[0], row[1]


async def ensure_bot_user(
    discord_user_id: str,
    username: str | None,
    guild_pk: int | None,
    channel_id: str | None = None,
) -> str:
    """Find-or-create the Pioneer Square user row for a Discord bot account.

    Idempotent — safe to call for a bot that already has a row (returns the
    existing id without writing anything). ``guild_pk`` is used only to pick a
    parent/inherited role for a *new* row; pass ``None`` when unknown (the bot
    row is still created, just with no parent). ``channel_id`` is the Discord
    channel the bot was first seen in — persisted as its preferred channel for
    ``message_discord_bot`` (see ``foreman/tools.py``); pass ``None`` when
    unknown (e.g. a DM).
    """
    from database import AsyncSessionLocal  # noqa: PLC0415

    ps_user_id = _bot_ps_user_id(discord_user_id)

    async with AsyncSessionLocal() as db:
        existing = await db.exec(select(User.id).where(col(User.id) == ps_user_id))
        if existing.first():
            return ps_user_id

        now = datetime.now(UTC)
        parent_user_id: str | None = None
        parent_role = "member"
        if guild_pk is not None:
            parent_user_id, parent_role = await _find_parent_owner(db, guild_pk)
            if parent_user_id is None:
                logger.warning(
                    "discord bot_users: no human owner found for guild_pk=%s — "
                    "provisioning bot=%s with no parent",
                    guild_pk,
                    discord_user_id,
                )

        label = username or discord_user_id
        user_stmt = pg_insert(User).values(
            id=ps_user_id,
            github_id=ps_user_id,
            github_login=f"discordbot:{label}",
            display_name=label,
            is_bot=True,
            parent_user_id=parent_user_id,
            bot_provider="discord",
            discord_channel_id=channel_id,
            bot_metadata={"discord_user_id": discord_user_id, "discord_username": username},
            created_at=now,
            updated_at=now,
        )
        user_stmt = user_stmt.on_conflict_do_nothing(index_elements=["id"])
        await db.exec(user_stmt)

        link_stmt = pg_insert(DiscordAccountLink).values(
            ps_user_id=ps_user_id,
            discord_user_id=discord_user_id,
            discord_username=username or "",
            created_at=now,
        )
        link_stmt = link_stmt.on_conflict_do_update(
            index_elements=["discord_user_id"],
            set_={
                "ps_user_id": link_stmt.excluded.ps_user_id,
                "discord_username": link_stmt.excluded.discord_username,
            },
        )
        await db.exec(link_stmt)

        if guild_pk is not None and parent_user_id is not None:
            member_stmt = pg_insert(GuildMember).values(
                guild_id=guild_pk,
                user_id=ps_user_id,
                role=parent_role,
                created_at=now,
            )
            member_stmt = member_stmt.on_conflict_do_nothing(index_elements=["guild_id", "user_id"])
            await db.exec(member_stmt)

        await db.commit()
        logger.info(
            "discord bot_users: provisioned bot user %s (discord_id=%s parent=%s)",
            ps_user_id,
            discord_user_id,
            parent_user_id,
        )
        return ps_user_id
