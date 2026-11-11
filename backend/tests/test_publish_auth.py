"""Publication credentials are App-only, task-bound, and repository-scoped."""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from helpers import _sync_session, insert_guild, make_auth_token
from models import Guild, Task, User
from routes import auth
from sqlmodel import select


@pytest.fixture
def publication(client, monkeypatch):
    test_client, db_url = client
    slug = "g-publish"
    insert_guild(db_url, slug)
    worker = test_client.post(f"/guilds/{slug}/workers", json={"repos": ["owner/repo"]}).json()
    with _sync_session(db_url) as db:
        guild = db.scalar(select(Guild).where(Guild.slug == slug))
        guild.github_app_installation_id = "123"
        db.add(guild)
        db.merge(
            Task(
                id="t-publish",
                guild_id=guild.id,
                worker_id=worker["id"],
                description="publish",
                state="working",
                created_at=datetime.now(UTC),
            )
        )
        db.commit()
    mint = Mock(
        return_value={
            "access_token": "scoped-app-token",
            "username": "app[bot]",
            "expires_at": "2099-01-01T00:00:00Z",
        }
    )
    monkeypatch.setattr(auth, "get_app_publish_credentials", mint)
    return test_client, db_url, worker, mint


def request(publication, **params):
    client, _, worker, _ = publication
    return client.get(
        "/auth/github/publish-token",
        params={"guild_id": "g-publish", "task_id": "t-publish", "repo": "owner/repo", **params},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )


def test_publication_credentials_are_scoped_and_not_cacheable(publication):
    response = request(publication)
    assert response.status_code == 200, response.text
    assert response.json()["access_token"] == "scoped-app-token"
    assert response.headers["cache-control"] == "no-store"
    publication[3].assert_called_once_with("owner/repo", "123")


@pytest.mark.parametrize(
    "params,status",
    [({"repo": "other/repo"}, 403), ({"task_id": "missing"}, 404), ({"repo": "../repo"}, 422)],
)
def test_publication_rejects_unbound_targets(publication, params, status):
    assert request(publication, **params).status_code == status
    publication[3].assert_not_called()


def test_publication_rejects_other_worker(publication):
    client, _, _, mint = publication
    worker = client.post("/guilds/g-publish/workers", json={"repos": ["owner/repo"]}).json()
    response = client.get(
        "/auth/github/publish-token",
        params={"guild_id": "g-publish", "task_id": "t-publish", "repo": "owner/repo"},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert response.status_code == 403
    mint.assert_not_called()


@pytest.mark.parametrize("field,value,status", [("state", "done", 409), ("phase", "review", 403)])
def test_publication_rejects_terminal_or_review_task(publication, field, value, status):
    _, db_url, _, mint = publication
    with _sync_session(db_url) as db:
        task = db.get(Task, "t-publish")
        setattr(task, field, value)
        db.add(task)
        db.commit()
    assert request(publication).status_code == status
    mint.assert_not_called()


def test_publication_rejects_member_credentials(publication):
    client, db_url, _, mint = publication
    token = make_auth_token(db_url)
    response = client.get(
        "/auth/github/publish-token",
        params={"guild_id": "g-publish", "task_id": "t-publish", "repo": "owner/repo"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    mint.assert_not_called()


def test_publication_requires_auth(publication):
    response = publication[0].get(
        "/auth/github/publish-token",
        params={"guild_id": "g-publish", "task_id": "t-publish", "repo": "owner/repo"},
    )
    assert response.status_code == 401
    publication[3].assert_not_called()


def test_publication_rejects_cross_guild_task(publication):
    _, db_url, _, mint = publication
    insert_guild(db_url, "g-publish-other")
    with _sync_session(db_url) as db:
        guild = db.scalar(select(Guild).where(Guild.slug == "g-publish-other"))
        task = db.get(Task, "t-publish")
        task.guild_id = guild.id
        db.add(task)
        db.commit()
    assert request(publication).status_code == 404
    mint.assert_not_called()


def test_publication_requires_explicit_guild_installation(publication):
    _, db_url, _, mint = publication
    with _sync_session(db_url) as db:
        guild = db.scalar(select(Guild).where(Guild.slug == "g-publish"))
        guild.github_app_installation_id = None
        db.add(guild)
        db.commit()
    assert request(publication).status_code == 503
    mint.assert_not_called()


def test_publication_never_falls_back_to_personal_token(publication):
    publication[3].side_effect = ValueError("App is not configured")
    response = request(publication)
    assert response.status_code == 503
    assert "App" in response.json()["detail"]


def test_task_identity_returns_author_and_committer(publication, monkeypatch):
    client, db_url, worker, _ = publication
    monkeypatch.setattr(auth, "get_app_bot_identity", lambda: ("app[bot]", "9+app@x"))
    with _sync_session(db_url) as db:
        db.add(
            User(
                id="42",
                github_id="42",
                github_login="ada",
                display_name="Ada L",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        task = db.get(Task, "t-publish")
        task.user_id = "42"
        db.add(task)
        db.commit()
    r = client.get(
        "/auth/github/task-identity",
        params={"guild_id": "g-publish", "task_id": "t-publish"},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "author": {"name": "Ada L", "email": "42+ada@users.noreply.github.com"},
        "committer": {"name": "app[bot]", "email": "9+app@x"},
    }
