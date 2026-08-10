"""GitHub webhook receiver.

GitHub posts here for every subscribed event on a repo whose webhook is
configured against ``{backend_url}/webhooks/github/{guild_id}``. The handler
verifies the HMAC-SHA256 signature against the guild's stored secret,
persists the event (idempotent on the X-GitHub-Delivery header), and
broadcasts a ``github-event`` WS message to the guild.

When the event is linked to a known task and clears the noise filters
(see ``_should_dispatch_to_foreman``), the receiver also schedules a
``run_foreman_ai`` invocation with a structured summary so the foreman
can decide whether to send_followup, finalize, or no-op.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import UTC, datetime

import discord_notifier
from database import get_db_dep
from db import github_cache
from events import broadcast_msg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from foreman.constants import _TERMINAL_STATES
from foreman.runner import ensure_poll_loop, run_foreman_ai
from lock_service import LockService
from models import (
    GithubEvent,
    GithubToken,
    Guild,
    GuildMember,
    Message,
    Task,
    TaskEvent,
    finalize_soft_delete_at,
)
from pydantic import BaseModel
from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from util.tasks import spawn
from ws_types import ChatMsg, GithubEventMsg, TaskFinalizeMsg, TaskUpdateMsg

logger = logging.getLogger(__name__)

router = APIRouter()

# How long (seconds) to wait for further events on the same PR before
# delivering the buffered batch to the foreman.  Previously defaulted to 180s
# to cover the full CI run, but CI completion is now injected directly by
# GitHub Actions (POST /foreman/ci-notify), so a short window is sufficient
# to coalesce review comments and other rapid events.
# Configurable via WEBHOOK_DEBOUNCE_SECONDS (default: 30 seconds).
DEBOUNCE_WINDOW_SECONDS: float = float(os.environ.get("WEBHOOK_DEBOUNCE_SECONDS", "30"))


# Minimum time (seconds) between dispatching review_requested for the same
# PR/commit pair. GitHub can refire review_requested for the same head sha
# (e.g. re-requesting a dismissed review, or a reviewer being re-added), and
# without this the foreman would spin up a duplicate review task/PR comment
# each time. Enforced via LockService (backend/lock_service.py) so it holds
# across backend replicas, not just within one process.
# Configurable via PR_REVIEW_COOLDOWN_SECONDS (default: 300 seconds / 5 min).
# Read at call time so monkeypatch.setenv works in tests and the value can
# change without a restart.
def _get_pr_review_cooldown_seconds() -> int:
    return int(float(os.environ.get("PR_REVIEW_COOLDOWN_SECONDS", "300")))


# Shared secret for the /foreman/ci-notify endpoint.  GitHub Actions sets
# Authorization: Bearer <PIONEER_CI_KEY> on each CI completion POST.
# When empty the endpoint returns 503 (not configured).
# Read at call time so monkeypatch.setenv works in tests and secrets can rotate
# without a restart.
def _get_ci_key() -> str:
    return os.environ.get("PIONEER_CI_KEY", "")


# Cap on stored payloads. GitHub webhook payloads are typically <50 KB but
# review-comment diff hunks can balloon — 64 KB is a safe upper bound that
# keeps the row size manageable while preserving enough for foreman context.
_MAX_PAYLOAD_BYTES = 64 * 1024

# Event types that are *expected* to come from bots (``[bot]`` suffix in
# sender.login). For these the bot filter is bypassed because bots drive
# most CI/automation traffic — filtering them out would silence the very
# events we want to react to.
_BOT_OK_EVENTS = frozenset({"check_run", "check_suite", "status"})

# Label names (case-insensitive) that mark a GitHub issue ready for the foreman
# to pick up. Mirrors the "Periodic devReady issue pickup" label list in
# foreman/prompt.py so a webhook-triggered claim and the periodic sweep agree
# on what counts as devReady.
_DEVREADY_LABELS = frozenset({"devready", "dev-ready", "ready-for-dev", "ready"})


def _issue_label_names(issue_obj: dict) -> list[str]:
    labels = issue_obj.get("labels") or []
    return [lb["name"] for lb in labels if isinstance(lb, dict) and lb.get("name")]


def _matching_devready_label(label_names: list[str]) -> str | None:
    return next((name for name in label_names if name.lower() in _DEVREADY_LABELS), None)


def _devready_issue_trigger(event_type: str, action: str | None, payload: dict) -> str | None:
    """Return the matched devReady label name if this ``issues`` event should trigger
    immediate pickup, else ``None``.

    Triggers on ``action == "labeled"`` where the label just applied is a devReady
    alias, or ``action in {"opened", "reopened"}`` where the issue already carries one
    (e.g. it was created with the label already attached, or reopened after having it).
    """
    if event_type != "issues":
        return None
    issue_obj = payload.get("issue")
    if not isinstance(issue_obj, dict):
        return None
    if action == "labeled":
        label_obj = payload.get("label")
        label_name = label_obj.get("name") if isinstance(label_obj, dict) else None
        if label_name and label_name.lower() in _DEVREADY_LABELS:
            return label_name
        return None
    if action in {"opened", "reopened"}:
        return _matching_devready_label(_issue_label_names(issue_obj))
    return None


class DebounceQueue:
    """Per-PR debounce queue that coalesces rapid webhook events.

    State is scoped to the instance so tests can create an isolated copy
    without touching the module-level singleton.
    """

    def __init__(self, window_seconds: float = DEBOUNCE_WINDOW_SECONDS) -> None:
        self._window = window_seconds
        self._buffers: dict[str, list[tuple[str, str | None, str | None]]] = {}
        self._tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        self._generation: dict[str, int] = {}

    def reset(self) -> None:
        """Cancel all in-flight timers and clear state (sync, no await).

        Prefer ``shutdown()`` in async contexts — this variant exists for
        synchronous helpers that cannot await.
        """
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._buffers.clear()
        self._generation.clear()

    async def shutdown(self) -> None:
        """Cancel all in-flight timers and wait for them to finish.

        Awaiting the cancelled tasks lets them complete cleanly and prevents
        "Task destroyed but pending" warnings.  Call this at server shutdown
        and in async test teardown.
        """
        pending = [t for t in self._tasks.values() if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._buffers.clear()
        self._generation.clear()

    @staticmethod
    def _log_fire_error(task: asyncio.Task) -> None:  # type: ignore[type-arg]
        """Done callback: log any unexpected error from a _fire task."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception(
                "Debounce fire task %s raised an unexpected error",
                task.get_name(),
                exc_info=exc,
            )

    async def _deliver(
        self, key: str, guild_id: str, items: list[tuple[str, str | None, str | None]]
    ) -> None:
        summaries = [s for s, _, _ in items]
        combined = "\n\n---\n\n".join(summaries) if len(summaries) > 1 else summaries[0]
        # Use the first non-bot user_id in the batch. All items share the same
        # task owner, but a bot user_id (ending in "[bot]") is less useful for
        # attribution than a real human.  Fall back to any non-None user_id if
        # every entry is a bot, and to None if the batch is entirely anonymous.
        user_id = next(
            (uid for _, uid, _ in items if uid and not uid.endswith("[bot]")),
            next((uid for _, uid, _ in items if uid), None),
        )
        # All events buffered under one key share a task (key is "{guild}:{task_id}");
        # github events for a known task run in that task's isolated child context.
        task_id = next((tid for _, _, tid in items if tid), None)
        await run_foreman_ai(
            guild_id,
            combined,
            user_id=user_id,
            task_id=task_id,
            child=bool(task_id),
            trigger="github-event",
        )
        # Automated github churn (CI check/status especially) fired its run above;
        # only guarantee the safety-net loop exists — do NOT reset the backoff, or
        # a PR under active CI would pin the periodic poll at the floor.
        ensure_poll_loop(guild_id)
        logger.info(
            "github webhook debounce fired key=%s events=%d guild=%s",
            key,
            len(items),
            guild_id,
        )

    async def _fire(self, key: str, guild_id: str, gen: int) -> None:
        """Sleep for the debounce window then deliver all buffered events.

        ``gen`` is the generation counter captured when this timer was created.
        schedule() bumps the generation *before* cancelling the old task, so
        even if the old task's sleep resolves during the cancel window and the
        task runs during ``await gather()``, it will see a stale generation
        and bail out without delivering — eliminating the task-identity race.
        """
        try:
            await asyncio.sleep(self._window)
        except asyncio.CancelledError:
            # External cancellation (shutdown or test teardown): preserve the
            # buffer so the next schedule() can re-use it, and exit cleanly.
            raise
        # Stale-generation guard: if schedule() ran after our sleep started it
        # bumped the generation counter before cancelling us.  If the cancel
        # arrived too late (sleep future already resolved) and this coroutine
        # resumed normally, the generation mismatch catches the stale timer
        # and prevents double delivery.
        if self._generation.get(key) != gen:
            return
        items = self._buffers.pop(key, [])
        self._tasks.pop(key, None)
        self._generation.pop(key, None)
        if not items:
            return
        await self._deliver(key, guild_id, items)

    async def schedule(
        self,
        key: str,
        guild_id: str,
        summary: str,
        user_id: str | None,
        task_id: str | None = None,
    ) -> None:
        """Buffer *summary* and (re)start the per-PR debounce timer.

        Each call resets the window so the foreman is only invoked after
        _window seconds of silence on this PR.  The old timer is cancelled
        and awaited before the new one starts so there is never a window
        where two timers for the same key are both live.
        """
        existing = self._tasks.pop(key, None)
        # Snapshot the buffer BEFORE bumping the generation or cancelling.
        # If _fire had already consumed the buffer (completed mid-deliver and
        # was then cancelled), the snapshot lets us recover those events so
        # they are coalesced with the new one rather than silently dropped.
        snapshot = list(self._buffers.get(key, []))
        # Bump the generation BEFORE cancelling so that even if the old task's
        # sleep has already resolved and the task runs during ``await gather()``,
        # it will see a stale generation and bail out without delivering.
        gen = self._generation.get(key, 0) + 1
        self._generation[key] = gen
        if existing and not existing.done():
            existing.cancel()
            await asyncio.gather(existing, return_exceptions=True)
        # Seed the new buffer from the snapshot so events accumulated in the
        # old window are never lost regardless of whether the old fire task
        # was cancelled mid-sleep or ran to completion.  The generation bump
        # above ensures the old task cannot also deliver the same events, so
        # there is no risk of double delivery.
        self._buffers[key] = snapshot
        self._buffers[key].append((summary, user_id, task_id))
        # Use asyncio.create_task directly: the task is kept alive by
        # self._tasks[key] (a strong reference), so GC is not a concern.
        # _log_fire_error handles unexpected exceptions via the done callback.
        task = asyncio.create_task(
            self._fire(key, guild_id, gen),
            name=f"foreman.debounce:{key}",
        )
        task.add_done_callback(DebounceQueue._log_fire_error)
        self._tasks[key] = task
        logger.info(
            "github webhook debounce scheduled key=%s buffered=%d",
            key,
            len(self._buffers[key]),
        )


