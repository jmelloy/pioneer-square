"""Async HTTP client for Pioneer Square backend REST API calls.

The standalone foreman uses this to read guild state and write mutations
without direct database access.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class BackendError(Exception):
    """Raised when the backend returns an unexpected HTTP error."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"Backend error {status}: {detail}")
        self.status = status
        self.detail = detail


class ForemanHTTPClient:
    """Thin async httpx wrapper scoped to a guild."""

    def __init__(self, http_url: str, guild_id: str, auth_token: str | None = None):
        self._base = http_url.rstrip("/")
        self._guild_id = guild_id
        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=30.0,
        )

    # ── Internals ──────────────────────────────────────────────────────────

    def _guild_url(self, path: str) -> str:
        return f"{self._base}/guilds/{self._guild_id}{path}"

    async def _get(self, url: str, **params) -> Any:
        resp = await self._client.get(url, params=params or None)
        self._raise_for_status(resp)
        return resp.json()

    async def _post(self, url: str, body: dict) -> Any:
        resp = await self._client.post(url, json=body)
        self._raise_for_status(resp)
        return resp.json()

    async def _patch(self, url: str, body: dict) -> Any:
        resp = await self._client.patch(url, json=body)
        self._raise_for_status(resp)
        return resp.json()

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.is_success:
            return
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise BackendError(resp.status_code, detail)

    # ── State reads ────────────────────────────────────────────────────────

    async def get_state(self) -> dict:
        """Fetch guild state: workers, tasks, guild metadata (including owner_user_id)."""
        return await self._get(self._guild_url("/foreman/state"))

    async def get_history(self, user_id: str, limit: int | None = None) -> list[dict]:
        """Fetch raw ForemanTurn rows for a user, ordered oldest→newest."""
        params: dict = {"user_id": user_id}
        if limit is not None:
            params["limit"] = limit
        return await self._get(self._guild_url("/foreman/history"), **params)

    async def get_guild_key(self) -> dict:
        """Fetch the guild's Ed25519 signing key."""
        return await self._get(self._guild_url("/guild-key"))

    async def get_github_token(self) -> dict:
        """Fetch the guild's GitHub OAuth token."""
        return await self._get(
            f"{self._base}/auth/github/token",
            guild_id=self._guild_id,
        )

    # ── State writes ───────────────────────────────────────────────────────

    async def save_turn(
        self,
        user_id: str,
        role: str,
        content_json: str,
        *,
        is_tool_response: bool = False,
        parent_id: int | None = None,
    ) -> dict:
        """Persist one foreman conversation turn. Returns {id, created_at}."""
        body: dict = {
            "user_id": user_id,
            "role": role,
            "content_json": content_json,
            "is_tool_response": is_tool_response,
        }
        if parent_id is not None:
            body["parent_id"] = parent_id
        return await self._post(self._guild_url("/foreman/history"), body)

    async def update_turn_tokens(self, turn_id: int, input_tokens: int, output_tokens: int) -> None:
        """Update token counts for a saved turn."""
        await self._patch(
            self._guild_url(f"/foreman/turns/{turn_id}/tokens"),
            {"input_tokens": input_tokens, "output_tokens": output_tokens},
        )

    async def save_message(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        *,
        user_id: str | None = None,
        message_type: str = "chat",
    ) -> dict:
        """Persist a chat message. Returns {message_id, created_at}."""
        body: dict = {
            "from_agent": from_agent,
            "to_agent": to_agent,
            "content": content,
            "message_type": message_type,
        }
        if user_id:
            body["user_id"] = user_id
        return await self._post(self._guild_url("/messages"), body)

    # ── Tool execution ─────────────────────────────────────────────────────

    async def exec_tool(
        self,
        tool_name: str,
        tool_id: str,
        tool_input: dict,
        user_id: str | None = None,
    ) -> dict:
        """Execute a single tool call via the backend.

        Returns a tool_result block:
        {"type": "tool_result", "tool_use_id": str, "content": str, "is_error": bool}
        """
        body: dict = {
            "tool_name": tool_name,
            "tool_id": tool_id,
            "tool_input": tool_input,
        }
        if user_id:
            body["user_id"] = user_id
        logger.debug("exec_tool: %s (id=%s)", tool_name, tool_id)
        return await self._post(self._guild_url("/foreman/exec_tool"), body)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._client.aclose()
