"""Shared Discord role-ID logic: member authorization plus foreman-role mention
detection.

Both the slash-command interaction handler (``routes/discord.py``) and the
inbound chat router (Phase 5, ``discord/router.py``) gate cost-incurring
actions on the same ``DISCORD_ALLOWED_ROLE_IDS`` allowlist, so the check
lives here once instead of being duplicated. The Gateway transport
(``discord/router.py``) and consumer share the foreman-role mention check the
same way.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache


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


# The foreman role: a message that @-mentions this role always gets a
# response, regardless of channel/guild wiring — see the routing-model
# exception in ``discord/router.py``'s module docstring. Override via env for
# testing/ops without a code change.
FOREMAN_ROLE_ID = os.environ.get("DISCORD_FOREMAN_ROLE_ID") or "1522965022836396178"

_FOREMAN_ROLE_MENTION_RE = re.compile(rf"<@&{re.escape(FOREMAN_ROLE_ID)}>")


def mentions_foreman_role(message: dict) -> bool:
    """Return True if *message* @-mentions the foreman role.

    Checks Discord's structured ``mention_roles`` array first (populated on
    guild messages), then falls back to a literal ``<@&role_id>`` scan of the
    raw content — ``mention_roles`` is absent/empty on DMs (Discord has no
    guild there to resolve a role mention against) even when a user pastes
    the raw mention token directly.
    """
    mention_roles = message.get("mention_roles") or []
    if any(str(role_id) == FOREMAN_ROLE_ID for role_id in mention_roles):
        return True
    return bool(_FOREMAN_ROLE_MENTION_RE.search(message.get("content") or ""))


def strip_foreman_role_mention(content: str) -> str:
    """Remove every ``<@&role_id>`` foreman-role mention token from *content*."""
    return _FOREMAN_ROLE_MENTION_RE.sub("", content).strip()


# The bot user itself: @pioneer-square. Mentioning it is the ordinary way to
# ask for a response, and gets the same "always answer, anywhere" treatment as
# the foreman role above. Read from the environment per call (rather than
# captured at import like FOREMAN_ROLE_ID) because the Gateway's own-bot filter
# reads the same var and tests toggle it.
def bot_user_id() -> str | None:
    return os.environ.get("DISCORD_APPLICATION_ID") or None


@lru_cache(maxsize=4)
def _bot_mention_re(bot_id: str) -> re.Pattern[str]:
    """``<@id>``/``<@!id>`` — Discord emits the ``!`` form for nickname mentions."""
    return re.compile(rf"<@!?{re.escape(bot_id)}>")


def mentions_bot_user(message: dict) -> bool:
    """Return True if *message* @-mentions this application's bot user.

    Checks Discord's structured ``mentions`` array first, then falls back to a
    literal scan of the raw content, for the same reason
    ``mentions_foreman_role`` does — the structured array is not guaranteed to
    be populated on every message shape, and a pasted raw mention token should
    still count. Always False when ``DISCORD_APPLICATION_ID`` is unset, since
    there is then no id to compare against.
    """
    bot_id = bot_user_id()
    if not bot_id:
        return False
    mentions = message.get("mentions") or []
    if any(str(user.get("id")) == bot_id for user in mentions if isinstance(user, dict)):
        return True
    return bool(_bot_mention_re(bot_id).search(message.get("content") or ""))


def strip_bot_user_mention(content: str) -> str:
    """Remove every ``<@bot_id>``/``<@!bot_id>`` mention token from *content*."""
    bot_id = bot_user_id()
    if not bot_id:
        return content.strip()
    return _bot_mention_re(bot_id).sub("", content).strip()
