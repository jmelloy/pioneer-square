"""GitHub webhook receiver.

GitHub posts here for every subscribed event on a repo whose webhook is
configured against ``{backend_url}/webhooks/github/{guild_id}``. The handler
verifies the HMAC-SHA256 signature against the guild's stored secret,
persists the event (idempotent on the X-GitHub-Delivery header), and
broadcasts a ``github-event`` WS message to the guild.

Foreman dispatch (Phase 2) will be wired on top of the persisted row — the
goal of this file is just to land the receiver and its persistence story.
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
from models import GithubEvent, Guild, Task
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

router = APIRouter()


# Cap on stored payloads. GitHub webhook payloads are typically <50 KB but
# review-comment diff hunks can balloon — 64 KB is a safe upper bound that
# keeps the row size manageable while preserving enough for foreman context.
_MAX_PAYLOAD_BYTES = 64 * 1024


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


async def _find_task_id(db, guild_id: str, repo: str | None, pr_number: int | None) -> str | None:
    """Match a webhook to one of this guild's tasks by (repo, pr_number)."""
    if not repo or pr_number is None:
        return None
    res = await db.execute(
        select(Task.id)
        .where(
            Task.guild_id == guild_id,
            Task.pr_repo == repo,
            Task.pr_number == pr_number,
        )
        .order_by(Task.created_at.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


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

        task_id = await _find_task_id(db, guild_id, repo, pr_number)

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
    return Response(status_code=202)