_debounce_queue = DebounceQueue()


async def _auto_finalize_task_on_pr_merge(
    db: AsyncSession,
    guild_pk: int,
    guild_id: str,
    task_id: str,
) -> bool:
    """Directly transition a task to 'done' when its PR is merged.

    Uses a single conditional UPDATE to prevent TOCTOU races with concurrent
    state transitions. Returns True if finalization occurred.
    Releases any in-progress lock, discards queued follow-up events, and broadcasts
    TaskFinalizeMsg + TaskUpdateMsg so the frontend reflects the change immediately.
    """
    # Fetch worker_id + issue link for the broadcast and the keep-alive decision;
    # state filtering is delegated entirely to the conditional UPDATE to eliminate
    # the SELECT→UPDATE TOCTOU.
    row_result = await db.exec(
        select(col(Task.worker_id), col(Task.issue_number), col(Task.issue_state)).where(
            col(Task.id) == task_id, col(Task.guild_id) == guild_pk
        )
    )
    task_row = row_result.one_or_none()
    if task_row is None:
        return False
    worker_id, issue_number, issue_state = task_row

    # done + still-open issue ⇒ stay live (deleted_at NULL) until the issue closes.
    deleted_at = finalize_soft_delete_at("done", issue_number, issue_state)

    # Single conditional UPDATE — only fires if task is not already in a terminal
    # state, eliminating the race between two concurrent state transitions.
    upd = await db.exec(
        update(Task)
        .where(
            col(Task.id) == task_id,
            col(Task.guild_id) == guild_pk,
            col(Task.state).notin_(list(_TERMINAL_STATES)),
        )
        .values(state="done", deleted_at=deleted_at)
    )
    if (getattr(upd, "rowcount", 0) or 0) == 0:
        return False

    await LockService(db).release(f"task:{task_id}")
    await db.exec(delete(TaskEvent).where(col(TaskEvent.task_id) == task_id))
    await db.commit()

    await broadcast_msg(guild_id, TaskFinalizeMsg(workerId=worker_id, taskId=task_id))
    await broadcast_msg(
        guild_id,
        TaskUpdateMsg(
            taskId=task_id,
            state="done",
            deletedAt=deleted_at.isoformat() if deleted_at else None,
        ),
    )
    return True


