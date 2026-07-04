"""Shared Discord role-allowlist authorization check.

Both the slash-command interaction handler (``routes/discord.py``) and the
inbound chat router (Phase 5, ``discord/router.py``) gate cost-incurring
actions on the same ``DISCORD_ALLOWED_ROLE_IDS`` allowlist, so the check
lives here once instead of being duplicated.
"""

from __future__ import annotations

import os


def allowed_role_ids() -> set[str]:
    raw = os.environ.get("DISCORD_ALLOWED_ROLE_IDS", "")
    return {r.strip() for r in raw.split(",") if r.strip()}


def is_member_authorized(member: dict | None) -> bool:
    """Return True if *member* (a Discord partial-member object) has an allowed role.

    With no allowlist configured, everyone is allowed. A missing *member*
    (DM messages/interactions carry no member/role data) is denied once a
    restriction is configured.
    """
    allowed = allowed_role_ids()
    if not allowed:
        return True
    if not member:
        return False
    user_roles: list[str] = member.get("roles", [])
    return bool(set(user_roles) & allowed)
