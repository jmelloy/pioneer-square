"""Tests for backend/foreman/github_url_parser.py."""

import sys
from pathlib import Path

# Ensure the backend source is importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from foreman.github_url_parser import GitHubReference, annotate_message, parse_github_urls


class TestParseGitHubUrls:
    def test_pull_request_url(self):
        text = "Check this PR: https://github.com/acme/widgets/pull/42"
        refs = parse_github_urls(text)
        assert len(refs) == 1
        assert refs[0] == GitHubReference(owner="acme", repo="widgets", number=42, ref_type="pull")
        assert refs[0].slug == "acme/widgets"
        assert refs[0].kind == "PR"

    def test_issue_url(self):
        text = "See https://github.com/owner/repo/issues/456"
        refs = parse_github_urls(text)
        assert len(refs) == 1
        assert refs[0].owner == "owner"
        assert refs[0].repo == "repo"
        assert refs[0].number == 456
        assert refs[0].ref_type == "issues"
        assert refs[0].kind == "Issue"

    def test_trailing_slash(self):
        text = "https://github.com/o/r/pull/1/"
        refs = parse_github_urls(text)
        assert len(refs) == 1
        assert refs[0].number == 1

    def test_query_string(self):
        text = "https://github.com/o/r/issues/99?foo=bar&baz=1"
        refs = parse_github_urls(text)
        assert len(refs) == 1
        assert refs[0].number == 99

    def test_fragment(self):
        text = "https://github.com/o/r/pull/7#issuecomment-12345"
        refs = parse_github_urls(text)
        assert len(refs) == 1
        assert refs[0].number == 7

    def test_query_and_fragment(self):
        text = "https://github.com/o/r/issues/5?q=1#fragment"
        refs = parse_github_urls(text)
        assert len(refs) == 1
        assert refs[0].number == 5

    def test_pr_subpath(self):
        """PR URLs with /files or /commits sub-paths."""
        text = "https://github.com/o/r/pull/10/files"
        refs = parse_github_urls(text)
        assert len(refs) == 1
        assert refs[0].number == 10

    def test_multiple_urls(self):
        text = "Look at https://github.com/a/b/pull/1 and https://github.com/c/d/issues/2"
        refs = parse_github_urls(text)
        assert len(refs) == 2
        assert refs[0].slug == "a/b"
        assert refs[1].slug == "c/d"

    def test_duplicate_urls_deduplicated(self):
        text = "https://github.com/o/r/pull/5 https://github.com/o/r/pull/5"
        refs = parse_github_urls(text)
        assert len(refs) == 1

    def test_no_github_url(self):
        text = "Just a normal message with no URLs"
        refs = parse_github_urls(text)
        assert refs == []

    def test_non_github_url_ignored(self):
        text = "See https://gitlab.com/owner/repo/issues/1"
        refs = parse_github_urls(text)
        assert refs == []

    def test_malformed_url_no_number(self):
        text = "https://github.com/o/r/pull/"
        refs = parse_github_urls(text)
        assert refs == []

    def test_malformed_url_not_issue_or_pull(self):
        text = "https://github.com/o/r/tree/main"
        refs = parse_github_urls(text)
        assert refs == []

    def test_empty_string(self):
        assert parse_github_urls("") == []

    def test_owner_with_dots_and_hyphens(self):
        text = "https://github.com/my-org.name/repo-name.js/issues/100"
        refs = parse_github_urls(text)
        assert len(refs) == 1
        assert refs[0].owner == "my-org.name"
        assert refs[0].repo == "repo-name.js"


class TestAnnotateMessage:
    def test_no_urls_unchanged(self):
        msg = "Hello foreman, please help"
        assert annotate_message(msg) == msg

    def test_pr_annotation(self):
        msg = "Please review https://github.com/acme/app/pull/42"
        result = annotate_message(msg)
        assert "Parsed GitHub references:" in result
        assert "PR #42 in acme/app" in result
        # Original message preserved
        assert msg in result

    def test_issue_annotation(self):
        msg = "Fix https://github.com/org/lib/issues/7"
        result = annotate_message(msg)
        assert "Issue #7 in org/lib" in result

    def test_multiple_refs_annotated(self):
        msg = "https://github.com/a/b/pull/1 https://github.com/c/d/issues/2"
        result = annotate_message(msg)
        assert "PR #1 in a/b" in result
        assert "Issue #2 in c/d" in result

    def test_str_representation(self):
        ref = GitHubReference(owner="o", repo="r", number=5, ref_type="pull")
        assert str(ref) == "[GitHub PR #5 in o/r]"

        ref2 = GitHubReference(owner="o", repo="r", number=3, ref_type="issues")
        assert str(ref2) == "[GitHub Issue #3 in o/r]"
