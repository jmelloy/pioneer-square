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

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime

from database import get_db
from events import broadcast
from fastapi import APIRouter, HTTPException, Request, Response
from foreman import reset_foreman_poll, run_foreman_ai
from models import GithubEvent, Guild, Task
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from util.tasks import spawn

logger = logging.getLogger(__name__)

router = APIRouter()


# Cap on stored payloads. GitHub webhook payloads are typically <50 KB but
# review-comment diff hunks can balloon — 64 KB is a safe upper bound that
# keeps the row size manageable while preserving enough for foreman context.
_MAX_PAYLOAD_BYTES = 64 * 1024

# Event types that are *expected* to come from bots (``[bot]`` suffix in
# sender.login). For these the bot filter is bypassed because bots drive
# most CI/automation traffic — filtering them out would silence the very
# events we want to react to.
_BOT_OK_EVENTS = frozenset({"check_run", "check_suite", "status"})


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


async def _find_task(db, guild_id: str, repo: str | None, pr_number: int | None):
    """Match a webhook to one of this guild's tasks by (repo, pr_number).

    Returns the task row (id + user_id) or ``None``. The user_id is needed so
    foreman dispatch routes back to the user who originally created the task.
    """
    if not repo or pr_number is None:
        return None
    res = await db.execute(
        select(Task.id, Task.user_id)
        .where(
            Task.guild_id == guild_id,
            Task.pr_repo == repo,
            Task.pr_number == pr_number,
        )
        .order_by(Task.created_at.desc())
        .limit(1)
    )
    return res.first()


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
    header = (
        f"[github-event] {event_type}"
        + (f"/{action}" if action else "")
        + f" on {repo}#{pr_number} (task {task_id})"
    )
    sender_line = f" by @{sender_login}" if sender_login else ""

    detail = ""
    if event_type == "pull_request" and action in {"closed", "reopened"}:
        pr = payload.get("pull_request") or {}
        merged = pr.get("merged")
        if action == "closed" and merged:
            detail = (
                "PR was merged. Call finalize_task — "
                "the work has landed and no follow-up is needed."
            )
        elif action == "closed":
            detail = (
                "PR was closed without merging. Investigate why "
                "(check_pr_status, get_task_status), then either send_followup "
                "to address the rejection or finalize_task if the work is being abandoned."
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
            detail += "\nCall send_followup with the requested changes."
        elif state == "approved":
            detail += (
                "\nReview is approved — wait for merge before finalizing, "
                "or finalize now if the workflow auto-merges."
            )
    elif event_type == "pull_request_review_comment" and action == "created":
        comment = payload.get("comment") or {}
        path = comment.get("path", "")
        body = (comment.get("body") or "")[:400]
        detail = f"Inline comment on `{path}`:\n{body}\nDecide whether send_followup is warranted."
    elif event_type == "issue_comment" and action == "created":
        comment = payload.get("comment") or {}
        body = (comment.get("body") or "")[:400]
        detail = (
            f"PR comment:\n{body}\n"
            "If this is a request for changes, send_followup; otherwise no action."
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

    return f"{header}{sender_line}\n{detail}".strip() if detail else f"{header}{sender_line}"


@router.post("/webhooks/github/{guild_id}")
async def github_webhook(guild_id: str, request: Request) -> Response:
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

    db = await get_db()
    try:
        secret_res = await db.execute(select(Guild.webhook_secret).where(Guild.id == guild_id))
        secret = secret_res.scalar_one_or_none()
        if not secret:
            # Guild missing or no secret configured. Don't leak which is which.
            raise HTTPException(status_code=404, detail="Webhook not configured")

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
            return Response(status_code=204)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")

        action = payload.get("action") if isinstance(payload.get("action"), str) else None
        pr_number, pr_url, repo = _extract_pr_info(payload)
        sender = payload.get("sender") or {}
        sender_login = sender.get("login") if isinstance(sender, dict) else None

        task_row = await _find_task(db, guild_id, repo, pr_number)
        task_id = task_row.id if task_row else None
        task_user_id = task_row.user_id if task_row else None

        # Trim the persisted payload so a runaway diff hunk can't balloon the DB.
        body_text = body.decode("utf-8", errors="replace")
        if len(body_text) > _MAX_PAYLOAD_BYTES:
            body_text = body_text[:_MAX_PAYLOAD_BYTES] + "\n…[truncated]"

        created_at = datetime.now(UTC).isoformat()
        stmt = (
            sqlite_insert(GithubEvent)
            .values(
                guild_id=guild_id,
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
            result = await db.execute(stmt)
        except IntegrityError:
            await db.rollback()
            result = None
        await db.commit()

        # ``rowcount`` is 0 when ON CONFLICT DO NOTHING fired — i.e. GitHub
        # redelivered an event we already accepted. Return 202 so GitHub stops
        # retrying without re-triggering downstream side effects.
        is_duplicate = result is None or (result.rowcount or 0) == 0
        if is_duplicate:
            logger.info(
                "github webhook duplicate delivery guild=%s delivery=%s event=%s",
                guild_id,
                delivery_id,
                event_type,
            )
            return Response(status_code=202)

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
    finally:
        await db.close()

    await broadcast(
        guild_id,
        {
            "type": "github-event",
            "deliveryId": delivery_id,
            "event": event_type,
            "action": action,
            "repo": repo,
            "prNumber": pr_number,
            "prUrl": pr_url,
            "taskId": task_id,
            "sender": sender_login,
        },
    )

    dispatch, skip_reason = _should_dispatch_to_foreman(
        event_type, action, payload, sender_login, task_id
    )
    if dispatch:
        summary = _build_foreman_summary(
            event_type, action, payload, repo, pr_number, task_id, sender_login
        )
        spawn(
            run_foreman_ai(guild_id, summary, user_id=task_user_id),
            name=f"foreman.github-event:{delivery_id}",
        )
        reset_foreman_poll(guild_id)
    else:
        logger.info(
            "github webhook skip-foreman guild=%s delivery=%s event=%s reason=%s",
            guild_id,
            delivery_id,
            event_type,
            skip_reason,
        )

    return Response(status_code=202)