async def _auto_fail_task_on_pr_close(
    db: AsyncSession,
    guild_pk: int,
    guild_id: str,
    task_id: str,
) -> bool:
    """Directly transition a task to 'failed' when its PR is closed without merging.

    Uses a single conditional UPDATE to prevent TOCTOU races with concurrent
    state transitions. Returns True if the transition occurred.
    Releases any in-progress lock, discards queued follow-up events, and broadcasts
    TaskFinalizeMsg + TaskUpdateMsg so the frontend updates.
    """
    # Fetch worker_id for TaskFinalizeMsg; state filtering is delegated to the
    # conditional UPDATE to eliminate the SELECT→UPDATE TOCTOU.
    row_result = await db.exec(
        select(col(Task.id), col(Task.worker_id)).where(
            col(Task.id) == task_id, col(Task.guild_id) == guild_pk
        )
    )
    task_row = row_result.one_or_none()
    if task_row is None:
        return False
    worker_id = task_row[1]

    deleted_at = datetime.now(UTC)  # failures are stamped immediately

    # Single conditional UPDATE — only fires if task is not already in a terminal
    # state, eliminating the race between two concurrent state transitions.
    upd = await db.exec(
        update(Task)
        .where(
            col(Task.id) == task_id,
            col(Task.guild_id) == guild_pk,
            col(Task.state).notin_(list(_TERMINAL_STATES)),
        )
        .values(state="failed", deleted_at=deleted_at)
    )
    if (getattr(upd, "rowcount", 0) or 0) == 0:
        return False

    await LockService(db).release(f"task:{task_id}")
    await db.exec(delete(TaskEvent).where(col(TaskEvent.task_id) == task_id))
    await db.commit()

    await broadcast_msg(guild_id, TaskFinalizeMsg(workerId=worker_id, taskId=task_id))
    await broadcast_msg(
        guild_id,
        TaskUpdateMsg(
            taskId=task_id,
            state="failed",
            deletedAt=deleted_at.isoformat(),
        ),
    )
    return True


async def shutdown_debouncer() -> None:
    """Cancel all in-flight debounce timers and wait for them to complete.

    Call at server shutdown and in async test teardown fixtures.
    """
    await _debounce_queue.shutdown()


def _verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Constant-time HMAC-SHA256 check against GitHub's ``sha256=...`` header."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    provided = signature_header[len("sha256=") :]
    return hmac.compare_digest(expected, provided)


def _extract_pr_info(payload: dict) -> tuple[int | None, str | None, str | None]:
    """Pull (pr_number, pr_url, repo) out of a webhook payload.

    The fields live in different places per event type. ``pull_request``,
    ``pull_request_review``, ``pull_request_review_comment`` and
    ``issue_comment`` (on PRs) each carry a ``pull_request`` object;
    ``check_run`` / ``check_suite`` carry it under
    ``check_run.pull_requests[0]`` (an array because a head SHA could be in
    multiple PRs); ``status`` doesn't carry one at all and must be looked
    up separately.
    """
    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        repo = (payload.get("repository") or {}).get("full_name")
        return pr.get("number"), pr.get("html_url"), repo

    issue = payload.get("issue")
    if isinstance(issue, dict) and "pull_request" in issue:
        repo = (payload.get("repository") or {}).get("full_name")
        return issue.get("number"), issue.get("html_url"), repo

    check_run = payload.get("check_run") or payload.get("check_suite")
    if isinstance(check_run, dict):
        prs = check_run.get("pull_requests") or []
        if prs and isinstance(prs[0], dict):
            repo = (payload.get("repository") or {}).get("full_name")
            return prs[0].get("number"), prs[0].get("html_url"), repo

    repo = (payload.get("repository") or {}).get("full_name")
    return None, None, repo


def _check_run_head_branch(event_type: str, node: dict) -> str | None:
    """Return the head branch a ``check_run``/``check_suite`` event ran against.

    ``check_suite`` payloads carry ``head_branch`` directly; ``check_run``
    payloads nest it under their embedded (minimal) ``check_suite`` object.
    """
    if event_type == "check_suite":
        return node.get("head_branch")
    return (node.get("check_suite") or {}).get("head_branch")


def _should_notify_ci_result(head_branch: str | None, conclusion: str | None) -> bool:
    """Decide whether a ``check_run``/``check_suite`` conclusion should notify Discord.

    ``main`` has no PR review gate left to act on, so a passing/cancelled/etc.
    result there is just noise — only failures are worth paging on. PR branches
    keep the full pass/fail signal since that's what review depends on.
    """
    if conclusion not in {"success", "failure", "cancelled", "timed_out", "action_required"}:
        return False
    return head_branch != "main" or conclusion == "failure"


async def _find_task(db, guild_pk: int, repo: str | None, pr_number: int | None):
    """Match a webhook to one of this guild's tasks by (repo, pr_number).

    Returns the task row (id, user_id, issue_repo, issue_number, branch) or
    ``None``. The user_id is needed so foreman dispatch routes back to the
    user who originally created the task; issue_repo/issue_number is the
    task's linked GitHub issue (set at ``assign_task`` time), used to route
    PR Discord notifications into that issue's existing thread instead of a
    new one; branch is the fallback source for that linkage when the PR
    payload itself doesn't carry a head ref.
    """
    if not repo or pr_number is None:
        return None
    res = await db.exec(
        select(
            col(Task.id),
            col(Task.user_id),
            col(Task.issue_repo),
            col(Task.issue_number),
            col(Task.branch),
        )
        .where(
            col(Task.guild_id) == guild_pk,
            col(Task.pr_repo) == repo,
            col(Task.pr_number) == pr_number,
        )
        .order_by(col(Task.created_at).desc())
        .limit(1)
    )
    return res.first()


