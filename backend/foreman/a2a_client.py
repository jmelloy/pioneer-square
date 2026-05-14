"""A2A protocol client for the Foreman.

A2AClient — discovers remote agents via their agent card, negotiates auth
(DNSid challenge-response if declared), and dispatches tasks.

Typical usage:
    client = A2AClient("https://agent.meyers.life/.well-known/agent.json")
    result = await client.review_pr(
        pr_url,
        caller_domain=_guild_caller_domain(guild_id),
        config_path=os.environ.get("DNSID_AGENT_CONFIG"),
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
from typing import Any
from urllib.parse import urlparse

from foreman.auth import A2AAuthScheme, DNSidAuthScheme

logger = logging.getLogger(__name__)

_JSONRPC_VERSION = "2.0"
_DEFAULT_TIMEOUT_SECS = 180.0  # reviews can take ~2 min


def _fetch_json(url: str, *, data: bytes | None = None, headers: dict | None = None) -> Any:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT_SECS) as resp:
        return json.loads(resp.read())


def _guild_caller_domain(
    guild_id: str,
    base_domain: str | None = None,
) -> str:
    """Return the DNS identity for a guild (e.g. abc123.pioneer-square.melloy.life)."""
    domain = base_domain or os.environ.get("A2A_BASE_DOMAIN", "pioneer-square.melloy.life")
    return f"{guild_id}.{domain}"


class A2AClient:
    """Client for A2A-protocol agents.

    Fetches the remote agent card, negotiates authentication, and sends tasks.
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
            self._card = await asyncio.to_thread(_fetch_json, self._card_url)
        return self._card

    def _agent_base_url(self, card: dict) -> str:
        """Derive the operational base URL, falling back to the card URL's origin
        if the declared url is loopback or missing."""
        declared = card.get("url", "")
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
                skills = card.get("skills", [])
                purpose = skills[0].get("id", "a2a-pr-review") if skills else "a2a-pr-review"
                return DNSidAuthScheme(
                    caller_domain=caller_domain,
                    private_key_pem=private_key_pem,
                    purpose=purpose,
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
        """Send a task to the agent and return the raw result."""
        card = await self.fetch_agent_card()
        base_url = self._agent_base_url(card)

        logger.debug(
            "a2a send_task: fetched agent card from %s base_url=%s",
            self._card_url,
            base_url,
        )
        auth = self._auth_scheme
        if auth is None and caller_domain:
            auth = self._resolve_auth_scheme(card, caller_domain, private_key_pem)

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth is not None:
            auth_headers = await auth.get_auth_headers(base_url)
            headers.update(auth_headers)

        body = json.dumps(
            {
                "jsonrpc": _JSONRPC_VERSION,
                "method": "tasks/send",
                "params": {"skill_id": skill_id, "message": message},
                "id": 1,
            }
        ).encode()

        task_url = f"{base_url}/jsonrpc"
        log_headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
        logger.debug(
            "a2a send_task: url=%s method=POST skill_id=%s headers=%s payload=%.500s",
            task_url,
            skill_id,
            log_headers,
            body.decode(),
        )

        t0 = time.monotonic()
        try:
            result = await asyncio.to_thread(_fetch_json, task_url, data=body, headers=headers)
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
        logger.info("a2a send_task: url=%s status=200 elapsed_ms=%d", task_url, elapsed_ms)
        logger.debug("a2a send_task: response_preview=%.500s", str(result))

        return result.get("result", result)

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
            {"parts": [{"type": "github-pr-url", "text": pr_url}]},
            caller_domain=caller_domain,
            private_key_pem=private_key_pem,
        )
