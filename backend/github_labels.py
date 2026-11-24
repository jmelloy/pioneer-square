"""Pioneer Square ``ps:`` issue lifecycle labels (#1491).

One label per lifecycle state, grouped by phase. ``ps:ready`` replaces the old
``devReady`` label; the legacy aliases are still honored so in-flight issues keep
getting picked up, and claiming one swaps it for ``ps:working`` (the migration path).
"""

import logging
import urllib.error
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# name -> (color, description). Colors are per-phase so the board reads at a glance.
PS_LABELS: dict[str, tuple[str, str]] = {
    "ps:needsTriage": ("d4c5f9", "Issue requires initial assessment"),
    "ps:triaging": ("d4c5f9", "Triage in progress"),
    "ps:triageComplete": ("d4c5f9", "Triage done, ready for planning"),
    "ps:needsPlan": ("c5def5", "Requires implementation plan/spec"),
    "ps:planning": ("c5def5", "Plan being written"),
    "ps:planComplete": ("c5def5", "Plan approved, ready for dev"),
    "ps:ready": ("0e8a16", "Ready for implementation (replaces devReady)"),
    "ps:working": ("fbca04", "Actively being implemented"),
    "ps:complete": ("1d76db", "Implementation done"),
    "ps:needsReview": ("f9d0c4", "Awaiting code review"),
    "ps:reviewing": ("f9d0c4", "Review in progress"),
    "ps:reviewComplete": ("f9d0c4", "Review passed, ready to merge"),
}

PS_READY = "ps:ready"
PS_WORKING = "ps:working"

# Deprecated devReady-family aliases, still treated as ps:ready during migration.
LEGACY_READY_LABELS = ("devReady", "dev-ready", "ready-for-dev", "ready")

# Search order for the periodic sweep: canonical first.
READY_LABELS = (PS_READY, *LEGACY_READY_LABELS)
_READY_LOWER = frozenset(name.lower() for name in READY_LABELS)


def is_ready_label(name: str | None) -> bool:
    """True for ps:ready or any legacy devReady alias (case-insensitive, like GitHub)."""
    return name is not None and name.lower() in _READY_LOWER


# ponytail: per-process memo, so each repo is checked once per backend restart.
_ensured_repos: set[str] = set()


def ensure_ps_labels(repo: str, token: str, post: Callable[..., Any]) -> None:
    """Create any missing ps: labels on ``repo``. Sync; run via ``_to_thread``.

    ``post`` is ``foreman.tools._gh_api_post`` (passed in to avoid an import cycle).
    GitHub answers 422 for an existing label, which is the common case and ignored.
    """
    if repo in _ensured_repos:
        return
    for name, (color, description) in PS_LABELS.items():
        try:
            post(
                f"/repos/{repo}/labels",
                token,
                {"name": name, "color": color, "description": description},
            )
        except urllib.error.HTTPError as exc:
            if exc.code != 422:
                raise
    _ensured_repos.add(repo)
