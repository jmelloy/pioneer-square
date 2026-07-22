"""DNSid-authenticated inbound A2A JSON-RPC receiver."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from database import get_db_dep
from events import broadcast_msg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from foreman.dnsid_identity import (
    A2A_VERSION,
    DNSID_A2A_EXTENSION_URI,
    get_dnsid_runtime,
)
from models import ExternalA2ARequest, Guild, Message, Task
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from ws_types import ChatMsg

router = APIRouter()
logger = logging.getLogger(__name__)

_MAX_REQUEST_BYTES = 1024 * 1024
_SUPPORTED_METHODS = frozenset({"message/send", "message/stream", "tasks/send", "tasks/get"})
_SUPPORTED_SKILLS = frozenset({"foreman", "pr-review"})


def _error(request_id: object, code: int, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status_code=status,
    )


async def _bounded_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_REQUEST_BYTES:
                raise ValueError("A2A request body exceeds 1 MiB")
        except ValueError as exc:
            if "exceeds" in str(exc):
                raise
            raise ValueError("invalid Content-Length") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_REQUEST_BYTES:
            raise ValueError("A2A request body exceeds 1 MiB")
        chunks.append(chunk)
    return b"".join(chunks)


def _request_parts(params: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    message = params.get("message")
    if not isinstance(message, dict):
        message = params
    parts = message.get("parts", [])
    return message, parts if isinstance(parts, list) else []


def _part_text(part: object) -> str:
    if not isinstance(part, dict):
        return ""
    if isinstance(part.get("text"), str):
        return part["text"]
    content = part.get("content")
    if isinstance(content, dict):
        value = content.get("value")
        if isinstance(value, str) and content.get("$case") == "text":
            return value
    return ""


def _parse_work(params: dict[str, Any]) -> tuple[str, str, str]:
    message, parts = _request_parts(params)
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    skill = params.get("skill") or metadata.get("skill") or "foreman"
    if skill not in _SUPPORTED_SKILLS:
        raise ValueError(f"unsupported skill: {skill!r}")

    texts = [text for part in parts if (text := _part_text(part))]
    pr_urls = [
        part.get("url")
        for part in parts
        if isinstance(part, dict)
        and part.get("type") == "github-pr-url"
        and isinstance(part.get("url"), str)
    ]
    if skill == "pr-review":
        pr_url = pr_urls[0] if pr_urls else ""
        if not pr_url.startswith("https://github.com/") or "/pull/" not in pr_url:
            raise ValueError("pr-review requires a https://github.com/.../pull/... part")
        instructions = "\n".join(texts).strip()
        content = f"Review this pull request: {pr_url}"
        if instructions:
            content += f"\n\nExternal reviewer instructions:\n{instructions}"
    else:
        content = "\n".join(texts).strip()
        if not content:
            raise ValueError("foreman requires a non-empty text part")

    external_message_id = (
        message.get("messageId") or message.get("message_id") or params.get("id") or ""
    )
    if not isinstance(external_message_id, str) or not external_message_id.strip():
        raise ValueError("A2A messageId is required")
    return str(skill), content, external_message_id


async def _get_task_result(
    db: AsyncSession,
    guild: Guild,
    sender: str,
    params: dict[str, Any],
    request_id: object,
) -> JSONResponse:
    external_message_id = params.get("id") or params.get("taskId") or params.get("task_id")
    if not isinstance(external_message_id, str) or not external_message_id:
        return _error(request_id, -32602, "tasks/get requires id", 400)

    audit = (
        await db.exec(
            select(ExternalA2ARequest).where(
                col(ExternalA2ARequest.guild_id) == guild.id,
                col(ExternalA2ARequest.sender_fqdn) == sender,
                col(ExternalA2ARequest.external_message_id) == external_message_id,
            )
        )
    ).one_or_none()
    if audit is None:
        return _error(request_id, -32004, "A2A task not found", 404)

    task = None
    if audit.task_id:
        task = (
            await db.exec(
                select(Task).where(
                    col(Task.id) == audit.task_id,
                    col(Task.guild_id) == guild.id,
                )
            )
        ).one_or_none()
    if task is None:
        task = (
            await db.exec(
                select(Task)
                .where(
                    col(Task.guild_id) == guild.id,
                    col(Task.user_id) == sender,
                    col(Task.created_at) >= audit.verified_at,
                )
                .order_by(col(Task.created_at).asc())
                .limit(1)
            )
        ).one_or_none()
        if task is not None:
            audit.task_id = task.id

    state_map = {
        "done": "completed",
        "failed": "failed",
        "cancelled": "failed",
        "pending": "submitted",
        "working": "working",
        "awaiting-review": "working",
    }
    state = state_map.get(task.state if task else "", "submitted")
    audit.state = state
    artifacts: list[dict[str, Any]] = []
    response_message = (
        await db.exec(
            select(Message)
            .where(
                col(Message.guild_id) == guild.id,
                col(Message.from_agent) == "foreman",
                col(Message.user_id) == sender,
                col(Message.created_at) >= audit.verified_at,
            )
            .order_by(col(Message.created_at).desc())
            .limit(1)
        )
    ).one_or_none()
    if response_message is not None:
        artifacts.append(
            {
                "artifactId": f"review-{audit.id}",
                "name": "Foreman response",
                "parts": [{"text": response_message.content, "mediaType": "text/markdown"}],
            }
        )
    if task is not None and task.pr_url:
        artifacts.append(
            {
                "artifactId": f"pr-{task.id}",
                "name": "Pull request",
                "parts": [{"url": task.pr_url, "mediaType": "text/uri-list"}],
            }
        )
    await db.commit()
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "id": external_message_id,
                "contextId": external_message_id,
                "status": {"state": state},
                "artifacts": artifacts,
                **({"metadata": {"pioneerTaskId": task.id}} if task is not None else {}),
            },
        }
    )


async def _handle_a2a(request: Request, db: AsyncSession) -> JSONResponse:
    runtime = get_dnsid_runtime()
    if runtime is None:
        return _error(None, -32001, "DNSid A2A receiver is not configured", 404)

    try:
        body = await _bounded_body(request)
    except ValueError as exc:
        return _error(None, -32600, str(exc), 413)

    request_id: object = None
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("JSON-RPC body must be an object")
        request_id = payload.get("id")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return _error(request_id, -32700, f"invalid JSON: {exc}", 400)

    if payload.get("jsonrpc") != "2.0":
        return _error(request_id, -32600, "jsonrpc must be '2.0'", 400)

    if request.headers.get("a2a-version", "") != A2A_VERSION:
        return _error(request_id, -32600, f"A2A-Version must be {A2A_VERSION}", 400)
    extensions = {
        item.strip()
        for item in request.headers.get("a2a-extensions", "").split(",")
        if item.strip()
    }
    if DNSID_A2A_EXTENSION_URI not in extensions:
        return _error(request_id, -32600, "required DNSid A2A extension is missing", 400)

    url = runtime.public_url(request.url.path, request.url.query)
    try:
        verified = await asyncio.to_thread(
            runtime.verify_request,
            request.method,
            url,
            {key.lower(): value for key, value in request.headers.items()},
            body,
        )
    except Exception as exc:
        logger.info("Rejected DNSid A2A signature: %s", exc)
        return _error(request_id, -32001, "DNSid authentication failed", 401)

    sender = verified.domain.rstrip(".").lower()
    if verified.cached_state() != "ACTIVE":
        return _error(request_id, -32001, "DNSid principal is not ACTIVE", 401)
    if sender not in runtime.allowlist:
        return _error(request_id, -32003, f"DNSid principal {sender} is not authorized", 403)

    method = payload.get("method")
    if method not in _SUPPORTED_METHODS:
        return _error(request_id, -32601, f"unsupported A2A method: {method!r}", 400)
    params = payload.get("params")
    if not isinstance(params, dict):
        return _error(request_id, -32602, "params must be an object", 400)

    guild = (
        await db.exec(select(Guild).where(col(Guild.slug) == runtime.guild_slug))
    ).one_or_none()
    if guild is None or guild.id is None:
        return _error(request_id, -32004, "configured Pioneer guild was not found", 404)
    if method == "tasks/get":
        return await _get_task_result(db, guild, sender, params, request_id)

    try:
        skill, content, external_message_id = _parse_work(params)
    except ValueError as exc:
        return _error(request_id, -32602, str(exc), 400)

    replay_material = "\n".join(
        [request.headers.get("signature-input", ""), request.headers.get("signature", "")]
    )
    replay_key = hashlib.sha256(replay_material.encode()).hexdigest()
    audit = {
        "external_message_id": external_message_id,
        "sender_fqdn": sender,
        "governance_id": verified.record.gi,
        "dnssec_state": verified.dnssec_state.name,
        "dnsid_status": verified.cached_state(),
        "verified_at": verified.verified_at.isoformat(),
        "skill": skill,
    }
    created_at = datetime.now(UTC)
    message = Message(
        guild_id=guild.id,
        from_agent=sender,
        to_agent="foreman",
        content=content,
        message_type="chat",
        created_at=created_at,
        user_id=sender,
        source="a2a",
        meta=json.dumps({"dnsid": audit}, separators=(",", ":")),
    )
    db.add(message)
    await db.flush()
    db.add(
        ExternalA2ARequest(
            guild_id=guild.id,
            replay_key=replay_key,
            external_message_id=external_message_id,
            sender_fqdn=sender,
            governance_id=verified.record.gi,
            dnssec_state=verified.dnssec_state.name,
            dnsid_status=verified.cached_state(),
            verified_at=verified.verified_at,
            skill=skill,
            message_id=message.id,
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _error(request_id, -32001, "replayed A2A request", 401)

    await broadcast_msg(
        runtime.guild_slug,
        ChatMsg(
            from_=sender,
            to="foreman",
            content=content,
            createdAt=created_at.isoformat(),
            userId=sender,
            source="a2a",
        ),
    )
    from ws_handlers import _trigger_foreman

    await _trigger_foreman(
        runtime.guild_slug,
        "chat",
        content,
        user_id=sender,
        task_name=f"foreman.a2a:{runtime.guild_slug}:{external_message_id}",
    )

    result_message_id = str(uuid.uuid4())
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "messageId": result_message_id,
                "contextId": external_message_id,
                "role": "ROLE_AGENT",
                "parts": [
                    {
                        "text": f"Accepted {skill} request from verified DNSid {sender}",
                        "mediaType": "text/plain",
                    }
                ],
            },
        }
    )


@router.post("/")
async def a2a_root(request: Request, db: AsyncSession = Depends(get_db_dep)) -> JSONResponse:
    return await _handle_a2a(request, db)


@router.post("/jsonrpc")
async def a2a_jsonrpc(request: Request, db: AsyncSession = Depends(get_db_dep)) -> JSONResponse:
    """Compatibility route for existing Pioneer and pre-1.0 A2A clients."""
    return await _handle_a2a(request, db)
