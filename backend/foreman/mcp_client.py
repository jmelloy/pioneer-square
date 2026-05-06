"""Lightweight async MCP client for the Foreman.

Supports two transports:
- **stdio**: launches a subprocess and speaks JSON-RPC 2.0 over stdin/stdout.
  Configure via ``REVIEWER_MCP_CMD`` env var (e.g. ``"crv-mcp"``).
- **HTTP**: POSTs JSON-RPC 2.0 to an HTTP endpoint.
  Configure via ``REVIEWER_MCP_URL`` env var.

When both are set, HTTP takes precedence.
"""

import asyncio
import json
import logging
import os
import uuid

logger = logging.getLogger(__name__)

_JSONRPC_VERSION = "2.0"
_MCP_PROTOCOL_VERSION = "2024-11-05"
_DEFAULT_TIMEOUT_SECS = 180.0  # code-review-agent reviews can take ~2 min


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
        self._url = url if url is not None else os.environ.get("REVIEWER_MCP_URL")
        self._timeout = timeout
        if not self._cmd and not self._url:
            raise ValueError(
                "MCPClient requires REVIEWER_MCP_CMD (stdio) or REVIEWER_MCP_URL (HTTP)."
            )

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
        import urllib.request

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
