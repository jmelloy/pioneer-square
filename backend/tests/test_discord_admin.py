"""Tests for the Discord account/bot management REST surface (routes/discord_admin.py)."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import _sync_session, insert_guild, insert_member, make_auth_token
from models import DiscordAccountLink, DiscordChannelGuild, User


def _link_discord_account(
    db_url: str, ps_user_id: str, discord_user_id: str, discord_username: str = "dUser"
) -> None:
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.add(
            DiscordAccountLink(
                ps_user_id=ps_user_id,
                discord_user_id=discord_user_id,
                discord_username=discord_username,
                created_at=now,
            )
        )
        session.commit()


def _insert_bot_user(
    db_url: str, guild_slug: str, user_id: str, parent_user_id: str, discord_channel_id: str
) -> None:
    from models import Guild, GuildMember
    from sqlalchemy import select
    from sqlmodel import col

    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.add(
            User(
                id=user_id,
                github_id=user_id,
                github_login=f"discordbot:{user_id}",
                display_name=user_id,
                is_bot=True,
                parent_user_id=parent_user_id,
                bot_provider="discord",
                discord_channel_id=discord_channel_id,
                created_at=now,
                updated_at=now,
            )
        )
        guild_pk = session.scalar(
            select(col(Guild.id)).where(
                col(Guild.slug) == guild_slug, col(Guild.deleted_at).is_(None)
            )
        )
        session.add(GuildMember(guild_id=guild_pk, user_id=user_id, role="member", created_at=now))
        session.commit()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_status_requires_membership(client):
    test_client, db_url = client
    insert_guild(db_url, "gd-status-1", owner_user_id=None)
    token = make_auth_token(db_url, user_id="gh-status-1", username="statususer1")
    resp = test_client.get(
        "/api/guilds/gd-status-1/discord/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_status_reports_env_config(client, monkeypatch):
    test_client, db_url = client
    token = make_auth_token(db_url, user_id="gh-status-2", username="statususer2")
    insert_guild(db_url, "gd-status-2", owner_user_id="gh-status-2")

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "chan-1")
    monkeypatch.delenv("DISCORD_APPLICATION_ID", raising=False)
    monkeypatch.delenv("DISCORD_PUBLIC_KEY", raising=False)

    resp = test_client.get(
        "/api/guilds/gd-status-2/discord/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bot_configured"] is True
    assert body["bot_token_set"] is True
    assert body["channel_id_set"] is True
    assert body["slash_commands_configured"] is False
    assert "gateway" in body
    assert body["gateway"]["enabled"] is False


def test_status_unknown_guild_returns_404(client):
    test_client, db_url = client
    token = make_auth_token(db_url, user_id="gh-status-3", username="statususer3")
    resp = test_client.get(
        "/api/guilds/gd-does-not-exist/discord/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403 or resp.status_code == 404


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def test_list_accounts_scoped_to_guild_members(client):
    test_client, db_url = client
    owner_token = make_auth_token(db_url, user_id="gh-acct-owner", username="acctowner")
    insert_guild(db_url, "gd-acct-1", owner_user_id="gh-acct-owner")
    insert_member(db_url, "gd-acct-1", "gh-acct-member")
    make_auth_token(db_url, user_id="gh-acct-outsider", username="acctoutsider")

    _link_discord_account(db_url, "gh-acct-owner", "dc-owner-1", "OwnerDisc")
    _link_discord_account(db_url, "gh-acct-member", "dc-member-1", "MemberDisc")
    _link_discord_account(db_url, "gh-acct-outsider", "dc-outsider-1", "OutsiderDisc")

    resp = test_client.get(
        "/api/guilds/gd-acct-1/discord/accounts",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    discord_ids = {row["discord_user_id"] for row in resp.json()}
    assert discord_ids == {"dc-owner-1", "dc-member-1"}


def test_unlink_account_requires_owner(client):
    test_client, db_url = client
    insert_guild(db_url, "gd-acct-2", owner_user_id="gh-acct-owner2")
    insert_member(db_url, "gd-acct-2", "gh-acct-member2")
    member_token = make_auth_token(db_url, user_id="gh-acct-member2", username="acctmember2")
    _link_discord_account(db_url, "gh-acct-member2", "dc-member-2")

    resp = test_client.delete(
        "/api/guilds/gd-acct-2/discord/accounts/dc-member-2",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert resp.status_code == 403


def test_unlink_account_removes_link(client):
    test_client, db_url = client
    owner_token = make_auth_token(db_url, user_id="gh-acct-owner3", username="acctowner3")
    insert_guild(db_url, "gd-acct-3", owner_user_id="gh-acct-owner3")
    insert_member(db_url, "gd-acct-3", "gh-acct-member3")
    _link_discord_account(db_url, "gh-acct-member3", "dc-member-3")

    resp = test_client.delete(
        "/api/guilds/gd-acct-3/discord/accounts/dc-member-3",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "unlinked"

    with _sync_session(db_url) as session:
        remaining = (
            session.query(DiscordAccountLink).filter_by(discord_user_id="dc-member-3").one_or_none()
        )
        assert remaining is None


def test_unlink_account_not_found(client):
    test_client, db_url = client
    owner_token = make_auth_token(db_url, user_id="gh-acct-owner4", username="acctowner4")
    insert_guild(db_url, "gd-acct-4", owner_user_id="gh-acct-owner4")

    resp = test_client.delete(
        "/api/guilds/gd-acct-4/discord/accounts/does-not-exist",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def test_add_list_remove_channel_binding(client):
    test_client, db_url = client
    owner_token = make_auth_token(db_url, user_id="gh-chan-owner", username="chanowner")
    insert_guild(db_url, "gd-chan-1", owner_user_id="gh-chan-owner")
    headers = {"Authorization": f"Bearer {owner_token}"}

    resp = test_client.post(
        "/api/guilds/gd-chan-1/discord/channels",
        json={"discord_guild_id": "dg-1", "discord_channel_id": "dc-chan-1"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    binding = resp.json()
    assert binding["ps_guild_id"] == "gd-chan-1"
    binding_id = binding["id"]

    resp = test_client.get("/api/guilds/gd-chan-1/discord/channels", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = test_client.delete(
        f"/api/guilds/gd-chan-1/discord/channels/{binding_id}", headers=headers
    )
    assert resp.status_code == 200, resp.text

    resp = test_client.get("/api/guilds/gd-chan-1/discord/channels", headers=headers)
    assert resp.json() == []


def test_add_channel_binding_requires_owner(client):
    test_client, db_url = client
    insert_guild(db_url, "gd-chan-2", owner_user_id="gh-chan-owner2")
    insert_member(db_url, "gd-chan-2", "gh-chan-member2")
    member_token = make_auth_token(db_url, user_id="gh-chan-member2", username="chanmember2")

    resp = test_client.post(
        "/api/guilds/gd-chan-2/discord/channels",
        json={"discord_guild_id": "dg-2", "discord_channel_id": "dc-chan-2"},
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert resp.status_code == 403


def test_add_channel_binding_rewires_existing_pair(client):
    """Re-adding the same (discord_guild_id, discord_channel_id) pair for a
    different Pioneer Square guild updates the binding in place, matching
    /join-channel's re-run behaviour."""
    test_client, db_url = client
    owner_a = make_auth_token(db_url, user_id="gh-chan-a", username="chana")
    owner_b = make_auth_token(db_url, user_id="gh-chan-b", username="chanb")
    insert_guild(db_url, "gd-chan-a", owner_user_id="gh-chan-a")
    insert_guild(db_url, "gd-chan-b", owner_user_id="gh-chan-b")

    resp = test_client.post(
        "/api/guilds/gd-chan-a/discord/channels",
        json={"discord_guild_id": "dg-shared", "discord_channel_id": "dc-shared"},
        headers={"Authorization": f"Bearer {owner_a}"},
    )
    assert resp.status_code == 200
    binding_id = resp.json()["id"]

    resp = test_client.post(
        "/api/guilds/gd-chan-b/discord/channels",
        json={"discord_guild_id": "dg-shared", "discord_channel_id": "dc-shared"},
        headers={"Authorization": f"Bearer {owner_b}"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == binding_id
    assert resp.json()["ps_guild_id"] == "gd-chan-b"

    with _sync_session(db_url) as session:
        rows = session.query(DiscordChannelGuild).filter_by(discord_channel_id="dc-shared").all()
        assert len(rows) == 1
        assert rows[0].ps_guild_id == "gd-chan-b"


def test_remove_channel_binding_scoped_to_guild(client):
    """A binding created for guild A can't be deleted through guild B's route."""
    test_client, db_url = client
    owner_a = make_auth_token(db_url, user_id="gh-chan-c", username="chanc")
    owner_b = make_auth_token(db_url, user_id="gh-chan-d", username="chand")
    insert_guild(db_url, "gd-chan-c", owner_user_id="gh-chan-c")
    insert_guild(db_url, "gd-chan-d", owner_user_id="gh-chan-d")

    resp = test_client.post(
        "/api/guilds/gd-chan-c/discord/channels",
        json={"discord_guild_id": "dg-c", "discord_channel_id": "dc-c"},
        headers={"Authorization": f"Bearer {owner_a}"},
    )
    binding_id = resp.json()["id"]

    resp = test_client.delete(
        f"/api/guilds/gd-chan-d/discord/channels/{binding_id}",
        headers={"Authorization": f"Bearer {owner_b}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Bots
# ---------------------------------------------------------------------------


def test_list_bots_scoped_to_guild(client):
    test_client, db_url = client
    owner_token = make_auth_token(db_url, user_id="gh-bot-owner", username="botowner")
    insert_guild(db_url, "gd-bot-1", owner_user_id="gh-bot-owner")
    _insert_bot_user(db_url, "gd-bot-1", "discordbot:111", "gh-bot-owner", "dc-chan-bot")

    resp = test_client.get(
        "/api/guilds/gd-bot-1/discord/bots",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    bots = resp.json()
    assert len(bots) == 1
    assert bots[0]["user_id"] == "discordbot:111"
    assert bots[0]["discord_channel_id"] == "dc-chan-bot"