async def _get_guild_owner_github_login(db: AsyncSession, guild_pk: int) -> str | None:
    """Return the GitHub login of the guild owner, or None if not found."""
    result = await db.exec(
        select(col(GithubToken.github_username))
        .join(GuildMember, col(GuildMember.user_id) == col(GithubToken.github_user_id))
        .where(col(GuildMember.guild_id) == guild_pk, col(GuildMember.role) == "owner")
        .limit(1)
    )
    row = result.first()
    return row if isinstance(row, str) else None


def _should_dispatch_to_foreman(
    event_type: str,
    action: str | None,
    payload: dict,
    sender_login: str | None,
    task_id: str | None,
) -> tuple[bool, str]:
    """Decide whether this webhook should wake the foreman.

    Returns ``(dispatch, reason)``. *reason* is the skip reason when
    ``dispatch`` is False, or an empty string otherwise — useful for
    log lines and tests.

    Filtering rules (see docs/github-pr-subscriptions.md §4):
    - No matching task ⇒ skip (foreman has no context)
    - ``[bot]`` senders on non-CI events ⇒ skip (Dependabot opening PRs etc)
    - ``check_run`` events with ``status != "completed"`` ⇒ skip (pending runs are noisy)
    """
    if not task_id:
        return False, "no-matching-task"
    if sender_login and sender_login.endswith("[bot]") and event_type not in _BOT_OK_EVENTS:
        return False, "bot-non-ci-sender"
    if event_type in {"check_run", "check_suite"}:
        node = payload.get(event_type) or {}
        status = node.get("status") if isinstance(node, dict) else None
        if status and status != "completed":
            return False, f"check-status-{status}"
        # Skip neutral / skipped conclusions — only success/failure/cancelled
        # are actionable signals for the foreman.
        conclusion = node.get("conclusion") if isinstance(node, dict) else None
        if conclusion in {"neutral", "skipped"}:
            return False, f"check-conclusion-{conclusion}"
    return True, ""


def _build_foreman_summary(
    event_type: str,
    action: str | None,
    payload: dict,
    repo: str | None,
    pr_number: int | None,
    task_id: str,
    sender_login: str | None,
) -> str:
    """Render a structured ``[github-event]`` message for the foreman.

    The format is intentionally compact and includes the task id + PR
    coordinates first so the foreman can immediately route to send_followup
    / finalize_task without a get_task_status round-trip for trivial cases.
    """
    task_part = f" (task {task_id})" if task_id else " (new)"
    header = (
        f"[github-event] {event_type}"
        + (f"/{action}" if action else "")
        + f" on {repo}#{pr_number}{task_part}"
    )
    sender_line = f" by @{sender_login}" if sender_login else ""

    detail = ""
    if event_type == "pull_request" and action == "review_requested":
        pr = payload.get("pull_request") or {}
        title = (pr.get("title") or "")[:200]
        requested_reviewer = (payload.get("requested_reviewer") or {}).get("login", "")
        pr_url_val = pr.get("html_url") or ""
        detail = (
            f"Review requested from @{requested_reviewer}.\n"
            f"PR title: {title}\n"
            f"PR URL: {pr_url_val}\n"
            "Create a review task and assign a worker to perform a full PR review. "
            "The worker must check out the branch, run available tests/lint, and post "
            f"findings via: gh pr review {pr_number} --repo {repo} "
            '[--approve | --request-changes | --comment] --body "..."\n'
            "The worker is explicitly permitted — and encouraged — to --approve if the "
            "code looks good. Only use --request-changes for real blocking issues; "
            "use --comment for minor nits that don't block merging. "
            "Do NOT commit any files. Do NOT open a new PR.\n"
            "If a review task already exists for this PR in the current task list, "
            "skip task creation to avoid duplicates."
        )
    elif event_type == "pull_request" and action in {"closed", "reopened"}:
        pr = payload.get("pull_request") or {}
        merged = pr.get("merged")
        if action == "closed" and merged:
            detail = (
                "PR was merged. The task has been automatically finalized (state=done). "
                "No action needed — this message is informational."
            )
        elif action == "closed":
            detail = (
                "PR was closed without merging. The task has been automatically marked as failed. "
                "If the work should be retried, use send_followup or assign_task to create "
                "a new task on a fresh branch."
            )
        else:
            detail = (
                "PR was reopened — no immediate action required unless prior work needs revisiting."
            )
    elif event_type == "pull_request_review" and action == "submitted":
        review = payload.get("review") or {}
        state = review.get("state")
        body = (review.get("body") or "")[:400]
        detail = f"Review state: {state}." + (f"\nReview body:\n{body}" if body else "")
        if state == "changes_requested":
            detail += (
                "\nCall send_followup with the requested changes — but first check them "
                "against the linked issue (source of truth for intent) and decline, "
                "assertively and citing the issue number, anything that contradicts it. "
                "Minor nits can still be accepted."
            )
        elif state == "approved":
            detail += (
                "\nReview is approved — wait for merge before finalizing, "
                "or finalize now if the workflow auto-merges."
            )
    elif event_type == "pull_request_review_comment" and action == "created":
        comment = payload.get("comment") or {}
        path = comment.get("path", "")
        body = (comment.get("body") or "")[:400]
        detail = (
            f"Inline comment on `{path}`:\n{body}\n"
            "Decide whether send_followup is warranted. If so, check the comment against "
            "the linked issue first and decline (assertively, citing the issue number) "
            "anything that contradicts its stated intent; minor nits can still be accepted."
        )
    elif event_type == "issue_comment" and action == "created":
        comment = payload.get("comment") or {}
        body = (comment.get("body") or "")[:400]
        detail = (
            f"PR comment:\n{body}\n"
            "If this is a request for changes, send_followup — but first check it against "
            "the linked issue (source of truth for intent) and decline, assertively and "
            "citing the issue number, anything that contradicts it; otherwise no action."
        )
    elif event_type in {"check_run", "check_suite"}:
        node = payload.get(event_type) or {}
        name = node.get("name") or node.get("app", {}).get("slug") or "check"
        conclusion = node.get("conclusion")
        summary = (node.get("output") or {}).get("summary") or ""
        detail = f"{name}: {conclusion}." + (f"\nSummary: {summary[:400]}" if summary else "")
        if conclusion == "failure":
            detail += (
                "\nCI failed — call send_followup with concrete instructions to fix the failure."
            )
        elif conclusion == "success":
            detail += "\nCI passed — if all required checks have completed, you can finalize_task."
    elif event_type == "status":
        state = payload.get("state")
        context = payload.get("context") or "status"
        description = payload.get("description") or ""
        detail = f"{context}: {state}.{(' ' + description) if description else ''}"
    elif event_type == "issues" and action in {"labeled", "opened", "reopened"}:
        issue_obj = payload.get("issue") or {}
        matched_label = _devready_issue_trigger(event_type, action, payload)
        title = (issue_obj.get("title") or "")[:200]
        issue_url = issue_obj.get("html_url") or ""
        assignees = issue_obj.get("assignees") or []
        assignee_logins = [a["login"] for a in assignees if isinstance(a, dict) and a.get("login")]
        assignee_str = ", ".join(assignee_logins) or "none"
        label_line = f"Label applied: {matched_label}\n" if matched_label else ""
        detail = (
            f"{label_line}"
            f"Issue title: {title}\n"
            f"Issue URL: {issue_url}\n"
            f"Current assignees: {assignee_str}\n"
            "This issue is devReady — run the devReady pickup flow now (same as "
            "periodic-check): skip if already assigned to someone else, or if a "
            "non-terminal task already references this issue number in the current "
            "<state> task list; otherwise claim_github_issue, then create_task + "
            "assign_task as an atomic pair with issue_number and issue_repo set on both."
        )

    return f"{header}{sender_line}\n{detail}".strip() if detail else f"{header}{sender_line}"


