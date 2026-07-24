"""Unit tests for pioneer_worker.log_format."""

from __future__ import annotations

from pioneer_worker.log_format import strip_worktree_prefix


def test_strip_worktree_prefix_strips_host_path():
    path = "/Users/jeffrey.melloy/pioneer-square/ps-worker/work/dnsid/w-4de676/t-vdfpj3/dnsid-infra/envs/dev/main.tf"
    assert strip_worktree_prefix(path) == "dnsid-infra/envs/dev/main.tf"


def test_strip_worktree_prefix_repo_root():
    path = "/work/dnsid/w-4de676/t-vdfpj3/dnsid-infra"
    assert strip_worktree_prefix(path) == "dnsid-infra"


def test_strip_worktree_prefix_no_marker_unchanged():
    assert strip_worktree_prefix("/tmp/foo.py") == "/tmp/foo.py"


def test_strip_worktree_prefix_relative_path_unchanged():
    assert strip_worktree_prefix("dnsid-infra/envs/dev/main.tf") == "dnsid-infra/envs/dev/main.tf"


def test_strip_worktree_prefix_empty_string():
    assert strip_worktree_prefix("") == ""


def test_strip_worktree_prefix_uses_last_marker():
    # Defensive: if a repo's own path happens to contain a similar-looking
    # segment, strip up to the last (i.e. innermost/real) task marker.
    path = "/work/t-aaaaaa/nested/t-bbbbbb/myrepo/main.py"
    assert strip_worktree_prefix(path) == "myrepo/main.py"
