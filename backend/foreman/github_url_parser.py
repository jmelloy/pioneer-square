"""Parse GitHub URLs from chat messages and annotate them for the Foreman.

When a user pastes a GitHub URL like:
  https://github.com/owner/repo/pull/123
  https://github.com/owner/repo/issues/456

This module extracts the owner/repo slug and issue/PR number so the Foreman
can reference them directly in tool calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches GitHub issue and PR URLs, handling:
# - Trailing slashes
# - Query strings (?foo=bar)
# - Fragment anchors (#issuecomment-123)
# - Optional /files, /commits sub-paths on PRs
_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/"
    r"(?P<owner>[A-Za-z0-9\-_.]+)/"
    r"(?P<repo>[A-Za-z0-9\-_.]+)/"
    r"(?P<type>(?:pull|issues))/"
    r"(?P<number>\d+)"
    r"(?:/[^\s]*)?"  # optional sub-path (e.g. /files, /commits)
    r"(?:\?[^\s#]*)?"  # optional query string
    r"(?:#[^\s]*)?"  # optional fragment
)


@dataclass
class GitHubReference:
    """A parsed GitHub issue or PR reference."""

    owner: str
    repo: str
    number: int
    ref_type: str  # "pull" or "issues"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def kind(self) -> str:
        return "PR" if self.ref_type == "pull" else "Issue"

    def __str__(self) -> str:
        return f"[GitHub {self.kind} #{self.number} in {self.slug}]"


def parse_github_urls(text: str) -> list[GitHubReference]:
    """Extract all GitHub issue/PR references from a text string.

    Returns an empty list if no valid URLs are found. Malformed or
    non-GitHub URLs are silently ignored.
    """
    refs: list[GitHubReference] = []
    seen: set[tuple[str, str, int, str]] = set()

    for match in _GITHUB_URL_RE.finditer(text):
        owner = match.group("owner")
        repo = match.group("repo")
        number = int(match.group("number"))
        ref_type = match.group("type")

        key = (owner, repo, number, ref_type)
        if key not in seen:
            seen.add(key)
            refs.append(GitHubReference(owner=owner, repo=repo, number=number, ref_type=ref_type))

    return refs


def annotate_message(message: str) -> str:
    """Annotate a chat message with parsed GitHub URL metadata.

    If the message contains GitHub issue/PR URLs, appends a structured
    annotation block that the Foreman can reference. If no URLs are found,
    returns the message unchanged.
    """
    refs = parse_github_urls(message)
    if not refs:
        return message

    annotations = []
    for ref in refs:
        annotations.append(
            f"  - {ref.kind} #{ref.number} in {ref.slug} "
            f"(https://github.com/{ref.slug}/{ref.ref_type}/{ref.number})"
        )

    annotation_block = "\n\n---\n[Parsed GitHub references:\n" + "\n".join(annotations) + "\n]"
    return message + annotation_block