def _build_chat_line(
    event_type: str,
    action: str | None,
    repo: str | None,
    pr_number: int | None,
    payload: dict,
    task_id: str | None,
) -> str:
    """Build a compact ``[github-event]`` chat line for the foreman stream.

    Format: ``[github-event] pull_request/closed — repo#42 (merged=true) — task: t-abc``
    """
    event_action = f"{event_type}/{action}" if action else event_type
    pr_part = f" — {repo}#{pr_number}" if repo and pr_number else (f" — {repo}" if repo else "")

    extra = ""
    if event_type == "pull_request" and action == "review_requested":
        requested_reviewer = (payload.get("requested_reviewer") or {}).get("login")
        if requested_reviewer:
            extra = f"reviewer={requested_reviewer}"
    elif event_type == "pull_request" and action == "closed":
        pr = payload.get("pull_request") or {}
        merged = pr.get("merged")
        if merged is not None:
            extra = f"merged={str(merged).lower()}"
    elif event_type in {"check_run", "check_suite"}:
        node = payload.get(event_type) or {}
        conclusion = node.get("conclusion") if isinstance(node, dict) else None
        if conclusion:
            extra = f"conclusion={conclusion}"
    elif event_type == "pull_request_review" and action == "submitted":
        review = payload.get("review") or {}
        state = review.get("state") if isinstance(review, dict) else None
        if state:
            extra = f"state={state}"

    extra_part = f" ({extra})" if extra else ""
    task_part = f" — task: {task_id}" if task_id else ""

    return f"[github-event] {event_action}{pr_part}{extra_part}{task_part}"


