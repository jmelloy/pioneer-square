"""MCP and A2A protocol clients for the Foreman.

MCPClient — lightweight async MCP client.
Supports two transports:
- **HTTP**: POSTs JSON-RPC 2.0 to an HTTP endpoint.
  Configure via ``REVIEWER_MCP_URL`` env var (default: ``https://agent.meyers.life``).
- **stdio**: launches a subprocess and speaks JSON-RPC 2.0 over stdin/stdout.
  Configure via ``REVIEWER_MCP_CMD`` env var (e.g. ``"crv-mcp"``).
HTTP takes precedence when both are configured.

A2AClient — client for A2A-protocol agents.
Handles agent card discovery, authentication scheme negotiation (including
DNSid challenge-response), and task dispatch to A2A-compatible agents.

Typical usage:
    client = A2AClient("https://agent.meyers.life/.well-known/agent.json")
    result = await client.review_pr(
        pr_url,
        caller_domain=_guild_caller_domain(guild_id),
        private_key_pem=guild_key.private_key_pem,
        key_id=guild_key.key_id,
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
import uuid
from typing import Any
from urllib.parse import urlparse

from foreman.auth import A2AAuthScheme, DNSidAuthScheme

logger = logging.getLogger(__name__)

_JSONRPC_VERSION = "2.0"
_MCP_PROTOCOL_VERSION = "2024-11-05"
_DEFAULT_TIMEOUT_SECS = 180.0  # code-review-agent reviews can take ~2 min
_DEFAULT_MCP_URL = "https://agent.meyers.life"


# ---------------------------------------------------------------------------
# MCP client
# ---------------------------------------------------------------------------


class MCPError(Exception):
    """Raised when an MCP server returns a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data=None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"MCP error {code}: {message}")


