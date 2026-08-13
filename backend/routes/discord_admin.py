"""Discord account & bot management REST surface for the Pioneer Square UI.

Everything here is read-mostly and guild-scoped, sitting on top of tables and
env vars that already exist for the Discord integration (see
``docs/discord.md``):

- **Status** (``GET .../discord/status``) — the bot is configured instance-wide
  via env vars (``DISCORD_BOT_TOKEN`` etc.), not a per-guild DB row, so this
  reports which env vars are set plus the live Gateway task state rather than
  reading a "bot config" table that doesn't exist.
- **Accounts** (``.../discord/accounts``) — ``discord_account_links`` rows for
  human users who are members of this guild. Accounts are created only by
  redeeming ``/connect-account`` in Discord (see ``routes/discord_connect.py``);
  this surface adds the ability to unlink one, which previously had no UI or
  REST path.
- **Channels** (``.../discord/channels``) — CRUD over ``discord_channel_guilds``,
  the same table ``/join-channel``/``/leave-channel`` manage from Discord. This
  gives guild owners a REST/UI equivalent.
- **Bots** (``.../discord/bots``) — auto-provisioned ``users`` rows for other
  Discord bots seen posting in this guild (see ``discord/bot_users.py``).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from auth_deps import get_guild_pk, require_member
from database import get_db_dep
from fastapi import APIRouter, Depends, HTTPException
from models import DiscordAccountLink, DiscordChannelGuild, GuildMember, User
from pydantic import BaseModel, Field
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from utils import row_to_dict

router = APIRouter()


def _split_env_list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@router.get("/api/guilds/{guild_id}/discord/status")
async def get_discord_status(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Report the instance's Discord bot configuration and live health.

    There is exactly one bot per Pioneer Square instance, configured via env
    vars — not a per-guild DB row — so this reflects which vars are set
    rather than a queryable "bot" entity.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    from discord.gateway import gateway_status  # noqa: PLC0415

    bot_token_set = bool(os.environ.get("DISCORD_BOT_TOKEN"))
    channel_id_set = bool(os.environ.get("DISCORD_CHANNEL_ID"))

    return {
        "bot_configured": bot_token_set and channel_id_set,
        "bot_token_set": bot_token_set,
        "channel_id_set": channel_id_set,
        "slash_commands_configured": bool(os.environ.get("DISCORD_APPLICATION_ID"))
        and bool(os.environ.get("DISCORD_PUBLIC_KEY")),
        "application_id_set": bool(os.environ.get("DISCORD_APPLICATION_ID")),
        "public_key_set": bool(os.environ.get("DISCORD_PUBLIC_KEY")),
        "stream_tasks_enabled": os.environ.get("DISCORD_STREAM_TASKS", "").strip().lower()
        in ("1", "true", "yes", "on"),
        "pioneer_guild_slug": os.environ.get("DISCORD_PIONEER_GUILD_SLUG") or None,
        "allowed_role_ids": _split_env_list(os.environ.get("DISCORD_ALLOWED_ROLE_IDS")),
        "operator_role_name": os.environ.get("DISCORD_OPERATOR_ROLE_NAME")
        or "Pioneer Square Operator",
        "gateway": gateway_status(),
    }


@router.get("/api/guilds/{guild_id}/discord/accounts")
async def list_discord_accounts(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """List Discord accounts linked to human members of this guild."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    result = await db.exec(
        select(
            col(User.id).label("user_id"),
            col(User.github_login),
            col(User.display_name),
            col(User.avatar_url),
            col(DiscordAccountLink.discord_user_id),
            col(DiscordAccountLink.discord_username),
            col(DiscordAccountLink.created_at).label("linked_at"),
        )
        .select_from(GuildMember)
        .join(User, col(User.id) == col(GuildMember.user_id))
        .join(DiscordAccountLink, col(DiscordAccountLink.ps_user_id) == col(User.id))
        .where(col(GuildMember.guild_id) == guild_pk, col(User.is_bot).is_(False))
        .order_by(col(DiscordAccountLink.created_at).asc())
    )
    return [dict(r._mapping) for r in result.all()]