@router.post("/webhooks/github/{guild_id}")
async def github_webhook(
    guild_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db_dep),
) -> Response:
    body = await request.body()
    if len(body) > _MAX_PAYLOAD_BYTES * 4:
        # Hard upper bound on accept size; 4× the persisted cap so we still
        # accept large but plausible deliveries (e.g. very long PR bodies).
        raise HTTPException(status_code=413, detail="Payload too large")

    delivery_id = request.headers.get("x-github-delivery")
    event_type = request.headers.get("x-github-event")
    signature = request.headers.get("x-hub-signature-256")
    if not delivery_id or not event_type:
        raise HTTPException(status_code=400, detail="Missing GitHub headers")

    logger.info(
        "github webhook received guild=%s delivery=%s event=%s",
        guild_id,
        delivery_id,
        event_type,
    )

    guild_res = await db.exec(
        select(col(Guild.webhook_secret), col(Guild.id)).where(col(Guild.slug) == guild_id)
    )
    guild_row = guild_res.one_or_none()
    if not guild_row or not guild_row.webhook_secret:
        # Guild missing or no secret configured. Don't leak which is which.
        raise HTTPException(status_code=404, detail="Webhook not configured")
    secret = guild_row.webhook_secret
    guild_pk = guild_row.id

    if not _verify_signature(secret, body, signature):
        logger.warning(
            "github webhook signature mismatch guild=%s delivery=%s event=%s",
            guild_id,
            delivery_id,
            event_type,
        )
        raise HTTPException(status_code=401, detail="Invalid signature")

    # GitHub sends ping deliveries on webhook setup; accept them but skip
    # the rest of the pipeline (no PR context, no foreman action).
    if event_type == "ping":
        logger.info(
            "github webhook ping guild=%s delivery=%s",
            guild_id,
            delivery_id,
        )
        return Response(status_code=204)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.warning(
            "github webhook invalid JSON guild=%s delivery=%s event=%s excerpt=%r",
            guild_id,
            delivery_id,
            event_type,
            body[:200].decode("utf-8", errors="replace"),
        )
        raise HTTPException(status_code=400, detail="Invalid JSON body") from None
    if not isinstance(payload, dict):
        logger.warning(
            "github webhook non-object payload guild=%s delivery=%s event=%s",
            guild_id,
            delivery_id,
            event_type,
        )
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")

    action = payload.get("action") if isinstance(payload.get("action"), str) else None
    pr_number, pr_url, repo = _extract_pr_info(payload)
    sender = payload.get("sender") or {}
    sender_login = sender.get("login") if isinstance(sender, dict) else None

    task_row = await _find_task(db, guild_pk, repo, pr_number)
    task_id = task_row.id if task_row else None
    task_user_id = task_row.user_id if task_row else None

    # Trim the persisted payload so a runaway diff hunk can't balloon the DB.
    body_text = body.decode("utf-8", errors="replace")
    if len(body_text) > _MAX_PAYLOAD_BYTES:
        body_text = body_text[:_MAX_PAYLOAD_BYTES] + "\n…[truncated]"

    created_at = datetime.now(UTC)
    stmt = (
        pg_insert(GithubEvent)
        .values(
            guild_id=guild_pk,
            task_id=task_id,
            delivery_id=delivery_id,
            event_type=event_type,
            action=action,
            repo=repo or "",
            pr_number=pr_number,
            pr_url=pr_url,
            sender_login=sender_login,
            payload_json=body_text,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=["delivery_id"])
    )
    try:
        result = await db.exec(stmt)
    except IntegrityError:
        await db.rollback()
        result = None
    await db.commit()

    # ``rowcount`` is 0 when ON CONFLICT DO NOTHING fired — i.e. GitHub
    # redelivered an event we already accepted. Return 202 so GitHub stops
    # retrying without re-triggering downstream side effects.
    is_duplicate = result is None or (getattr(result, "rowcount", 0) or 0) == 0
    if is_duplicate:
        logger.info(
            "github webhook duplicate delivery guild=%s delivery=%s event=%s",
            guild_id,
            delivery_id,
            event_type,
        )
        return Response(status_code=202)

    # Keep the local github_issues/github_pull_requests cache current (#864).
    if event_type == "issues" and repo:
        issue_payload = payload.get("issue")
        if isinstance(issue_payload, dict):
            await github_cache.upsert_issue(db, repo, issue_payload)
    elif event_type == "pull_request" and repo:
        pr_payload = payload.get("pull_request")
        if isinstance(pr_payload, dict):
            await github_cache.upsert_pr(db, repo, pr_payload)

    # Back-fill pr_url on the task from the webhook payload when a
    # pull_request event arrives and the task doesn't already have it set.
    # This is a safety net for the (rare) case where the worker's
    # task-update message was dropped before pr_url was persisted.
    if task_id and pr_url and event_type == "pull_request":
        await db.exec(
            update(Task)
            .where(col(Task.id) == task_id, col(Task.pr_url).is_(None))
            .values(pr_url=pr_url)
        )
        await db.commit()

    # Back-fill pr_url onto tasks matched by branch name rather than (repo,
    # pr_number) — covers the window before the worker's task-update message
    # sets pr_repo/pr_number, using the branch column set at task-creation
    # time. Restricted to non-terminal tasks so a stale/reused branch name on
    # a finished task is never touched.
    if event_type == "pull_request" and action in {"opened", "synchronize", "reopened"} and pr_url:
        pr_obj = payload.get("pull_request") or {}
        head_ref = (pr_obj.get("head") or {}).get("ref")
        if head_ref:
            branch_match_result = await db.exec(
                select(col(Task.id)).where(
                    col(Task.guild_id) == guild_pk,
                    col(Task.branch) == head_ref,
                    col(Task.pr_url).is_(None),
                    col(Task.state).notin_(list(_TERMINAL_STATES)),
                )
            )
            branch_matched_task_ids = branch_match_result.all()
            if branch_matched_task_ids:
                await db.exec(
                    update(Task)
                    .where(
                        col(Task.id).in_(branch_matched_task_ids),
                        col(Task.pr_url).is_(None),
                    )
                    .values(pr_url=pr_url)
                )
                await db.commit()
                logger.info(
                    "github webhook branch-matched pr_url backfill guild=%s branch=%s "
                    "pr_url=%s tasks=%s",
                    guild_id,
                    head_ref,
                    pr_url,
                    branch_matched_task_ids,
                )

    # Keep issue_state and issue_title current on all tasks linked to this issue
    # when GitHub sends an issues event (opened / closed / reopened / edited).
    if event_type == "issues" and action in ("opened", "closed", "reopened", "edited") and repo:
        issue_obj = payload.get("issue") or {}
        issue_num = issue_obj.get("number")
        if issue_num:
            new_issue_state = "closed" if action == "closed" else "open"
            new_issue_title: str | None = issue_obj.get("title") or None
            update_values: dict = {"issue_state": new_issue_state}
            if new_issue_title:
                update_values["issue_title"] = new_issue_title
            await db.exec(
                update(Task)
                .where(
                    col(Task.guild_id) == guild_pk,
                    col(Task.issue_repo) == repo,
                    col(Task.issue_number) == issue_num,
                )
                .values(**update_values)
            )
            await db.commit()
            logger.info(
                "github webhook updated issue_state=%s issue_title=%r for issue %s#%s guild=%s",
                new_issue_state,
                new_issue_title,
                repo,
                issue_num,
                guild_id,
            )

            # A closed issue sweeps its linked tasks directly — no foreman/LLM
            # round-trip needed, since the issue's own state is the only signal
            # that matters here. The periodic sweep in
            # foreman.runner._sweep_closed_issues is a safety net for missed
            # deliveries; this is the primary (near-instant) path.
            if action == "closed":
                from foreman.tools import finalize_closed_issue

                finalized_ids = await finalize_closed_issue(db, guild_pk, guild_id, repo, issue_num)
                if finalized_ids:
                    logger.info(
                        "github webhook finalized %d task(s) on issue %s#%s close guild=%s",
                        len(finalized_ids),
                        repo,
                        issue_num,
                        guild_id,
                    )

    # Deterministic lifecycle transitions: finalize on merge, fail on close-without-merge.
    # These happen directly — no AI decision needed for these clear-cut outcomes.
    if task_id and event_type == "pull_request" and action == "closed":
        pr = payload.get("pull_request") or {}
        merged = pr.get("merged")
        if merged:
            finalized = await _auto_finalize_task_on_pr_merge(db, guild_pk, guild_id, task_id)
            if finalized:
                logger.info(
                    "github webhook auto-finalized task=%s on PR merge guild=%s",
                    task_id,
                    guild_id,
                )
        else:
            failed = await _auto_fail_task_on_pr_close(db, guild_pk, guild_id, task_id)
            if failed:
                logger.info(
                    "github webhook auto-failed task=%s on unmerged PR close guild=%s",
                    task_id,
                    guild_id,
                )

    logger.info(
        "github webhook accepted guild=%s delivery=%s event=%s action=%s repo=%s pr=%s task=%s",
        guild_id,
        delivery_id,
        event_type,
        action,
        repo,
        pr_number,
        task_id,
    )

    # Discord notifications for key GitHub events.
    _pr_label = f"{repo}#{pr_number}" if repo and pr_number else (repo or "")
    if event_type == "pull_request" and action == "opened":
        pr_obj = payload.get("pull_request") or {}
        pr_title = (pr_obj.get("title") or "")[:120]
        assignees = pr_obj.get("assignees") or []
        labels = pr_obj.get("labels") or []
        assignee_logins = [a["login"] for a in assignees if isinstance(a, dict) and a.get("login")]
        assignee_mentions = [
            await discord_notifier.mention_or_login(login) for login in assignee_logins
        ]
        assignee_str = ", ".join(assignee_mentions) or "None"
        labels_str = (
            ", ".join(lb["name"] for lb in labels if isinstance(lb, dict) and lb.get("name"))
            or "None"
        )
        spawn(
            discord_notifier.notify_event(
                "pr-opened",
                title=f"PR opened: {_pr_label}",
                description=pr_title or f"New pull request on {_pr_label}",
                url=pr_url or None,
                issue_repo=repo,
                issue_number=pr_number,
                kind="pr",
                thread_name=f"#{pr_number}: {pr_title}" if pr_title else f"#{pr_number}",
                header_fields={"Assignee": assignee_str, "Labels": labels_str},
                ps_guild_slug=guild_id,
                task_id=task_id,
            ),
            name=f"discord.pr-opened:{_pr_label}",
        )
    elif event_type == "pull_request" and action == "closed":
        pr_obj = payload.get("pull_request") or {}
        if pr_obj.get("merged"):
            spawn(
                discord_notifier.notify_event(
                    "pr-merged",
                    title=f"PR merged: {_pr_label}",
                    description=f"Pull request `{_pr_label}` was merged."
                    + (f" Task: `{task_id}`." if task_id else ""),
                    url=pr_url or None,
                    issue_repo=repo,
                    issue_number=pr_number,
                    kind="pr",
                    close=True,
                    ps_guild_slug=guild_id,
                    task_id=task_id,
                ),
                name=f"discord.pr-merged:{_pr_label}",
            )
        else:
            spawn(
                discord_notifier.notify_event(
                    "pr-closed",
                    title=f"PR closed: {_pr_label}",
                    description=f"Pull request `{_pr_label}` was closed without merging."
                    + (f" Task: `{task_id}`." if task_id else ""),
                    url=pr_url or None,
                    issue_repo=repo,
                    issue_number=pr_number,
                    kind="pr",
                    close=True,
                    ps_guild_slug=guild_id,
                    task_id=task_id,
                ),
                name=f"discord.pr-closed:{_pr_label}",
            )
    elif event_type == "pull_request" and action == "synchronize":
        spawn(
            discord_notifier.notify_event(
                "pr-updated",
                title=f"PR updated: {_pr_label}",
                description=f"New commits pushed to `{_pr_label}`."
                + (f" Task: `{task_id}`." if task_id else ""),
                url=pr_url or None,
                issue_repo=repo,
                issue_number=pr_number,
                kind="pr",
                ps_guild_slug=guild_id,
                task_id=task_id,
            ),
            name=f"discord.pr-updated:{_pr_label}",
        )
    elif event_type == "pull_request_review" and action == "submitted":
        review = payload.get("review") or {}
        state = review.get("state") or "commented"
        reviewer_mention = await discord_notifier.mention_or_login(
            (review.get("user") or {}).get("login")
        )
        spawn(
            discord_notifier.notify_event(
                "pr-review",
                title=f"PR review {state.replace('_', ' ')}: {_pr_label}",
                description=f"{reviewer_mention} {state.replace('_', ' ')} `{_pr_label}`."
                + (f" Task: `{task_id}`." if task_id else ""),
                url=(review.get("html_url") or pr_url) or None,
                issue_repo=repo,
                issue_number=pr_number,
                kind="pr",
                ps_guild_slug=guild_id,
                task_id=task_id,
            ),
            name=f"discord.pr-review:{_pr_label}",
        )
    elif event_type in {"check_run", "check_suite"}:
        node = payload.get(event_type) or {}
        conclusion = node.get("conclusion") if isinstance(node, dict) else None
        check_name = (
            (node.get("name") or node.get("app", {}).get("slug") or "CI")
            if isinstance(node, dict)
            else "CI"
        )
        head_branch = _check_run_head_branch(event_type, node) if isinstance(node, dict) else None
        # Buffered per-PR and combined into one Discord message (see
        # discord_notifier.notify_ci_check) instead of one message per check —
        # a CI matrix can complete several of these within seconds of each other.
        if _should_notify_ci_result(head_branch, conclusion):
            spawn(
                discord_notifier.notify_ci_check(
                    check_name,
                    conclusion,
                    pr_label=_pr_label,
                    url=pr_url or None,
                    issue_repo=repo,
                    issue_number=pr_number,
                    ps_guild_slug=guild_id,
                    task_id=task_id,
                ),
                name=f"discord.ci-check:{_pr_label}:{check_name}",
            )

    await broadcast_msg(
        guild_id,
        GithubEventMsg(
            deliveryId=delivery_id,
            event=event_type,
            action=action,
            repo=repo,
            prNumber=pr_number,
            prUrl=pr_url,
            taskId=task_id,
            sender=sender_login,
        ),
    )

    chat_line = _build_chat_line(event_type, action, repo, pr_number, payload, task_id)
    chat_now = datetime.now(UTC)
    await broadcast_msg(
        guild_id,
        ChatMsg(
            from_="github",
            to="foreman",
            content=chat_line,
            createdAt=chat_now.isoformat(),
        ),
    )
    db.add(
        Message(
            guild_id=guild_pk,
            from_agent="github",
            to_agent="foreman",
            content=chat_line,
            message_type="chat",
            created_at=chat_now,
            task_id=task_id,
        )
    )
    await db.commit()

    # review_requested is handled on its own path: it creates a new task so it
    # does not require an existing task_id, but it does require the requested
    # reviewer to match the guild owner's GitHub login.
    if event_type == "pull_request" and action == "review_requested":
        requested_reviewer = (payload.get("requested_reviewer") or {}).get("login")
        guild_owner_login = await _get_guild_owner_github_login(db, guild_pk)
        head_sha = ((payload.get("pull_request") or {}).get("head") or {}).get("sha") or ""
        if (
            requested_reviewer
            and guild_owner_login
            and requested_reviewer.lower() == guild_owner_login.lower()
        ):
            # Cooldown guards against duplicate review tasks when GitHub refires
            # review_requested for the same PR/commit (e.g. a dismissed review
            # being re-requested). Scoped by head sha so a genuinely new commit
            # still gets reviewed immediately.
            cooldown_seconds = _get_pr_review_cooldown_seconds()
            cooldown_key = f"pr_review:{repo}:{pr_number}:{head_sha}"
            cooldown_acquired = await LockService(db).acquire(
                cooldown_key, owner=delivery_id, ttl_seconds=cooldown_seconds
            )
            await db.commit()
            if not cooldown_acquired:
                logger.info(
                    "github webhook review_requested skip-cooldown guild=%s delivery=%s repo=%s "
                    "pr=%s sha=%s cooldown_seconds=%s",
                    guild_id,
                    delivery_id,
                    repo,
                    pr_number,
                    head_sha,
                    cooldown_seconds,
                )
            else:
                summary = _build_foreman_summary(
                    event_type, action, payload, repo, pr_number, task_id or "", sender_login
                )
                # Key is PR-scoped so rapid re-requests for the same PR coalesce.
                key = f"{guild_id}:review_requested:{repo}#{pr_number}"
                await _debounce_queue.schedule(
                    key, guild_id, summary, task_user_id, task_id=task_id
                )
                logger.info(
                    "github webhook review_requested dispatched guild=%s delivery=%s repo=%s pr=%s reviewer=%s",
                    guild_id,
                    delivery_id,
                    repo,
                    pr_number,
                    requested_reviewer,
                )
        else:
            logger.info(
                "github webhook skip-foreman guild=%s delivery=%s event=pull_request/review_requested "
                "reason=reviewer-mismatch requested=%s owner=%s",
                guild_id,
                delivery_id,
                requested_reviewer,
                guild_owner_login,
            )
    elif (devready_label := _devready_issue_trigger(event_type, action, payload)) is not None:
        # devReady issue pickup, same as review_requested above: no existing task/PR
        # is required, so this bypasses _should_dispatch_to_foreman's task_id gate.
        issue_obj = payload.get("issue") or {}
        issue_num = issue_obj.get("number")
        summary = _build_foreman_summary(
            event_type, action, payload, repo, issue_num, task_id or "", sender_login
        )
        # Key is issue-scoped so rapid re-labeling of the same issue coalesces.
        key = f"{guild_id}:issues-devready:{repo}#{issue_num}"
        await _debounce_queue.schedule(key, guild_id, summary, task_user_id, task_id=task_id)
        logger.info(
            "github webhook issues devready dispatched guild=%s delivery=%s repo=%s issue=%s "
            "action=%s label=%s",
            guild_id,
            delivery_id,
            repo,
            issue_num,
            action,
            devready_label,
        )
    else:
        dispatch, skip_reason = _should_dispatch_to_foreman(
            event_type, action, payload, sender_login, task_id
        )
        if dispatch:
            summary = _build_foreman_summary(
                event_type, action, payload, repo, pr_number, task_id or "", sender_login
            )
            key = f"{guild_id}:{task_id}"
            await _debounce_queue.schedule(key, guild_id, summary, task_user_id, task_id=task_id)
        else:
            logger.info(
                "github webhook skip-foreman guild=%s delivery=%s event=%s reason=%s",
                guild_id,
                delivery_id,
                event_type,
                skip_reason,
            )

    return Response(status_code=202)