class MCPClient:
    """Minimal async MCP client that the Foreman uses to call external agents."""

    def __init__(
        self,
        cmd: str | list[str] | None = None,
        url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECS,
    ):
        self._cmd = cmd if cmd is not None else os.environ.get("REVIEWER_MCP_CMD")
        _explicit_url = url if url is not None else os.environ.get("REVIEWER_MCP_URL")
        # Default to the shared MCP server only when no transport is explicitly configured.
        self._url = _explicit_url if _explicit_url else (None if self._cmd else _DEFAULT_MCP_URL)
        self._timeout = timeout

    async def list_tools(self) -> list[dict]:
        """Return the tools exposed by the MCP server."""
        result = await self._request("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """Call a named MCP tool and return the result dict."""
        return await self._request("tools/call", {"name": name, "arguments": arguments})

    # ------------------------------------------------------------------
    # Transport helpers
    # ------------------------------------------------------------------

    async def _request(self, method: str, params: dict) -> dict:
        if self._url:
            return await self._http_request(method, params)
        return await self._stdio_request(method, params)

    async def _http_request(self, method: str, params: dict) -> dict:
        """JSON-RPC 2.0 over HTTP (streamable-HTTP MCP transport)."""
        import urllib.error

        req_id = str(uuid.uuid4())
        payload = json.dumps(
            {"jsonrpc": _JSONRPC_VERSION, "id": req_id, "method": method, "params": params}
        ).encode()

        def _do_post():
            req = urllib.request.Request(
                self._url,
                data=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                raise MCPError(-32000, f"HTTP {exc.code}: {exc.reason}") from exc

        data = await asyncio.to_thread(_do_post)
        if "error" in data:
            err = data["error"]
            raise MCPError(err.get("code", -1), err.get("message", "unknown"), err.get("data"))
        return data.get("result", {})

    async def _stdio_request(self, method: str, params: dict) -> dict:
        """JSON-RPC 2.0 over a stdio subprocess (MCP stdio transport).

        Performs the full MCP handshake (initialize → notifications/initialized)
        before sending the actual request, then reads stdout until the matching
        response arrives.  Progress notifications are logged and skipped.
        """
        cmd = self._cmd
        if isinstance(cmd, str):
            cmd = cmd.split()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        req_id = str(uuid.uuid4())
        try:
            # Write the full MCP session in one shot:
            # 1. initialize request
            # 2. notifications/initialized notification
            # 3. actual request
            messages = (
                json.dumps(
                    {
                        "jsonrpc": _JSONRPC_VERSION,
                        "id": "mcp-init",
                        "method": "initialize",
                        "params": {
                            "protocolVersion": _MCP_PROTOCOL_VERSION,
                            "clientInfo": {
                                "name": "pioneer-square-foreman",
                                "version": "1.0",
                            },
                            "capabilities": {},
                        },
                    }
                )
                + "\n"
                + json.dumps({"jsonrpc": _JSONRPC_VERSION, "method": "notifications/initialized"})
                + "\n"
                + json.dumps(
                    {
                        "jsonrpc": _JSONRPC_VERSION,
                        "id": req_id,
                        "method": method,
                        "params": params,
                    }
                )
                + "\n"
            )
            proc.stdin.write(messages.encode())
            await proc.stdin.drain()
            proc.stdin.close()

            deadline = asyncio.get_event_loop().time() + self._timeout
            while True:
                remaining = max(0.5, deadline - asyncio.get_event_loop().time())
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                except TimeoutError:
                    raise TimeoutError(
                        f"MCP {method!r} timed out after {self._timeout:.0f}s"
                    ) from None

                if not line:
                    stderr_out = b""
                    try:
                        stderr_out = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
                    except TimeoutError:
                        pass
                    raise RuntimeError(
                        "MCP server closed stdout unexpectedly. "
                        f"stderr: {stderr_out.decode(errors='replace')[:400]}"
                    )

                try:
                    data = json.loads(line.decode(errors="replace"))
                except json.JSONDecodeError:
                    logger.debug("MCP non-JSON line: %s", line[:120])
                    continue

                msg_id = data.get("id")
                if msg_id == req_id:
                    if "error" in data:
                        err = data["error"]
                        raise MCPError(
                            err.get("code", -1),
                            err.get("message", "unknown"),
                            err.get("data"),
                        )
                    return data.get("result", {})

                # Skip the init handshake response and all notifications
                if "method" in data:
                    logger.debug("MCP notification: %s", data.get("method"))
                elif msg_id == "mcp-init":
                    logger.debug("MCP initialize handshake complete")
                else:
                    logger.debug("MCP unexpected message id=%s", msg_id)

        finally:
            try:
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()


# ---------------------------------------------------------------------------
# A2A client
# ---------------------------------------------------------------------------


def _fetch_json(url: str, *, data: bytes | None = None, headers: dict | None = None) -> Any:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=15) as resp:
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
        private_key_pem: str,
        key_id: str,
    ) -> A2AAuthScheme | None:
        """Instantiate the first supported auth scheme declared in the card."""
        schemes: dict = card.get("securitySchemes", {})
        security: list = card.get("security", [])
        required = {k for entry in security for k in entry}
        for name in required:
            spec = schemes.get(name, {})
            if spec.get("scheme", "").upper() == "DNSID":
                # Use the first skill id as purpose, falling back to the default
                skills = card.get("skills", [])
                purpose = skills[0].get("id", "a2a-pr-review") if skills else "a2a-pr-review"
                return DNSidAuthScheme(
                    caller_domain=caller_domain,
                    private_key_pem=private_key_pem,
                    key_id=key_id,
                    purpose=purpose,
                )
        return None

    async def review_pr(
        self,
        pr_url: str,
        *,
        caller_domain: str | None = None,
        private_key_pem: str | None = None,
        key_id: str | None = None,
    ) -> dict:
        """Submit a GitHub PR URL to a code-review-agent skill and return the result."""
        card = await self.fetch_agent_card()
        base_url = self._agent_base_url(card)

        auth = self._auth_scheme
        if auth is None and caller_domain and private_key_pem and key_id:
            auth = self._resolve_auth_scheme(card, caller_domain, private_key_pem, key_id)

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth is not None:
            auth_headers = await auth.get_auth_headers(base_url)
            headers.update(auth_headers)

        task_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tasks/send",
                "params": {
                    "skill_id": "pr-review",
                    "message": {"parts": [{"type": "github-pr-url", "text": pr_url}]},
                },
                "id": 1,
            }
        ).encode()

        result = await asyncio.to_thread(
            _fetch_json,
            f"{base_url}/a2a",
            data=task_body,
            headers=headers,
        )
        return result.get("result", result)
