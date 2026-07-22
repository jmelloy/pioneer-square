"""A2A protocol client for the Foreman.

A2AClient — discovers remote agents via their agent card, negotiates auth
(DNSid challenge-response if declared), and dispatches tasks via the
message/stream SSE endpoint.

Typical usage:
    client = A2AClient("https://agent.meyers.life/.well-known/agent.json")
    result = await client.review_pr(
        pr_url,
        caller_domain=_guild_caller_domain(guild_id),
        private_key_pem=os.environ.get("GUILD_PRIVATE_KEY_PEM"),
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any
from urllib.parse import urlparse

from foreman.auth import A2AAuthScheme, DNSidAuthScheme

logger = logging.getLogger(__name__)

_JSONRPC_VERSION = "2.0"
_DEFAULT_TIMEOUT_SECS = 180.0  # reviews can take ~2 min
_MAX_SSE_STREAM_BYTES = 32 * 1024 * 1024  # 32 MiB


def _fetch_json(url: str, *, data: bytes | None = None, headers: dict | None = None) -> Any:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT_SECS) as resp:
        return json.loads(resp.read())


def _read_sse_events(
    url: str,
    *,
    data: bytes,
    headers: dict,
) -> list[dict]:
    """POST to url, read SSE (text/event-stream) response, return all parsed events."""
    req = urllib.request.Request(url, data=data, headers=headers)
    events: list[dict] = []
    buf: list[str] = []
    stream_bytes = 0

    with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT_SECS) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            stream_bytes += len(line) + 1
            if stream_bytes > _MAX_SSE_STREAM_BYTES:
                logger.warning("a2a SSE stream exceeded byte cap, truncating")
                break

            if line == "":
                if buf:
                    payload = "\n".join(buf)
                    buf = []
                    try:
                        events.append(json.loads(payload))
                    except json.JSONDecodeError:
                        logger.debug("a2a SSE non-JSON frame: %.200s", payload)
            elif line.startswith("data:"):
                buf.append(line[5:].lstrip(" "))
            # event:/id:/retry:/comment lines silently ignored

    if buf:
        payload = "\n".join(buf)
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            pass

    return events


def _guild_caller_domain(
    guild_id: str,
    base_domain: str | None = None,
) -> str:
    """Return the DNS identity for a guild (e.g. abc123.pioneer-square.melloy.life)."""
    domain = base_domain or os.environ.get("A2A_BASE_DOMAIN", "pioneer-square.melloy.life")
    return f"{guild_id}.{domain}"


class A2AClient:
    """Client for A2A-protocol agents.

    Fetches the remote agent card, negotiates authentication, and sends tasks
    via the message/stream SSE endpoint.
    """

    def __init__(
        self,
        agent_card_url: str,
        auth_scheme: A2AAuthScheme | None = None,
    ) -> None:
        self._card_url = agent_card_url
        self._card: dict | None = None
        self._auth_scheme = auth_scheme

    async def fetch_agent_card(self) -> dict:
        if self._card is None:
            from foreman.dnsid_identity import get_dnsid_runtime

            runtime = get_dnsid_runtime()
            hostname = urlparse(self._card_url).hostname
            if runtime is not None and hostname:
                verified = await asyncio.to_thread(runtime.manager.verify_domain, hostname)
                if verified.cached_state() != "ACTIVE":
                    raise RuntimeError(f"target DNSid {hostname} is not ACTIVE")
            self._card = await asyncio.to_thread(_fetch_json, self._card_url)
        return self._card

    def _agent_base_url(self, card: dict) -> str:
        """Derive the operational base URL, falling back to the card URL's origin
        if the declared url is loopback or missing."""
        declared = card.get("url", "")
        interfaces = card.get("supportedInterfaces", [])
        if not declared and interfaces and isinstance(interfaces[0], dict):
            declared = interfaces[0].get("url", "")
        if declared and "127.0.0.1" not in declared and "localhost" not in declared:
            return declared.rstrip("/")
        parsed = urlparse(self._card_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _resolve_auth_scheme(
        self,
        card: dict,
        caller_domain: str,
        private_key_pem: str | None = None,
    ) -> A2AAuthScheme | None:
        """Instantiate the first supported auth scheme declared in the card."""
        schemes: dict = card.get("securitySchemes", {})
        security: list = card.get("security", [])
        required = {k for entry in security for k in entry}
        for name in required:
            spec = schemes.get(name, {})
            if spec.get("scheme", "").upper() == "DNSID":
                if not private_key_pem:
                    raise ValueError(
                        f"Agent {self._card_url} requires DNSid auth but no private key is available for guild"
                    )
                # purpose is a protocol constant, not the skill id
                service_domain = spec.get("serviceDomain")
                purpose = "a2a-pr-review"
                return DNSidAuthScheme(
                    caller_domain=caller_domain,
                    private_key_pem=private_key_pem,
                    purpose=purpose,
                    service_domain=service_domain,
                )
        return None

    async def send_task(
        self,
        skill_id: str,
        message: dict,
        *,
        caller_domain: str | None = None,
        private_key_pem: str | None = None,
    ) -> dict:
        """Send a task via message/stream and return collected artifacts.

        Auth token (if needed) is placed in the JSON-RPC params as
        `authorization: "DNSid <jwt>"` per the protocol spec.
        """
        card = await self.fetch_agent_card()
        base_url = self._agent_base_url(card)

        from foreman.dnsid_identity import get_dnsid_runtime

        dnsid_runtime = get_dnsid_runtime()

        logger.debug(
            "a2a send_task: fetched agent card from %s base_url=%s",
            self._card_url,
            base_url,
        )
        auth = self._auth_scheme
        if dnsid_runtime is None and auth is None and caller_domain:
            auth = self._resolve_auth_scheme(card, caller_domain, private_key_pem)

        # Resolve auth token — challenge-response runs here.
        auth_token: str | None = None
        if auth is not None:
            auth_headers = await auth.get_auth_headers(base_url)
            raw = auth_headers.get("Authorization", "")
            auth_token = raw  # e.g. "DNSid <jwt>"

        message_id = str(uuid.uuid4())
        params: dict[str, Any] = {
            "skill": skill_id,
            "message": {
                "messageId": message_id,
                "role": "ROLE_USER",
                "parts": message.get("parts", []),
                "metadata": {"skill": skill_id},
            },
        }
        if auth_token:
            params["authorization"] = auth_token

        body = json.dumps(
            {
                "jsonrpc": _JSONRPC_VERSION,
                "method": "message/send" if dnsid_runtime is not None else "message/stream",
                "params": params,
                "id": 1,
            }
        ).encode()

        task_url = base_url + ("/" if dnsid_runtime is not None else "/jsonrpc")
        logger.debug(
            "a2a send_task: url=%s method=POST skill_id=%s payload=%.500s",
            task_url,
            skill_id,
            body.decode(),
        )

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json" if dnsid_runtime is not None else "text/event-stream",
        }

        if dnsid_runtime is not None:
            from dnsid.models import HttpRequest, HttpSigningOptions
            from foreman.dnsid_identity import (
                A2A_VERSION,
                DNSID_A2A_EXTENSION_URI,
                DNSID_A2A_SIGNATURE_TAG,
            )

            headers["a2a-version"] = A2A_VERSION
            headers["a2a-extensions"] = DNSID_A2A_EXTENSION_URI
            signed = await asyncio.to_thread(
                dnsid_runtime.http_signatures.create_signed_http_request,
                HttpRequest(method="POST", url=task_url, headers=headers, body=body),
                HttpSigningOptions(
                    additional_components=[
                        "content-type",
                        "a2a-version",
                        "a2a-extensions",
                    ],
                    tag=DNSID_A2A_SIGNATURE_TAG,
                ),
            )
            headers = signed.headers

        t0 = time.monotonic()
        try:
            if dnsid_runtime is not None:
                response = await asyncio.to_thread(
                    _fetch_json, task_url, data=body, headers=headers
                )
                if "error" in response:
                    raise RuntimeError(response["error"])
                return response.get("result", response)
            events = await asyncio.to_thread(_read_sse_events, task_url, data=body, headers=headers)
        except urllib.error.HTTPError as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            try:
                err_body = exc.read().decode(errors="replace")
            except Exception:
                err_body = ""
            logger.error(
                "a2a send_task: url=%s status=%d elapsed_ms=%d err_body=%.500s",
                task_url,
                exc.code,
                elapsed_ms,
                err_body,
                exc_info=True,
            )
            raise
        except Exception:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.error(
                "a2a send_task: url=%s elapsed_ms=%d request_failed",
                task_url,
                elapsed_ms,
                exc_info=True,
            )
            raise

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "a2a send_task: url=%s status=200 elapsed_ms=%d events=%d",
            task_url,
            elapsed_ms,
            len(events),
        )
        logger.debug("a2a send_task: events=%.500s", str(events))

        return _assemble_result(events)

    async def review_pr(
        self,
        pr_url: str,
        *,
        caller_domain: str | None = None,
        private_key_pem: str | None = None,
    ) -> dict:
        """Submit a GitHub PR URL to the pr-review skill and return the task result."""
        return await self.send_task(
            "pr-review",
            {"parts": [{"type": "github-pr-url", "url": pr_url}]},
            caller_domain=caller_domain,
            private_key_pem=private_key_pem,
        )


def _assemble_result(events: list[dict]) -> dict:
    """Collect SSE events into a task result dict compatible with the existing foreman tools."""
    artifacts: list[dict] = []
    terminal_state: str | None = None
    error_code: str | None = None
    message: str | None = None

    for event in events:
        kind = event.get("kind")
        if kind == "status-update":
            state = event.get("state")
            if state in ("completed", "failed", "rejected"):
                terminal_state = state
                error_code = event.get("error_code")
                message = event.get("message")
        elif kind == "artifact-update":
            art = event.get("artifact")
            if art:
                artifacts.append(art)

    if terminal_state in ("failed", "rejected"):
        raise RuntimeError(f"a2a agent {terminal_state}: {error_code or message or 'no detail'}")

    return {
        "status": {"state": terminal_state or "unknown"},
        "artifacts": artifacts,
    }