@router.delete("/api/guilds/{guild_id}/discord/accounts/{discord_user_id}")
async def unlink_discord_account(
    guild_id: str,
    discord_user_id: str,
    github_user_id: str = Depends(require_member("owner")),
    db: AsyncSession = Depends(get_db_dep),
):
    """Unlink a Discord account from Pioneer Square (owner only).

    Scoped to accounts belonging to members of this guild, so an owner can't
    unlink an account through a guild the linked user has nothing to do with.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    result = await db.exec(
        select(DiscordAccountLink)
        .join(GuildMember, col(GuildMember.user_id) == col(DiscordAccountLink.ps_user_id))
        .where(
            col(DiscordAccountLink.discord_user_id) == discord_user_id,
            col(GuildMember.guild_id) == guild_pk,
        )
    )
    link = result.one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="Discord account link not found")

    await db.delete(link)
    await db.commit()
    return {"status": "unlinked", "discord_user_id": discord_user_id}


class ChannelBindingCreate(BaseModel):
    discord_guild_id: str = Field(min_length=1)
    discord_channel_id: str = Field(min_length=1)


@router.get("/api/guilds/{guild_id}/discord/channels")
async def list_discord_channels(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """List Discord channels wired to this guild for event routing."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    result = await db.exec(
        select(DiscordChannelGuild)
        .where(col(DiscordChannelGuild.ps_guild_id) == guild_id)
        .order_by(col(DiscordChannelGuild.created_at).asc())
    )
    return [row_to_dict(b) for b in result.all()]


@router.post("/api/guilds/{guild_id}/discord/channels")
async def add_discord_channel(
    guild_id: str,
    data: ChannelBindingCreate,
    github_user_id: str = Depends(require_member("owner")),
    db: AsyncSession = Depends(get_db_dep),
):
    """Wire a Discord channel to this guild for event routing (owner only).

    Equivalent to running ``/join-channel`` in Discord — updates the binding
    in place if the (discord_guild_id, discord_channel_id) pair is already
    wired to a different Pioneer Square guild.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    result = await db.exec(
        select(DiscordChannelGuild).where(
            col(DiscordChannelGuild.discord_guild_id) == data.discord_guild_id,
            col(DiscordChannelGuild.discord_channel_id) == data.discord_channel_id,
        )
    )
    existing = result.one_or_none()
    if existing:
        existing.ps_guild_id = guild_id
        db.add(existing)
        await db.commit()
        await db.refresh(existing)
        return row_to_dict(existing)

    binding = DiscordChannelGuild(
        discord_guild_id=data.discord_guild_id,
        discord_channel_id=data.discord_channel_id,
        ps_guild_id=guild_id,
        created_at=datetime.now(UTC),
    )
    db.add(binding)
    await db.commit()
    await db.refresh(binding)
    return row_to_dict(binding)


@router.delete("/api/guilds/{guild_id}/discord/channels/{binding_id}")
async def remove_discord_channel(
    guild_id: str,
    binding_id: int,
    github_user_id: str = Depends(require_member("owner")),
    db: AsyncSession = Depends(get_db_dep),
):
    """Remove a Discord channel binding from this guild (owner only)."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    result = await db.exec(
        select(DiscordChannelGuild).where(
            col(DiscordChannelGuild.id) == binding_id,
            col(DiscordChannelGuild.ps_guild_id) == guild_id,
        )
    )
    binding = result.one_or_none()
    if binding is None:
        raise HTTPException(status_code=404, detail="Channel binding not found")

    await db.delete(binding)
    await db.commit()
    return {"status": "removed", "id": binding_id}


@router.get("/api/guilds/{guild_id}/discord/bots")
async def list_discord_bots(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """List auto-provisioned Discord bot identities seen in this guild.

    These are ``users`` rows created by ``discord/bot_users.py`` the first
    time some *other* application's Discord bot posts in a wired channel —
    not Pioneer Square's own bot, which has no per-guild DB identity (see
    ``get_discord_status``).
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    result = await db.exec(
        select(
            col(User.id).label("user_id"),
            col(User.display_name),
            col(User.discord_channel_id),
            col(User.bot_metadata),
            col(User.parent_user_id),
            col(User.created_at),
            col(GuildMember.role),
        )
        .join(GuildMember, col(GuildMember.user_id) == col(User.id))
        .where(
            col(GuildMember.guild_id) == guild_pk,
            col(User.is_bot).is_(True),
            col(User.bot_provider) == "discord",
        )
        .order_by(col(User.created_at).asc())
    )
    return [dict(r._mapping) for r in result.all()]
