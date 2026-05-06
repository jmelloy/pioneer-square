"""Unit tests for foreman/mcp_client.py.

All tests are purely in-process — no real subprocess is launched and no real
network connection is made.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foreman.mcp_client import _DEFAULT_MCP_URL, _MCP_PROTOCOL_VERSION, MCPClient, MCPError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _make_fake_proc(stdout_lines: list[bytes], returncode: int = 0):
    """Return a MagicMock that looks like an asyncio subprocess."""
    proc = MagicMock()

    # stdin
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()
    proc.stdin.is_closing = MagicMock(return_value=False)

    # stdout — readline() returns one entry per call, then b"" (EOF)
    _lines = iter(stdout_lines + [b""])
    proc.stdout = MagicMock()
    proc.stdout.readline = AsyncMock(side_effect=lambda: next(_lines))

    # stderr
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=b"")

    # process lifecycle
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


def _init_response() -> bytes:
    return _json_line(
        {
            "jsonrpc": "2.0",
            "id": "mcp-init",
            "result": {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "serverInfo": {"name": "test-server", "version": "0.1"},
                "capabilities": {},
            },
        }
    )


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestMCPClientInit:
    def test_defaults_to_agent_meyers_life(self, monkeypatch):
        monkeypatch.delenv("REVIEWER_MCP_CMD", raising=False)
        monkeypatch.delenv("REVIEWER_MCP_URL", raising=False)
        client = MCPClient()
        assert client._url == _DEFAULT_MCP_URL
        assert client._cmd is None

    def test_uses_env_var_cmd(self, monkeypatch):
        monkeypatch.setenv("REVIEWER_MCP_CMD", "crv-mcp")
        monkeypatch.delenv("REVIEWER_MCP_URL", raising=False)
        client = MCPClient()
        assert client._cmd == "crv-mcp"
        # cmd-only config: no URL default — uses stdio transport
        assert client._url is None

    def test_uses_env_var_url(self, monkeypatch):
        monkeypatch.delenv("REVIEWER_MCP_CMD", raising=False)
        monkeypatch.setenv("REVIEWER_MCP_URL", "http://localhost:9000/mcp")
        client = MCPClient()
        assert client._url == "http://localhost:9000/mcp"
        assert client._cmd is None

    def test_explicit_params_override_env(self, monkeypatch):
        monkeypatch.setenv("REVIEWER_MCP_CMD", "from-env")
        client = MCPClient(cmd="explicit-cmd")
        assert client._cmd == "explicit-cmd"

    def test_explicit_url_overrides_default(self, monkeypatch):
        monkeypatch.delenv("REVIEWER_MCP_URL", raising=False)
        client = MCPClient(url="http://private-instance:9000")
        assert client._url == "http://private-instance:9000"

    def test_http_takes_precedence_when_both_set(self, monkeypatch):
        monkeypatch.setenv("REVIEWER_MCP_CMD", "crv-mcp")
        monkeypatch.setenv("REVIEWER_MCP_URL", "http://localhost:9000")
        client = MCPClient()
        assert client._url == "http://localhost:9000"


# ---------------------------------------------------------------------------
# Stdio transport
# ---------------------------------------------------------------------------


class TestMCPClientStdio:
    """Tests for the stdio (subprocess) transport."""

    @pytest.mark.asyncio
    async def test_list_tools_returns_tool_list(self):
        # We don't know the req_id at construction time, so build a proc that
        # scans the written bytes to find the req_id and respond accordingly.
        client = MCPClient(cmd="crv-mcp")

        def fake_exec(*args, **kwargs):
            # Capture the req_id from the written bytes by using a recording stdin.
            req_id_holder = []

            proc = MagicMock()
            written_data = []

            def _write(data):
                written_data.append(data)

            async def _drain():
                # After drain, we can inspect what was written and build the response.
                pass

            proc.stdin = MagicMock()
            proc.stdin.write = _write
            proc.stdin.drain = _drain
            proc.stdin.close = MagicMock()
            proc.stdin.is_closing = MagicMock(return_value=False)

            async def _readline():
                # First call: parse all written data to find the req_id.
                all_written = b"".join(written_data)
                lines = [l for l in all_written.split(b"\n") if l.strip()]
                req_id = None
                for line in lines:
                    try:
                        msg = json.loads(line)
                        if msg.get("method") == "tools/list":
                            req_id = msg["id"]
                    except Exception:
                        pass
                if req_id and not req_id_holder:
                    req_id_holder.append(req_id)

                # Yield init response, then the actual response
                if not hasattr(_readline, "_call_count"):
                    _readline._call_count = 0
                _readline._call_count += 1
                if _readline._call_count == 1:
                    return _init_response()
                if _readline._call_count == 2 and req_id_holder:
                    return _json_line(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id_holder[0],
                            "result": {
                                "tools": [{"name": "start_conversation", "inputSchema": {}}]
                            },
                        }
                    )
                return b""

            proc.stdout = MagicMock()
            proc.stdout.readline = _readline
            proc.stderr = MagicMock()
            proc.stderr.read = AsyncMock(return_value=b"")
            proc.wait = AsyncMock(return_value=0)
            proc.kill = MagicMock()
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            tools = await client.list_tools()

        assert isinstance(tools, list)
        assert tools[0]["name"] == "start_conversation"

    @pytest.mark.asyncio
    async def test_call_tool_happy_path(self):
        """call_tool returns the result dict from the MCP server."""
        client = MCPClient(cmd="crv-mcp")
        expected_result = {
            "content": [{"type": "text", "text": "## Review\nLooks good."}],
            "isError": False,
        }

        call_count = 0

        async def fake_readline():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _init_response()
            if call_count == 2:
                # Return a response matching whatever id was sent.
                # We don't have the id here, so we use a sentinel approach:
                # the test patches uuid4 to a known value.
                return _json_line(
                    {"jsonrpc": "2.0", "id": "test-req-id", "result": expected_result}
                )
            return b""

        fake_proc = MagicMock()
        fake_proc.stdin = MagicMock()
        fake_proc.stdin.write = MagicMock()
        fake_proc.stdin.drain = AsyncMock()
        fake_proc.stdin.close = MagicMock()
        fake_proc.stdin.is_closing = MagicMock(return_value=False)
        fake_proc.stdout = MagicMock()
        fake_proc.stdout.readline = fake_readline
        fake_proc.stderr = MagicMock()
        fake_proc.stderr.read = AsyncMock(return_value=b"")
        fake_proc.wait = AsyncMock(return_value=0)
        fake_proc.kill = MagicMock()

        with (
            patch("asyncio.create_subprocess_exec", return_value=fake_proc),
            patch("foreman.mcp_client.uuid.uuid4", return_value="test-req-id"),
        ):
            result = await client.call_tool("start_conversation", {"agent_url": "http://x"})

        assert result == expected_result

    @pytest.mark.asyncio
    async def test_raises_mcp_error_on_error_response(self):
        """An error in the JSON-RPC response raises MCPError."""
        client = MCPClient(cmd="crv-mcp")
        call_count = 0

        async def fake_readline():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _init_response()
            if call_count == 2:
                return _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": "test-req-id",
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )
            return b""

        fake_proc = _make_fake_proc([])
        fake_proc.stdout.readline = fake_readline

        with (
            patch("asyncio.create_subprocess_exec", return_value=fake_proc),
            patch("foreman.mcp_client.uuid.uuid4", return_value="test-req-id"),
        ):
            with pytest.raises(MCPError) as exc_info:
                await client.call_tool("no_such_method", {})

        assert exc_info.value.code == -32601
        assert "Method not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_runtime_error_on_eof(self):
        """EOF from the subprocess raises RuntimeError."""
        client = MCPClient(cmd="crv-mcp")
        fake_proc = _make_fake_proc([])  # stdout_lines is empty → immediate EOF

        with (
            patch("asyncio.create_subprocess_exec", return_value=fake_proc),
            patch("foreman.mcp_client.uuid.uuid4", return_value="test-req-id"),
        ):
            with pytest.raises(RuntimeError, match="closed stdout"):
                await client.call_tool("tools/list", {})

    @pytest.mark.asyncio
    async def test_skips_notifications_and_finds_response(self):
        """Progress notifications on stdout are skipped; the final response is returned."""
        client = MCPClient(cmd="crv-mcp")

        notification = _json_line(
            {"jsonrpc": "2.0", "method": "notifications/agent_progress", "params": {}}
        )
        expected_result = {"content": [{"type": "text", "text": "done"}]}

        lines = [_init_response(), notification, notification]

        call_count = [0]

        async def fake_readline():
            call_count[0] += 1
            if call_count[0] <= len(lines):
                return lines[call_count[0] - 1]
            if call_count[0] == len(lines) + 1:
                return _json_line(
                    {"jsonrpc": "2.0", "id": "test-req-id", "result": expected_result}
                )
            return b""

        fake_proc = _make_fake_proc([])
        fake_proc.stdout.readline = fake_readline

        with (
            patch("asyncio.create_subprocess_exec", return_value=fake_proc),
            patch("foreman.mcp_client.uuid.uuid4", return_value="test-req-id"),
        ):
            result = await client.call_tool("start_conversation", {})

        assert result == expected_result


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


class TestMCPClientHTTP:
    """Tests for the HTTP transport."""

    @pytest.mark.asyncio
    async def test_list_tools_via_http(self):
        """list_tools() POSTs to the URL and returns parsed tools."""
        client = MCPClient(url="http://localhost:9000/mcp")
        expected_tools = [{"name": "start_conversation", "inputSchema": {}}]
        response_body = json.dumps(
            {"jsonrpc": "2.0", "id": "test-req-id", "result": {"tools": expected_tools}}
        ).encode()

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read = MagicMock(return_value=response_body)

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch("foreman.mcp_client.uuid.uuid4", return_value="test-req-id"),
        ):
            tools = await client.list_tools()

        assert tools == expected_tools

    @pytest.mark.asyncio
    async def test_call_tool_via_http(self):
        """call_tool() POSTs tools/call and returns the result."""
        client = MCPClient(url="http://localhost:9000/mcp")
        expected_result = {"content": [{"type": "text", "text": "## Review"}]}
        response_body = json.dumps(
            {"jsonrpc": "2.0", "id": "test-req-id", "result": expected_result}
        ).encode()

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read = MagicMock(return_value=response_body)

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch("foreman.mcp_client.uuid.uuid4", return_value="test-req-id"),
        ):
            result = await client.call_tool("start_conversation", {"agent_url": "http://x"})

        assert result == expected_result

    @pytest.mark.asyncio
    async def test_http_raises_mcp_error_on_error_response(self):
        """JSON-RPC error response raises MCPError."""
        client = MCPClient(url="http://localhost:9000/mcp")
        response_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "test-req-id",
                "error": {"code": -32000, "message": "Internal error"},
            }
        ).encode()

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read = MagicMock(return_value=response_body)

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch("foreman.mcp_client.uuid.uuid4", return_value="test-req-id"),
        ):
            with pytest.raises(MCPError) as exc_info:
                await client.call_tool("bad_call", {})

        assert exc_info.value.code == -32000

    @pytest.mark.asyncio
    async def test_http_raises_mcp_error_on_http_error(self):
        """HTTP errors (4xx/5xx) raise MCPError."""
        import urllib.error

        client = MCPClient(url="http://localhost:9000/mcp")
        http_err = urllib.error.HTTPError(
            url="http://localhost:9000/mcp",
            code=503,
            msg="Service Unavailable",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=http_err):
            with pytest.raises(MCPError, match="503"):
                await client.call_tool("any", {})


# ---------------------------------------------------------------------------
# _extract_review_data helper
# ---------------------------------------------------------------------------


class TestExtractReviewData:
    def test_extracts_text_content(self):
        from foreman.tools import _extract_review_data

        result = {
            "content": [{"type": "text", "text": "## Code Review\nLooks good."}],
        }
        _event, body = _extract_review_data(result)
        assert "Looks good" in body

    def test_approved_verdict_maps_to_approve(self):
        from foreman.tools import _extract_review_data

        result = {
            "content": [{"type": "text", "text": "LGTM"}],
            "structuredContent": {
                "artifacts": [
                    {
                        "content_type": "application/vnd.code-review-agent.report+json",
                        "body": json.dumps({"summary": {"verdict": "approved"}}),
                    }
                ]
            },
        }
        event, _body = _extract_review_data(result)
        assert event == "APPROVE"

    def test_changes_requested_verdict(self):
        from foreman.tools import _extract_review_data

        result = {
            "content": [{"type": "text", "text": "Needs changes."}],
            "structuredContent": {
                "artifacts": [
                    {
                        "content_type": "application/vnd.code-review-agent.report+json",
                        "body": json.dumps({"summary": {"verdict": "changes-requested"}}),
                    }
                ]
            },
        }
        event, _body = _extract_review_data(result)
        assert event == "REQUEST_CHANGES"

    def test_unknown_verdict_defaults_to_comment(self):
        from foreman.tools import _extract_review_data

        result = {"content": [{"type": "text", "text": "Some review."}]}
        event, _body = _extract_review_data(result)
        assert event == "COMMENT"

    def test_empty_content_returns_fallback_body(self):
        from foreman.tools import _extract_review_data

        result = {}
        _event, body = _extract_review_data(result)
        assert body  # non-empty fallback

    def test_invalid_json_artifact_falls_back_to_comment(self):
        from foreman.tools import _extract_review_data

        result = {
            "content": [{"type": "text", "text": "Review text."}],
            "structuredContent": {
                "artifacts": [
                    {
                        "content_type": "application/vnd.code-review-agent.report+json",
                        "body": "not valid json {{{",
                    }
                ]
            },
        }
        event, _body = _extract_review_data(result)
        assert event == "COMMENT"
