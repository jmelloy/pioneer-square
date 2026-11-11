"""Author (triggering user) / committer (bot) identity plumbing for publish."""

from pioneer_worker import publish


def _ctx(**kw):
    return publish.PublishContext("http://b", "g", "t", "tok", "br", **kw)


def test_commit_env_sets_author_and_committer():
    env = publish.commit_env(
        _ctx(
            author=("Ada", "ada@example.com"),
            committer=("app[bot]", "1+app@users.noreply.github.com"),
        )
    )
    assert env == {
        "GIT_AUTHOR_NAME": "Ada",
        "GIT_AUTHOR_EMAIL": "ada@example.com",
        "GIT_COMMITTER_NAME": "app[bot]",
        "GIT_COMMITTER_EMAIL": "1+app@users.noreply.github.com",
    }


def test_commit_env_empty_without_identity():
    assert publish.commit_env(_ctx()) == {}


def test_identity_round_trips_through_environment(monkeypatch):
    ctx = _ctx(author=("Ada", "a@x"), committer=("bot", "b@x"))
    env = ctx.environment()
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    restored = publish.PublishContext.from_env()
    assert restored.author == ("Ada", "a@x")
    assert restored.committer == ("bot", "b@x")
