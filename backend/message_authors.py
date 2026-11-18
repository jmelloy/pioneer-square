"""Author display fields (``authorName``/``authorAvatar``) for chat messages (#1492).

Discord messages carry their author's Discord identity in ``Message.meta``
(see ``discord/router.py``); everything else is resolved from the ``users``
row behind ``Message.user_id`` — a GitHub identity for humans.
"""

from __future__ import annotations

from models import User
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession


def discord_author(author: dict) -> dict:
    """Author fields from a Discord gateway ``author`` object."""
    author_id, avatar = author.get("id"), author.get("avatar")
    return {
        "authorName": author.get("global_name") or author.get("username"),
        # ponytail: no avatar hash → None; the frontend falls back to an initial.
        "authorAvatar": (
            f"https://cdn.discordapp.com/avatars/{author_id}/{avatar}.png?size=64"
            if author_id and avatar
            else None
        ),
    }


def _user_author(user: User) -> dict:
    avatar = user.avatar_url
    if not avatar and not user.is_bot and user.github_id:
        avatar = f"https://avatars.githubusercontent.com/u/{user.github_id}?v=4"
    return {"authorName": user.display_name or user.github_login, "authorAvatar": avatar}


async def user_authors(db: AsyncSession, user_ids) -> dict[str, dict]:
    """Map each users.id in *user_ids* to its author fields."""
    ids = {uid for uid in user_ids if uid}
    if not ids:
        return {}
    users = (await db.exec(select(User).where(col(User.id).in_(ids)))).all()
    return {u.id: _user_author(u) for u in users}


async def fill_authors(db: AsyncSession, payloads: list[dict]) -> None:
    """Add author fields to user message payloads that don't already carry them."""
    pending = [p for p in payloads if p.get("user_id") and not p.get("authorName")]
    authors = await user_authors(db, (p["user_id"] for p in pending))
    for p in pending:
        p.update(authors.get(p["user_id"], {}))