# ---------------------------------------------------------------------------
# CI completion notification (GitHub Actions → foreman context)
# ---------------------------------------------------------------------------


class CINotifyPayload(BaseModel):
    """Body for POST /guilds/{guild_id}/foreman/ci-notify."""

    repo: str
    pr_number: int | None = None
    workflow_name: str
    conclusion: str  # success | failure | cancelled | timed_out
    task_id: str | None = None
    run_id: str | None = None
    run_url: str | None = None
    branch: str | None = None


@router.post("/guilds/{guild_id}/foreman/ci-notify")
async def ci_notify(
    guild_id: str,
    body: CINotifyPayload,
    request: Request,
    db: AsyncSession = Depends(get_db_dep),
) -> Response:
    """CI completion notification injected directly by GitHub Actions.

    Persists a structured ``[ci-notify]`` chat message so the foreman has
    CI context the next time it runs, **without** triggering a new foreman
    AI invocation.  This is the event-driven counterpart to the webhook
    debounce: GHA posts here the moment the workflow finishes, giving the
    foreman immediate context while avoiding an automatic re-trigger.

    Auth: ``Authorization: Bearer <PIONEER_CI_KEY>`` where the key matches
    the ``PIONEER_CI_KEY`` environment variable on the backend.
    """
    ci_key = _get_ci_key()
    if not ci_key:
        raise HTTPException(
            status_code=503, detail="CI notifications not configured (PIONEER_CI_KEY not set)"
        )
    auth = request.headers.get("authorization", "")
    provided = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
    if not hmac.compare_digest(provided.encode(), ci_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid CI key")

    repo = body.repo or ""
    pr_part = f"#{body.pr_number}" if body.pr_number is not None else ""
    workflow = body.workflow_name or "CI"
    conclusion = body.conclusion or "unknown"

    guild_res = await db.exec(select(col(Guild.id)).where(col(Guild.slug) == guild_id))
    guild_pk = guild_res.one_or_none()
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    task_id = body.task_id
    message_task_id: str | None = None  # FK field — only set when task is confirmed to exist
    if task_id:
        # Validate the caller-provided task_id belongs to this guild before using it as FK
        task_exists = await db.scalar(
            select(col(Task.id)).where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
        )
        message_task_id = task_id if task_exists else None
    elif body.pr_number is not None:
        task_row = await _find_task(db, guild_pk, repo, body.pr_number)
        if task_row:
            task_id = task_row.id
            message_task_id = task_id

    task_part = f" (task {task_id})" if task_id else ""
    run_part = (
        f" — {body.run_url}" if body.run_url else (f" — run {body.run_id}" if body.run_id else "")
    )
    content = f"[ci-notify] {workflow}/{conclusion} on {repo}{pr_part}{task_part}{run_part}"

    created_at = datetime.now(UTC)
    db.add(
        Message(
            guild_id=guild_pk,
            from_agent="github",
            to_agent="foreman",
            content=content,
            message_type="chat",
            created_at=created_at,
            task_id=message_task_id,
        )
    )
    await db.commit()
    await broadcast_msg(
        guild_id,
        ChatMsg(
            from_="github",
            to="foreman",
            content=content,
            createdAt=created_at.isoformat(),
        ),
    )
    logger.info(
        "ci-notify guild=%s repo=%s pr=%s task=%s conclusion=%s",
        guild_id,
        repo,
        body.pr_number,
        task_id,
        conclusion,
    )

    return Response(status_code=202)
