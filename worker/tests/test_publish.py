"""The publisher uses real Git objects and a simulated GitHub API, never git push."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from pioneer_worker import publish


def git(root, *args, data=None, env=None):
    return (
        subprocess.run(
            ["git", *args], cwd=root, input=data, capture_output=True, check=True, env=env
        )
        .stdout.decode()
        .strip()
    )


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git(tmp_path, "init", "-b", "task")
    git(tmp_path, "config", "user.name", "Local Agent")
    git(tmp_path, "config", "user.email", "agent@example.invalid")
    (tmp_path / "original.txt").write_text("original\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    git(tmp_path, "remote", "add", "origin", "https://github.com/owner/repo.git")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", base)
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return tmp_path, base


class GitHub:
    def __init__(self, root, base):
        self.root, self.base = root, base
        self.remote = None
        self.verified = True
        self.wrong_tree = False
        self.race = False
        self.timeout_after_update = False
        self.calls = []
        self.commits = {}
        self.on_verify = None
        self.credentials_status = 200

    def request(self, request):
        import os

        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, body))
        status = 200
        if path == "/auth/github/publish-token":
            status = self.credentials_status
            assert request.headers["Authorization"] == "Bearer worker-secret"
            assert request.url.params["task_id"] == "t-test"
            result = {"access_token": "installation-secret", "username": "app[bot]"}
        else:
            assert request.headers["Authorization"] == "Bearer installation-secret"
            if path == "/repos/owner/repo":
                result = {"node_id": "REPO_NODE", "default_branch": "main"}
            elif path == "/repos/owner/repo/git/ref/heads/task":
                status = 200 if self.remote else 404
                result = {"object": {"sha": self.remote}} if self.remote else {}
            elif path.endswith("/git/blobs"):
                oid = git(
                    self.root,
                    "hash-object",
                    "-w",
                    "--stdin",
                    data=base64.b64decode(body["content"]),
                )
                status, result = 201, {"sha": oid}
            elif path.endswith("/git/trees"):
                index = self.root / ".git" / "server-index"
                env = {**os.environ, "GIT_INDEX_FILE": str(index)}
                git(self.root, "read-tree", body["base_tree"], env=env)
                for entry in body["tree"]:
                    if entry["sha"] is None:
                        git(self.root, "update-index", "--force-remove", entry["path"], env=env)
                    else:
                        git(
                            self.root,
                            "update-index",
                            "--add",
                            "--cacheinfo",
                            f"{entry['mode']},{entry['sha']},{entry['path']}",
                            env=env,
                        )
                tree = git(self.root, "write-tree", env=env)
                status, result = 201, {"sha": self.base if self.wrong_tree else tree}
            elif path.endswith("/git/commits"):
                assert set(body) == {"tree", "parents", "message"}
                # A real object with different identity from the local commit.
                raw = (
                    f"tree {body['tree']}\nparent {body['parents'][0]}\n"
                    "author app[bot] <bot@example.invalid> 1700000000 +0000\n"
                    "committer GitHub <noreply@github.com> 1700000000 +0000\n\n"
                    + body["message"]
                    + "\n"
                )
                oid = git(
                    self.root, "hash-object", "-w", "-t", "commit", "--stdin", data=raw.encode()
                )
                result = {
                    "sha": oid,
                    "tree": {"sha": body["tree"]},
                    "verification": {
                        "verified": self.verified,
                        "reason": "valid" if self.verified else "unsigned",
                    },
                }
                self.commits[oid] = {
                    "sha": oid,
                    "author": {"login": "app[bot]"},
                    "commit": {
                        "message": body["message"],
                        "tree": {"sha": body["tree"]},
                        "verification": result["verification"],
                    },
                }
                status = 201
            elif "/commits/" in path:
                if self.on_verify:
                    callback, self.on_verify = self.on_verify, None
                    callback()
                result = self.commits[path.rsplit("/", 1)[1]]
            elif path == "/graphql":
                update = body["variables"]["input"]["refUpdates"][0]
                assert update["name"] == "refs/heads/task"
                assert update["force"] is False
                assert update["beforeOid"] == (self.remote or "0" * 40)
                if self.race:
                    result = {"errors": [{"message": "Reference moved", "type": "STALE_DATA"}]}
                else:
                    self.remote = update["afterOid"]
                    result = {"data": {"updateRefs": {"clientMutationId": None}}}
                    if self.timeout_after_update:
                        self.timeout_after_update = False
                        raise httpx.ReadTimeout("ambiguous update", request=request)
            else:
                raise AssertionError((request.method, path))
        return httpx.Response(status, json=result)


@pytest.fixture
def publisher(workspace, monkeypatch):
    root, base = workspace
    server = GitHub(root, base)
    client = httpx.AsyncClient
    monkeypatch.setattr(
        publish.httpx,
        "AsyncClient",
        lambda **kw: client(transport=httpx.MockTransport(server.request), **kw),
    )
    real_git = publish._git

    async def local_git(args, **kw):
        assert args[0] != "push"
        if args[0] == "fetch":
            args = ["fetch", "--no-tags", str(server.root), args[-1]]
        return await real_git(args, **kw)

    monkeypatch.setattr(publish, "_git", local_git)
    context = publish.PublishContext(
        "https://backend.invalid", "guild", "t-test", "worker-secret", "task"
    )
    return root, server, context


async def test_publishes_exact_snapshot_and_continues(publisher):
    root, server, context = publisher
    (root / "original.txt").unlink()
    (root / "binary.bin").write_bytes(bytes(range(256)))
    script = root / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    (root / "link").symlink_to("run.sh")
    git(root, "clone", "--quiet", "--no-hardlinks", ".", "submodule")
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{server.base},submodule")
    result = await publish.publish_checkpoint(root, context, "first checkpoint")
    assert result == "pushed"
    assert git(root, "rev-parse", "HEAD") == server.remote
    assert git(root, "status", "--porcelain") == ""
    modes = git(root, "ls-tree", "HEAD")
    assert "100755 blob" in modes and "120000 blob" in modes and "160000 commit" in modes
    assert await publish.publish_checkpoint(root, context, "no changes") == "nothing"
    (root / "binary.bin").write_bytes(b"updated\x00")
    assert await publish.publish_checkpoint(root, context, "second checkpoint") == "pushed"
    assert git(root, "status", "--porcelain") == ""
    assert len(server.commits) == 2


async def test_existing_local_commits_become_one_checkpoint(publisher):
    root, server, context = publisher
    for number in range(2):
        (root / "original.txt").write_text(str(number))
        git(root, "add", ".")
        git(root, "commit", "-m", f"local {number}")
    old = git(root, "rev-parse", "HEAD")
    await publish.publish_checkpoint(root, context, "combined checkpoint")
    assert git(root, "rev-parse", "HEAD^") == server.base
    assert git(root, "reflog", "show", "--format=%H", "HEAD").splitlines()[1] == old


@pytest.mark.parametrize("failure", ["verified", "wrong_tree", "race"])
async def test_rejects_invalid_publication_without_changing_local_head(publisher, failure):
    root, server, context = publisher
    (root / "original.txt").write_text("changed")
    setattr(server, failure, False if failure == "verified" else True)
    with pytest.raises(publish.PublishError):
        await publish.publish_checkpoint(root, context, "must fail")
    assert git(root, "rev-parse", "HEAD") == server.base
    assert (root / "original.txt").read_text() == "changed"
    assert server.remote is None


async def test_recovers_ambiguous_ref_update_without_duplicate_commit(publisher):
    root, server, context = publisher
    (root / "original.txt").write_text("changed")
    server.timeout_after_update = True
    with pytest.raises(publish.PublishError):
        await publish.publish_checkpoint(root, context, "checkpoint")
    assert server.remote is not None
    assert git(root, "rev-parse", "HEAD") == server.base
    assert await publish.publish_checkpoint(root, context, "retry") == "pushed"
    assert git(root, "rev-parse", "HEAD") == server.remote
    assert len(server.commits) == 1


async def test_remote_divergence_is_not_overwritten(publisher):
    root, server, context = publisher
    (root / "original.txt").write_text("remote content")
    await publish.publish_checkpoint(root, context, "remote")
    git(root, "update-ref", "HEAD", server.base)
    (root / "original.txt").write_text("different local content")
    with pytest.raises(publish.PublishError, match="diverged"):
        await publish.publish_checkpoint(root, context, "must not overwrite")
    assert len(server.commits) == 1


async def test_lfs_pointer_fails_before_publication(publisher):
    root, server, context = publisher
    (root / "large.bin").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10\n"
    )
    with pytest.raises(publish.PublishError, match="LFS"):
        await publish.publish_checkpoint(root, context, "unsupported")
    assert not server.commits


async def test_followup_from_another_clone_uses_canonical_history(publisher, tmp_path):
    root, server, context = publisher
    (root / "original.txt").write_text("first worker")
    await publish.publish_checkpoint(root, context, "first worker")
    first = server.remote
    other = tmp_path / "other-worker"
    git(root, "clone", "--quiet", "--no-hardlinks", str(root), str(other))
    git(other, "remote", "set-url", "origin", "https://github.com/owner/repo.git")
    (other / "original.txt").write_text("second worker")
    await publish.publish_checkpoint(other, context, "followup")
    assert git(other, "rev-parse", "HEAD^") == first
    assert git(root, "rev-parse", "HEAD") == first
    assert git(other, "status", "--porcelain") == ""


async def test_detached_linked_worktree_does_not_move_another_slots_branch(publisher, tmp_path):
    root, server, context = publisher
    (root / "original.txt").write_text("first checkpoint")
    await publish.publish_checkpoint(root, context, "first")
    first = server.remote
    other = tmp_path / "detached"
    git(root, "worktree", "add", "--detach", str(other), first)
    (other / "original.txt").write_text("detached followup")
    await publish.publish_checkpoint(other, context, "followup")
    assert git(other, "rev-parse", "HEAD") == server.remote
    assert git(other, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert git(root, "rev-parse", "HEAD") == first


async def test_edits_during_publication_are_preserved(publisher):
    root, server, context = publisher
    (root / "original.txt").write_text("snapshot")
    server.on_verify = lambda: (root / "original.txt").write_text("later edit")
    await publish.publish_checkpoint(root, context, "checkpoint")
    assert git(root, "show", "HEAD:original.txt") == "snapshot"
    assert (root / "original.txt").read_text() == "later edit"
    assert "original.txt" in git(root, "status", "--porcelain")


async def test_default_branch_is_never_a_task_publication_target(publisher):
    from dataclasses import replace

    root, server, context = publisher
    git(root, "checkout", "-b", "main")
    with pytest.raises(publish.PublishError, match="default branch"):
        await publish.publish_checkpoint(root, replace(context, branch="main"), "forbidden")
    assert not server.commits


async def test_simultaneous_publication_in_one_worktree_is_rejected(publisher):
    import fcntl

    root, server, context = publisher
    with (root / ".git" / "pioneer-publish.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(publish.PublishError, match="Another publication"):
            await publish.publish_checkpoint(root, context, "concurrent")
    assert not server.calls


@pytest.mark.parametrize("outage", ["missing-config", "network"])
async def test_unavailable_signing_logs_and_publishes_unsigned(
    publisher, monkeypatch, caplog, outage
):
    root, server, context = publisher
    (root / "original.txt").write_text("unsigned progress")
    if outage == "missing-config":
        server.credentials_status = 503
    else:

        def unavailable(request):
            raise httpx.ConnectError("unavailable", request=request)

        monkeypatch.setattr(server, "request", unavailable)
    real_git = publish._git
    pushes = []

    async def git_with_push(args, **kwargs):
        if args[0] == "push":
            pushes.append((args, kwargs.get("token")))
            return b""
        return await real_git(args, **kwargs)

    monkeypatch.setattr(publish, "_git", git_with_push)
    assert (
        await publish.publish_checkpoint(root, context, "progress", fallback_token="human-token")
        == "pushed"
    )
    assert "unsigned" in caplog.text
    assert not server.commits
    assert pushes == [
        (["push", "https://github.com/owner/repo.git", "HEAD:refs/heads/task"], "human-token")
    ]
    assert "gpgsig " not in git(root, "cat-file", "commit", "HEAD")
    assert git(root, "status", "--porcelain") == ""


async def test_unsigned_fallback_pushes_real_git_and_needs_no_preconfigured_identity(
    publisher, monkeypatch, tmp_path_factory
):
    root, server, context = publisher
    server.credentials_status = 503
    git(root, "config", "--unset", "user.name")
    git(root, "config", "--unset", "user.email")
    remote = tmp_path_factory.mktemp("unsigned-remote") / "repo.git"
    git(root, "init", "--bare", str(remote))
    (root / "original.txt").write_text("unsigned checkpoint")
    real_git = publish._git

    async def local_push(args, **kwargs):
        if args[0] == "push":
            return git(kwargs["cwd"], "push", str(remote), args[-1]).encode()
        return await real_git(args, **kwargs)

    monkeypatch.setattr(publish, "_git", local_push)
    assert await publish.publish_checkpoint(root, context, "unsigned") == "pushed"
    assert git(remote, "rev-parse", "refs/heads/task") == git(root, "rev-parse", "HEAD")
    assert (
        git(root, "show", "-s", "--format=%an <%ae>") == "Pioneer Square <pioneer-square@localhost>"
    )
    assert await publish.publish_checkpoint(root, context, "no duplicate") == "nothing"
    assert git(root, "status", "--porcelain") == ""


async def test_unavailable_signing_with_no_changes_does_not_create_empty_branch(publisher):
    root, server, context = publisher
    server.credentials_status = 503
    assert await publish.publish_checkpoint(root, context, "no changes") == "nothing"
    assert not server.commits


async def test_credential_authorization_errors_do_not_fall_back(publisher):
    root, server, context = publisher
    server.credentials_status = 403
    (root / "original.txt").write_text("must not publish")
    with pytest.raises(publish.PublishError):
        await publish.publish_checkpoint(root, context, "unauthorized")
    assert git(root, "rev-parse", "HEAD") == server.base


async def test_preflight_uses_only_backend_credentials_endpoint(publisher):
    root, server, context = publisher
    await publish.check_access(root, context)
    assert [path for _, path, _ in server.calls] == ["/auth/github/publish-token"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"detail": "Unavailable"}),
        httpx.Response(302, headers={"location": "https://other.invalid"}),
        httpx.Response(200, text="not JSON"),
        httpx.Response(200, json=[]),
    ],
)
async def test_invalid_service_responses_fail_closed(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(publish.PublishError):
            await publish._json(client, "GET", "https://backend.invalid")


async def test_preflight_rejects_personal_credentials(publisher, monkeypatch):
    root, server, context = publisher
    monkeypatch.setattr(
        server,
        "request",
        lambda _: httpx.Response(200, json={"access_token": "personal", "username": "human"}),
    )
    with pytest.raises(publish.PublishError, match="App credentials"):
        await publish.check_access(root, context)


async def test_preflight_connection_failure_is_actionable(publisher, monkeypatch):
    root, server, context = publisher

    def unavailable(request):
        raise httpx.ConnectError("unavailable", request=request)

    monkeypatch.setattr(server, "request", unavailable)
    with pytest.raises(publish.PublishError, match="Cannot reach"):
        await publish.check_access(root, context)


def test_task_environment_roundtrip_hides_secret(monkeypatch):
    context = publish.PublishContext("wss://backend.invalid", "g", "t", "worker-secret", "branch")
    for key, value in context.environment().items():
        monkeypatch.setenv(key, value)
    assert publish.PublishContext.from_env().backend_url == "https://backend.invalid"
    assert "worker-secret" not in repr(context)


def test_unconfigured_cli_fails_without_network(monkeypatch, capsys):
    for key in [
        "PIONEER_BACKEND_URL",
        "PIONEER_GUILD_ID",
        "PIONEER_TASK_ID",
        "PIONEER_AUTH_TOKEN",
        "PIONEER_PUBLISH_BRANCH",
    ]:
        monkeypatch.delenv(key, raising=False)
    assert publish.main(["-m", "checkpoint"]) == 1
    assert "PIONEER_" in capsys.readouterr().err
