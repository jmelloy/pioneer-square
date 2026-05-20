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
from datetime import UTC, datetime

from database import get_db
from events import broadcast
from fastapi import APIRouter, HTTPException, Request, Response
from models import GithubEvent, Guild, Message, Task
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from util.tasks import spawn

from foreman import reset_foreman_poll, run_foreman_ai

logger = logging.getLogger(__name__)

router = APIRouter()

# How long (seconds) to wait for further events on the same PR before
# delivering the buffered batch to the foreman.  GitHub often sends several
# check_run completions and a review comment within a second of each other;
# this window lets them coalesce into a single foreman invocation.
DEBOUNCE_WINDOW_SECONDS: float = 3.0

# Per-key debounce state.  Key format: "{guild_id}:{task_id}".
# _debounce_buffers accumulates (summary, user_id) pairs until the timer fires.
# _debounce_tasks holds the in-flight asyncio Task for each key.
_debounce_buffers: dict[str, list[tuple[str, str | None]]] = {}
_debounce_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]


# Cap on stored payloads. GitHub webhook payloads are typically <50 KB but
# review-comment diff hunks can balloon — 64 KB is a safe upper bound that
# keeps the row size manageable while preserving enough for foreman context.
_MAX_PAYLOAD_BYTES = 64 * 1024

# Event types that are *expected* to come from bots (``[bot]`` suffix in
# sender.login). For these the bot filter is bypassed because bots drive
# most CI/automation traffic — filtering them out would silence the very
# events we want to react to.
_BOT_OK_EVENTS = frozenset({"check_run", "check_suite", "status"})


def _debounce_key(guild_id: str, task_id: str) -> str:
    return f"{guild_id}:{task_id}"


async def _debounce_fire(key: str, guild_id: str) -> None:
    """Sleep for the debounce window then deliver all buffered events.

    If this task is cancelled before the window expires (because a new event
    arrived and reset the timer), the CancelledError propagates normally and
    nothing is delivered — the replacement timer carries the full buffer.
    """
    await asyncio.sleep(DEBOUNCE_WINDOW_SECONDS)
    items = _debounce_buffers.pop(key, [])
    _debounce_tasks.pop(key, None)
    if not items:
        return
    summaries = [s for s, _ in items]
    combined = "\n\n---\n\n".join(summaries) if len(summaries) > 1 else summaries[0]
    user_id = items[-1][1]
    spawn(
        run_foreman_ai(guild_id, combined, user_id=user_id),
        name=f"foreman.github-debounced:{key}",
    )
    reset_foreman_poll(guild_id)
    logger.info(
        "github webhook debounce fired key=%s events=%d guild=%s",
        key,
        len(items),
        guild_id,
    )


def _schedule_debounced_foreman(
    key: str,
    guild_id: str,
    summary: str,
    user_id: str | None,
) -> None:
    """Buffer *summary* and (re)start the per-PR debounce timer.

    Each call resets the window so the foreman is only invoked after
    DEBOUNCE_WINDOW_SECONDS of silence on this PR.
    """
    existing = _debounce_tasks.pop(key, None)
    if existing and not existing.done():
        existing.cancel()
    _debounce_buffers.setdefault(key, []).append((summary, user_id))
    _debounce_tasks[key] = spawn(
        _debounce_fire(key, guild_id),
        name=f"foreman.debounce:{key}",
    )
    logger.info(
        "github webhook debounce scheduled key=%s buffered=%d",
        key,
        len(_debounce_buffers[key]),
    )


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


async def _find_task(db, guild_pk: int, repo: str | None, pr_number: int | None):
    """Match a webhook to one of this guild's tasks by (repo, pr_number).

    Returns the task row (id + user_id) or ``None``. The user_id is needed so
    foreman dispatch routes back to the user who originally created the task.
    """
    if not repo or pr_number is None:
        return None
    res = await db.execute(
        select(Task.id, Task.user_id)
        .where(
            Task.guild_pk == guild_pk,
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
    if event_type == "pull_request" and action == "closed":
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

    logger.info(
        "github webhook received guild=%s delivery=%s event=%s",
        guild_id,
        delivery_id,
        event_type,
    )

    db = await get_db()
    try:
        guild_res = await db.execute(
            select(Guild.webhook_secret, Guild.id).where(Guild.guild_id == guild_id)
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

        created_at = datetime.now(UTC).isoformat()
        stmt = (
            sqlite_insert(GithubEvent)
            .values(
                guild_pk=guild_pk,
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

        # Back-fill pr_url on the task from the webhook payload when a
        # pull_request event arrives and the task doesn't already have it set.
        # This is a safety net for the (rare) case where the worker's
        # task-update message was dropped before pr_url was persisted.
        if task_id and pr_url and event_type == "pull_request":
            await db.execute(
                update(Task).where(Task.id == task_id, Task.pr_url.is_(None)).values(pr_url=pr_url)
            )
            await db.commit()

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

    chat_line = _build_chat_line(event_type, action, repo, pr_number, payload, task_id)
    chat_now = datetime.now(UTC).isoformat()
    await broadcast(
        guild_id,
        {
            "type": "chat",
            "from": "github",
            "to": "foreman",
            "content": chat_line,
            "createdAt": chat_now,
        },
    )
    msg_db = await get_db()
    try:
        msg_db.add(
            Message(
                guild_pk=guild_pk,
                from_agent="github",
                to_agent="foreman",
                content=chat_line,
                message_type="chat",
                created_at=chat_now,
            )
        )
        await msg_db.commit()
    finally:
        await msg_db.close()

    dispatch, skip_reason = _should_dispatch_to_foreman(
        event_type, action, payload, sender_login, task_id
    )
    if dispatch:
        summary = _build_foreman_summary(
            event_type, action, payload, repo, pr_number, task_id, sender_login
        )
        key = _debounce_key(guild_id, task_id)
        _schedule_debounced_foreman(key, guild_id, summary, task_user_id)
    else:
        logger.info(
            "github webhook skip-foreman guild=%s delivery=%s event=%s reason=%s",
            guild_id,
            delivery_id,
            event_type,
            skip_reason,
        )

    return Response(status_code=202)
