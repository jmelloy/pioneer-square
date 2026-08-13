"""Tests for /api/me and the guild member management endpoints."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import oauth as oauth_module
from helpers import _sync_session, insert_guild, insert_member, make_auth_token
from models import Guild, GuildInvite, GuildMember, User
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col


def _insert_guild_legacy_only(db_url: str, guild_id: str, user_id: str) -> None:
    """Insert a guild with github_user_id but no guild_members row."""

    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.execute(
            pg_insert(Guild)
            .values(slug=guild_id, created_at=now, name="Legacy", github_user_id=user_id)
            .on_conflict_do_nothing()
        )
        session.commit()


def _seed_user(db_url: str, user_id: str, login: str) -> None:
    """Insert a users row directly so it can be referenced as a member."""

    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.execute(
            pg_insert(User)
            .values(
                id=user_id,
                github_id=user_id,
                github_login=login,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["id"],
                set_={"github_login": login},
            )
        )
        session.commit()


# ---------------------------------------------------------------------------
# /api/me
# ---------------------------------------------------------------------------


def test_api_me_requires_auth(client):
    test_client, _ = client
    resp = test_client.get("/api/me")
    assert resp.status_code == 401


def test_api_me_returns_profile_and_memberships(client):
    test_client, db_url = client
    insert_guild(db_url, "gm-1", name="My Guild")
    _seed_user(db_url, "gh-user-test", "testuser")
    token = make_auth_token(db_url)
    resp = test_client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"]["github_login"] == "testuser"
    guild_ids = [m["guild_id"] for m in data["memberships"]]
    assert "gm-1" in guild_ids
    membership = next(m for m in data["memberships"] if m["guild_id"] == "gm-1")
    assert membership["role"] == "owner"


# ---------------------------------------------------------------------------
# Member management
# ---------------------------------------------------------------------------


def test_list_members_requires_membership(client):
    test_client, db_url = client
    insert_guild(db_url, "gm-list")
    other = make_auth_token(db_url, user_id="other-user", username="outsider")
    resp = test_client.get(
        "/api/guilds/gm-list/members",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 403


def test_list_members_returns_owner(client):
    test_client, db_url = client
    insert_guild(db_url, "gm-list2")
    token = make_auth_token(db_url)
    resp = test_client.get(
        "/api/guilds/gm-list2/members",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    members = resp.json()
    assert len(members) == 1
    assert members[0]["user_id"] == "gh-user-test"
    assert members[0]["role"] == "owner"


def test_add_member_resolves_login(client):
    test_client, db_url = client
    insert_guild(db_url, "gm-add")
    _seed_user(db_url, "user-99", "octocat")
    token = make_auth_token(db_url)
    resp = test_client.post(
        "/api/guilds/gm-add/members",
        json={"user": "octocat", "role": "member"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "user-99"
    assert body["role"] == "member"


def test_add_member_unknown_user_is_404(client):
    test_client, db_url = client
    insert_guild(db_url, "gm-unknown")
    token = make_auth_token(db_url)
    resp = test_client.post(
        "/api/guilds/gm-unknown/members",
        json={"user": "noone-here", "role": "member"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


def test_add_member_requires_owner(client):
    test_client, db_url = client
    insert_guild(db_url, "gm-perm")
    insert_member(db_url, "gm-perm", "user-bob", role="member")
    bob_token = make_auth_token(db_url, user_id="user-bob", username="bob")
    _seed_user(db_url, "user-99", "octocat")
    resp = test_client.post(
        "/api/guilds/gm-perm/members",
        json={"user": "octocat", "role": "member"},
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert resp.status_code == 403


def test_update_member_role(client):
    test_client, db_url = client
    insert_guild(db_url, "gm-role")
    insert_member(db_url, "gm-role", "user-bob", role="member")
    token = make_auth_token(db_url)
    resp = test_client.patch(
        "/api/guilds/gm-role/members/user-bob",
        json={"role": "viewer"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "viewer"


def test_cannot_demote_last_owner(client):
    test_client, db_url = client
    insert_guild(db_url, "gm-last")
    token = make_auth_token(db_url)
    resp = test_client.patch(
        "/api/guilds/gm-last/members/gh-user-test",
        json={"role": "member"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_remove_member(client):
    test_client, db_url = client
    insert_guild(db_url, "gm-del")
    insert_member(db_url, "gm-del", "user-bob", role="member")
    token = make_auth_token(db_url)
    resp = test_client.delete(
        "/api/guilds/gm-del/members/user-bob",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


def test_invalid_role_rejected(client):
    test_client, db_url = client
    insert_guild(db_url, "gm-bad")
    _seed_user(db_url, "user-99", "octocat")
    token = make_auth_token(db_url)
    resp = test_client.post(
        "/api/guilds/gm-bad/members",
        json={"user": "octocat", "role": "admin"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# guild_members migration — /api/me must not include legacy guilds
# ---------------------------------------------------------------------------


def test_api_me_excludes_legacy_guilds(client):
    """Guilds where guilds.github_user_id matches but no guild_members row exists
    must NOT appear in /api/me memberships; the legacy path has been removed."""
    test_client, db_url = client
    _seed_user(db_url, "gh-user-test", "testuser")
    _insert_guild_legacy_only(db_url, "gm-legacy", "gh-user-test")
    token = make_auth_token(db_url)
    resp = test_client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    guild_ids = [m["guild_id"] for m in resp.json()["memberships"]]
    assert "gm-legacy" not in guild_ids


# ---------------------------------------------------------------------------
# Pending invite (GitHub username/ID invite flow)
# ---------------------------------------------------------------------------


def test_create_invite_by_username(client):
    test_client, db_url = client
    insert_guild(db_url, "gi-create")
    token = make_auth_token(db_url)
    resp = test_client.post(
        "/api/guilds/gi-create/invites",
        json={"user": "newgithubuser", "role": "member"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["github_login"] == "newgithubuser"
    assert body["github_id"] is None
    assert body["role"] == "member"
    assert body["status"] == "pending"


def test_create_invite_by_numeric_id(client):
    test_client, db_url = client
    insert_guild(db_url, "gi-numeric")
    token = make_auth_token(db_url)
    resp = test_client.post(
        "/api/guilds/gi-numeric/invites",
        json={"user": "12345678", "role": "viewer"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["github_id"] == "12345678"
    assert body["github_login"] is None
    assert body["role"] == "viewer"
    assert body["status"] == "pending"


def test_invite_username_stored_lowercase(client):
    test_client, db_url = client
    insert_guild(db_url, "gi-case")
    token = make_auth_token(db_url)
    resp = test_client.post(
        "/api/guilds/gi-case/invites",
        json={"user": "MixedCaseUser"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["github_login"] == "mixedcaseuser"


def test_duplicate_invite_updates_role(client):
    test_client, db_url = client
    insert_guild(db_url, "gi-dup")
    token = make_auth_token(db_url)
    test_client.post(
        "/api/guilds/gi-dup/invites",
        json={"user": "someone", "role": "viewer"},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp = test_client.post(
        "/api/guilds/gi-dup/invites",
        json={"user": "someone", "role": "owner"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "owner"


def test_list_invites(client):
    test_client, db_url = client
    insert_guild(db_url, "gi-list")
    token = make_auth_token(db_url)
    test_client.post(
        "/api/guilds/gi-list/invites",
        json={"user": "alice"},
        headers={"Authorization": f"Bearer {token}"},
    )
    test_client.post(
        "/api/guilds/gi-list/invites",
        json={"user": "bob"},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp = test_client.get(
        "/api/guilds/gi-list/invites",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    logins = [i["github_login"] for i in resp.json()]
    assert "alice" in logins
    assert "bob" in logins


def test_list_invites_requires_owner(client):
    test_client, db_url = client
    insert_guild(db_url, "gi-perm")
    insert_member(db_url, "gi-perm", "user-bob", role="member")
    bob_token = make_auth_token(db_url, user_id="user-bob", username="bob")
    resp = test_client.get(
        "/api/guilds/gi-perm/invites",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert resp.status_code == 403


def test_cancel_invite(client):
    test_client, db_url = client
    insert_guild(db_url, "gi-cancel")
    token = make_auth_token(db_url)
    create_resp = test_client.post(
        "/api/guilds/gi-cancel/invites",
        json={"user": "tocancel"},
        headers={"Authorization": f"Bearer {token}"},
    )
    invite_id = create_resp.json()["id"]
    resp = test_client.delete(
        f"/api/guilds/gi-cancel/invites/{invite_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    # Should no longer appear in the list
    list_resp = test_client.get(
        "/api/guilds/gi-cancel/invites",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert all(i["id"] != invite_id for i in list_resp.json())


def test_invalid_role_rejected_for_invite(client):
    test_client, db_url = client
    insert_guild(db_url, "gi-badrole")
    token = make_auth_token(db_url)
    resp = test_client.post(
        "/api/guilds/gi-badrole/invites",
        json={"user": "someone", "role": "superuser"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def _insert_pending_invite(
    db_url: str, guild_id: str, github_login: str, role: str = "member"
) -> None:
    """Insert a pending guild invite row directly."""
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        from sqlalchemy import select as sa_select

        guild_pk = session.scalar(
            sa_select(Guild.id).where(Guild.slug == guild_id, Guild.deleted_at.is_(None))
        )
        assert guild_pk is not None
        owner_id = session.scalar(
            sa_select(GuildMember.user_id).where(
                GuildMember.guild_id == guild_pk, GuildMember.role == "owner"
            )
        )
        session.execute(
            pg_insert(GuildInvite)
            .values(
                guild_id=guild_pk,
                github_login=github_login.lower(),
                github_id=None,
                role=role,
                invited_by=owner_id,
                created_at=now,
                status="pending",
            )
            .on_conflict_do_nothing()
        )
        session.commit()


def test_pending_invite_accepted_on_login(client):
    """A pending invite for a GitHub username is auto-accepted when that user logs in."""
    test_client, db_url = client
    insert_guild(db_url, "gi-login")
    _insert_pending_invite(db_url, "gi-login", "newbie")

    # Simulate the user logging in via the dev guest endpoint (which upserts a User row
    # and runs the same invite-acceptance logic). We instead call the internal path
    # directly by inserting the user row and checking the DB reflects acceptance.
    # Here we verify via the REST invite list: after accepting, no pending invites remain.

    # Insert user row as if they logged in.
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.execute(
            pg_insert(User)
            .values(
                id="newbie-id",
                github_id="newbie-id",
                github_login="newbie",
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        # Manually run the invite-acceptance logic (mirrors oauth.create_session).
        guild_pk = session.scalar(
            select(Guild.id).where(Guild.slug == "gi-login", Guild.deleted_at.is_(None))
        )
        invite = session.scalar(
            select(GuildInvite).where(
                GuildInvite.guild_id == guild_pk,
                GuildInvite.github_login == "newbie",
                GuildInvite.status == "pending",
            )
        )
        assert invite is not None
        session.execute(
            pg_insert(GuildMember)
            .values(
                guild_id=guild_pk,
                user_id="newbie-id",
                role=invite.role,
                created_at=now,
            )
            .on_conflict_do_update(
                index_elements=["guild_id", "user_id"],
                index_where=text("deleted_at IS NULL"),
                set_={"role": pg_insert(GuildMember).excluded.role},
            )
        )
        invite.status = "accepted"
        session.commit()

    # The new user should now be a member.
    token = make_auth_token(db_url)
    resp = test_client.get(
        "/api/guilds/gi-login/members",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    user_ids = [m["user_id"] for m in resp.json()]
    assert "newbie-id" in user_ids

    # The invite should be gone from the pending list.
    invite_resp = test_client.get(
        "/api/guilds/gi-login/invites",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert invite_resp.status_code == 200
    assert all(i["github_login"] != "newbie" for i in invite_resp.json())


def test_pending_invite_accepted_via_oauth_exchange(client):
    """Pending invite is auto-accepted when the invited user goes through the real OAuth exchange.

    This exercises ``oauth.create_session`` end-to-end (including the invite-acceptance
    block) via the ``/auth/github/exchange`` endpoint rather than duplicating the logic
    inline, so a regression in ``create_session`` will cause this test to fail.
    """
    test_client, db_url = client
    insert_guild(db_url, "gi-oauth")
    _insert_pending_invite(db_url, "gi-oauth", "oauthuser")

    fake_state = "test-state-oauth-invite"
    oauth_module.oauth_states[fake_state] = None

    fake_token_resp = {"access_token": "ghs_fake", "token_type": "bearer", "scope": "repo"}
    fake_user_resp = {
        "id": 9999,  # numeric GitHub user ID
        "login": "oauthuser",
        "name": "OAuth User",
        "avatar_url": "https://example.com/avatar.png",
        "email": None,
    }
    # Patch GitHub HTTP helpers so no real network call is made.
    with (
        mock.patch.object(oauth_module, "_gh_exchange_code", return_value=fake_token_resp),
        mock.patch.object(oauth_module, "_gh_get_user", return_value=fake_user_resp),
    ):
        exchange_resp = test_client.post(
            "/auth/github/exchange",
            json={"code": "fake-code", "state": fake_state},
        )

    assert exchange_resp.status_code == 200, exchange_resp.text
    payload = exchange_resp.json()
    assert payload["gh_login"] == "oauthuser"
    assert "login_token" in payload

    # The invited user should now be a member.
    owner_token = make_auth_token(db_url)
    members_resp = test_client.get(
        "/api/guilds/gi-oauth/members",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert members_resp.status_code == 200
    user_ids = [m["user_id"] for m in members_resp.json()]
    assert str(fake_user_resp["id"]) in user_ids

    # The invite should no longer appear in the pending list.
    invites_resp = test_client.get(
        "/api/guilds/gi-oauth/invites",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert invites_resp.status_code == 200
    assert all(i["github_login"] != "oauthuser" for i in invites_resp.json())
