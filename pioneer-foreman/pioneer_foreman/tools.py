"""Foreman tool definitions and REST-backed executor.

All state mutations that the embedded foreman does via direct DB access are
translated to REST API calls here.  Broadcasts (task-assigned, task-followup,
etc.) that workers and the frontend need are relayed through the
``broadcast_fn`` callback, which sends ``foreman-broadcast`` envelopes over
the active WebSocket connection.

GitHub API calls are direct (same approach as the embedded foreman) using the
guild's OAuth token fetched from the backend at first use.

Known Phase 3 limitations vs. the embedded foreman:
- ``assign_task`` cannot set task.tool, task.issue_number, task.issue_repo,
  task.parent_task_id in the DB (Phase 1 PATCH endpoint doesn't expose them);
  these fields are still included in the ``task-assigned`` broadcast so worker
  runtime behaviour is correct.
- ``get_task_status`` does not return log lines (the logs endpoint requires
  member auth; Phase 3 uses worker auth only).
- ``send_followup`` cannot clear deleted_at / finished_at on terminal tasks
  (PATCH treats None as "no-op"); use the UI to clear those fields first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from backend.foreman_core.tools_schema import FOREMAN_TOOLS  # noqa: F401

logger = logging.getLogger(__name__)

DEFAULT_FINALIZE_TTL_SECONDS = 3 * 24 * 60 * 60  # 3 days

# Broadcast callback type: (guild_id, message_dict) → None
BroadcastFn = Callable[[str, dict], Awaitable[None]]


# ---------------------------------------------------------------------------
# GitHub API helpers (sync — run in thread pool via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _gh_api(path: str, token: str) -> Any:
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _gh_api_post(path: str, token: str, payload: dict, method: str = "POST") -> Any:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _gh_api_diff(path: str, token: str) -> str:
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.diff",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _gh_graphql(token: str, query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _parse_review_from_claude(text: str) -> dict:
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    return {"summary": stripped[:2000], "comments": []}


# ---------------------------------------------------------------------------
# A2A helpers
# ---------------------------------------------------------------------------

_PR_URL_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)/?$")
_VERDICT_TO_GH_EVENT = {
    "approved": "APPROVE",
    "changes-requested": "REQUEST_CHANGES",
    "comment": "COMMENT",
}
_REVIEW_REPORT_MIME = "application/vnd.code-review-agent.report+json"


def _fetch_agent_card(card_url: str) -> dict:
    req = urllib.request.Request(card_url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _post_agent_task(task_url: str, body: bytes) -> dict:
    req = urllib.request.Request(
        task_url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"Agent error {err.get('code')}: {err.get('message', 'unknown')}")
    return data.get("result", data)


def _extract_review_data(a2a_result: dict) -> tuple[str, str]:
    review_body = ""
    verdict = "comment"
    for artifact in a2a_result.get("artifacts", []):
        for part in artifact.get("parts", []):
            part_type = part.get("type", "")
            if part_type == "text" and not review_body:
                review_body = part.get("text", "")
            elif part_type == _REVIEW_REPORT_MIME:
                try:
                    report = json.loads(part.get("text", "{}"))
                    verdict = report.get("verdict") or report.get("summary", {}).get(
                        "verdict", "comment"
                    )
                except (json.JSONDecodeError, AttributeError):
                    pass
    github_event = _VERDICT_TO_GH_EVENT.get(str(verdict).lower(), "COMMENT")
    review_body = review_body or f"Automated code review completed (verdict: {verdict})."
    return github_event, review_body


# ---------------------------------------------------------------------------
# dnsid helpers (copied from backend/foreman/auth.py)
# ---------------------------------------------------------------------------


def _dnsid_bin() -> str:
    return os.path.expanduser(os.environ.get("DNSID_SDK_BIN", "~/dnsid-go/bin/dnsid-sdk"))


def _dnsid_sign_sync(claims: dict, private_key_pem: str) -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = os.path.join(tmpdir, "key.pem")
        config_path = os.path.join(tmpdir, "config.json")
        with open(key_path, "w") as f:
            f.write(private_key_pem)
        with open(config_path, "w") as f:
            json.dump({"key_path": key_path}, f)
        result = subprocess.run(
            [_dnsid_bin(), "sign", "--config", config_path],
            input=json.dumps(claims).encode(),
            capture_output=True,
            timeout=10,
        )
    out = json.loads(result.stdout)
    if not out.get("ok"):
        raise RuntimeError(
            f"dnsid sign [{out.get('error', '?')}]: "
            f"{out.get('message', result.stderr.decode(errors='replace')[:200])}"
        )
    return out["jwt"]


async def _run_dnsid(command: str, inp: dict, private_key_pem: str | None = None) -> dict:
    if command == "resolve":
        fqdn = inp.get("fqdn", "")
        if not fqdn:
            raise ValueError("dnsid resolve requires fqdn")
        cmd = [_dnsid_bin(), "resolve", fqdn]
        stdin_data = None
    elif command == "sign":
        claims = inp.get("claims")
        if not isinstance(claims, dict):
            raise ValueError("dnsid sign requires claims object")
        if not private_key_pem:
            raise ValueError("dnsid sign requires a guild signing key (none found)")
        return {
            "ok": True,
            "jwt": await asyncio.to_thread(_dnsid_sign_sync, claims, private_key_pem),
        }
    elif command == "verify":
        jwt_token = inp.get("jwt", "")
        expected_aud = inp.get("expected_aud", "")
        if not jwt_token:
            raise ValueError("dnsid verify requires jwt")
        if not expected_aud:
            raise ValueError("dnsid verify requires expected_aud")
        cmd = [_dnsid_bin(), "verify", "--jwt", jwt_token, "--expected-aud", expected_aud]
        if nonce := inp.get("expected_nonce"):
            cmd += ["--expected-nonce", nonce]
        stdin_data = None
    else:
        raise ValueError(f"Unknown dnsid command: {command!r}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(stdin_data), timeout=15)
    result = json.loads(stdout)
    if not result.get("ok"):
        raise RuntimeError(
            f"dnsid {command} [{result.get('error', '?')}]: "
            f"{result.get('message', stderr.decode(errors='replace')[:200])}"
        )
    return result


# ---------------------------------------------------------------------------
# GitHub token cache
# ---------------------------------------------------------------------------

_github_token_cache: dict[str, tuple[str, str]] = {}  # guild_id → (token, username)


async def _guild_github_token(
    guild_id: str,
    client: httpx.AsyncClient,
    cfg_token: str | None = None,
) -> tuple[str, str] | None:
    """Return (access_token, github_username) for the guild, or None."""
    if guild_id in _github_token_cache:
        return _github_token_cache[guild_id]
    if cfg_token:
        result = (cfg_token, "")
        _github_token_cache[guild_id] = result
        return result
    try:
        resp = await client.get("/auth/github/token", params={"guild_id": guild_id})
        if resp.status_code == 200:
            data = resp.json()
            result = (data["access_token"], data.get("username", ""))
            _github_token_cache[guild_id] = result
            return result
    except Exception as exc:
        logger.warning("guild=%s failed to fetch GitHub token from backend: %s", guild_id, exc)
    return None


async def _guild_private_key_pem(guild_id: str, client: httpx.AsyncClient) -> str | None:
    """Return the Ed25519 private key PEM for the guild, or None."""
    try:
        resp = await client.get(f"/guilds/{guild_id}/guild-key")
        if resp.status_code == 200:
            return resp.json().get("private_key_pem")
    except Exception as exc:
        logger.warning("guild=%s failed to fetch guild key: %s", guild_id, exc)
    return None


# ---------------------------------------------------------------------------
# Idle worker selection (mirrors backend/foreman/tools.py _select_followup_worker)
# ---------------------------------------------------------------------------


def _has_idle_agent(worker: dict) -> bool:
    """Return True if the worker has at least one idle agent."""
    agents_str = worker.get("agents", "")
    if not agents_str:
        return False
    for part in agents_str.split(","):
        kv = part.strip().split(":", 1)
        if len(kv) == 2 and kv[1] == "idle":
            return True
    return False


def _select_followup_worker(
    workers: list[dict],
    original_worker_id: str | None,
    preferred_worker_id: str | None = None,
) -> str | None:
    """Pick an idle worker for a follow-up, mirroring the embedded foreman logic."""
    online: dict[str, dict] = {w["id"]: w for w in workers if w.get("state") == "online"}
    if (
        preferred_worker_id
        and preferred_worker_id in online
        and _has_idle_agent(online[preferred_worker_id])
    ):
        return preferred_worker_id
    if (
        original_worker_id
        and original_worker_id in online
        and _has_idle_agent(online[original_worker_id])
    ):
        return original_worker_id
    for w in workers:
        wid = w["id"]
        if wid == "foreman":
            continue
        if w.get("state") == "online" and _has_idle_agent(w):
            return wid
    return None


# ---------------------------------------------------------------------------
# Finalize TTL resolver (mirrors backend/foreman/tools.py)
# ---------------------------------------------------------------------------


def _resolve_finalize_deleted_at(inp: dict) -> tuple[str, str | None]:
    raw = inp.get("deleted_at")
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError as exc:
            return "", f"Invalid deleted_at: {exc}"
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat(), None
    seconds = inp.get("expires_in_seconds")
    if seconds is not None:
        try:
            secs = int(seconds)
        except (TypeError, ValueError):
            return "", f"Invalid expires_in_seconds: {seconds!r}"
        if secs < 0:
            return "", "expires_in_seconds must be >= 0"
        return (datetime.now(UTC) + timedelta(seconds=secs)).isoformat(), None
    default = datetime.now(UTC) + timedelta(seconds=DEFAULT_FINALIZE_TTL_SECONDS)
    return default.isoformat(), None


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------


async def exec_tools(
    guild_id: str,
    tool_uses: list,
    *,
    client: httpx.AsyncClient,
    broadcast_fn: BroadcastFn,
    github_token: str | None = None,
    user_id: str | None = None,
) -> list:
    """Execute tool calls from the foreman AI and return tool-result blocks.

    Independent tool calls run concurrently. Results are returned in the same
    order as *tool_uses* to satisfy the Anthropic API's tool_result contract.
    """
    coros = [
        _exec_one_tool(
            guild_id,
            tu,
            client=client,
            broadcast_fn=broadcast_fn,
            github_token=github_token,
            user_id=user_id,
        )
        for tu in tool_uses
    ]
    return list(await asyncio.gather(*coros))


async def _exec_one_tool(
    guild_id: str,
    tu,
    *,
    client: httpx.AsyncClient,
    broadcast_fn: BroadcastFn,
    github_token: str | None = None,
    user_id: str | None = None,
) -> dict:
    inp = tu.input
    result_text = ""
    is_error = False

    try:
        # ── Task management tools (REST + broadcast relay) ────────────────

        if tu.name == "create_task":
            name = (inp.get("name") or "")[:80]
            desc = inp.get("description", name)
            phase = inp.get("phase", "execute")
            resp = await client.post(
                f"/guilds/{guild_id}/tasks",
                json={"name": name, "description": desc, "phase": phase, "user_id": user_id},
            )
            resp.raise_for_status()
            data = resp.json()
            task_id = data["task_id"]
            result_text = (
                f"Task {task_id} created: '{name}'. Reference this task_id in assign_task."
            )

        elif tu.name == "assign_task":
            wid = inp["worker_id"]
            desc = inp.get("description", "")
            phase = inp.get("phase", "execute")
            tool = inp.get("tool", "claude")
            existing_task_id = inp.get("task_id")
            repos: list[str] = inp.get("repos") or []

            # Fetch current state to validate worker and resolve repos
            state_resp = await client.get(f"/guilds/{guild_id}/foreman/state")
            state_resp.raise_for_status()
            state_data = state_resp.json()
            workers = state_data.get("workers", [])
            primary_repo: str | None = (state_data.get("guild") or {}).get("primary_repo")
            if not repos and primary_repo:
                repos = [primary_repo]

            worker_map = {w["id"]: w for w in workers}
            if wid not in worker_map:
                result_text = f"Worker {wid} not found — task NOT queued."
                is_error = True
            else:
                worker = worker_map[wid]
                worker_repos: list[str] = worker.get("repos") or []
                worker_org: str | None = worker.get("org")
                if repos:
                    unreachable = [
                        r
                        for r in repos
                        if r not in worker_repos
                        and not (worker_org and r.startswith(f"{worker_org}/"))
                    ]
                    if unreachable:
                        result_text = (
                            f"Worker {wid} cannot access repo(s) {unreachable} — "
                            f"task NOT queued. Worker has {len(worker_repos)} registered "
                            f"repo(s) and org={worker_org!r}. Choose a worker that has "
                            f"access to these repos, or omit repos to use the worker's "
                            f"full configured list."
                        )
                        is_error = True

            if not is_error:
                if existing_task_id:
                    # Update existing task: set worker_id, state, phase
                    name_override = inp.get("name")
                    patch_body: dict = {
                        "worker_id": wid,
                        "state": "pending",
                        "phase": phase,
                    }
                    patch_resp = await client.patch(
                        f"/guilds/{guild_id}/tasks/{existing_task_id}",
                        json={k: v for k, v in patch_body.items() if v is not None},
                    )
                    patch_resp.raise_for_status()
                    task_id = existing_task_id
                    task_name = name_override or desc[:60]
                    result_text = f"Task {task_id} assigned to {wid}."
                else:
                    # Create new task then update its worker
                    name = inp.get("name") or desc[:60]
                    create_resp = await client.post(
                        f"/guilds/{guild_id}/tasks",
                        json={
                            "name": name,
                            "description": desc,
                            "phase": phase,
                            "user_id": user_id,
                        },
                    )
                    create_resp.raise_for_status()
                    task_id = create_resp.json()["task_id"]
                    task_name = name
                    patch_resp = await client.patch(
                        f"/guilds/{guild_id}/tasks/{task_id}",
                        json={"worker_id": wid, "state": "pending"},
                    )
                    patch_resp.raise_for_status()
                    result_text = f"Task {task_id} queued for {wid}."

                # Relay task-assigned broadcast so the worker picks it up
                await broadcast_fn(
                    guild_id,
                    {
                        "type": "task-assigned",
                        "workerId": wid,
                        "taskId": task_id,
                        "name": task_name,
                        "description": desc,
                        "tool": tool,
                        "phase": phase,
                        **(
                            {"parentTaskId": inp.get("parent_task_id")}
                            if not existing_task_id
                            else {}
                        ),
                        "issueNumber": inp.get("issue_number"),
                        "issueRepo": inp.get("issue_repo"),
                        "repos": repos,
                    },
                )

        elif tu.name == "send_followup":
            task_id = inp["task_id"]
            instructions = inp["instructions"]
            preferred_worker_id = inp.get("preferred_worker_id")

            # Get current state for worker selection and task info
            state_resp = await client.get(f"/guilds/{guild_id}/foreman/state")
            state_resp.raise_for_status()
            state_data = state_resp.json()
            workers = state_data.get("workers", [])

            # Find the task in current state (non-terminal tasks only)
            task_info: dict | None = None
            for t in state_data.get("tasks", []):
                if t["id"] == task_id:
                    task_info = t
                    break

            if not task_info:
                result_text = (
                    f"Task {task_id} not found in active tasks. "
                    "It may be in a terminal state (done/failed/cancelled)."
                )
                is_error = True
            else:
                original_worker_id = task_info.get("worker_id")
                branch = task_info.get("branch")
                target_worker_id = _select_followup_worker(
                    workers, original_worker_id, preferred_worker_id
                )
                if not target_worker_id:
                    result_text = (
                        f"No idle worker available to continue task {task_id} on branch "
                        f"{branch or '<unknown>'}. Wait for one to come online."
                    )
                    is_error = True
                elif not branch:
                    result_text = (
                        f"Task {task_id} has no branch recorded — can't dispatch a "
                        "follow-up. The task may have failed before its first push."
                    )
                    is_error = True
                else:
                    patch_resp = await client.patch(
                        f"/guilds/{guild_id}/tasks/{task_id}",
                        json={
                            "state": "working",
                            "phase": "followup",
                            "worker_id": target_worker_id,
                        },
                    )
                    patch_resp.raise_for_status()
                    await broadcast_fn(
                        guild_id,
                        {
                            "type": "task-update",
                            "taskId": task_id,
                            "state": "working",
                            "workerId": target_worker_id,
                        },
                    )
                    await broadcast_fn(
                        guild_id,
                        {
                            "type": "task-followup",
                            "workerId": target_worker_id,
                            "taskId": task_id,
                            "name": task_info.get("name") or "",
                            "description": task_info.get("description") or "",
                            "tool": task_info.get("tool") or "claude",
                            "branch": branch,
                            "instructions": instructions,
                            "issueNumber": task_info.get("issue_number"),
                            "issueRepo": task_info.get("issue_repo"),
                        },
                    )
                    if target_worker_id != original_worker_id and original_worker_id:
                        result_text = (
                            f"Follow-up reassigned from {original_worker_id} "
                            f"to {target_worker_id} (task {task_id} on branch {branch})."
                        )
                    else:
                        result_text = (
                            f"Follow-up sent to {target_worker_id} for task {task_id} "
                            f"on branch {branch}."
                        )

        elif tu.name == "finalize_task":
            task_id = inp["task_id"]
            deleted_at, err = _resolve_finalize_deleted_at(inp)
            if err:
                result_text = err
                is_error = True
            else:
                finished_at = datetime.now(UTC).isoformat()
                patch_resp = await client.patch(
                    f"/guilds/{guild_id}/tasks/{task_id}",
                    json={"state": "done", "finished_at": finished_at, "deleted_at": deleted_at},
                )
                patch_resp.raise_for_status()
                # PATCH already broadcasts task-update; relay task-finalize for worker cleanup
                await broadcast_fn(
                    guild_id,
                    {"type": "task-finalize", "taskId": task_id},
                )
                result_text = f"Task {task_id} finalized; soft-delete at {deleted_at}."

        elif tu.name == "message_worker":
            wid = inp["worker_id"]
            msg = inp["message"]
            await broadcast_fn(
                guild_id,
                {"type": "worker-message", "workerId": wid, "message": msg},
            )
            result_text = f"Message delivered to {wid}."

        elif tu.name == "redirect_task":
            task_id = inp["task_id"]
            instructions = inp["instructions"]

            # Find task to get worker_id
            state_resp = await client.get(f"/guilds/{guild_id}/foreman/state")
            state_resp.raise_for_status()
            state_data = state_resp.json()
            task_info = next((t for t in state_data.get("tasks", []) if t["id"] == task_id), None)
            if not task_info:
                result_text = f"Task {task_id} not found in active tasks."
                is_error = True
            else:
                worker_id_val = task_info.get("worker_id")
                patch_resp = await client.patch(
                    f"/guilds/{guild_id}/tasks/{task_id}",
                    json={"state": "working"},
                )
                patch_resp.raise_for_status()
                await broadcast_fn(
                    guild_id,
                    {
                        "type": "task-redirect",
                        "workerId": worker_id_val,
                        "taskId": task_id,
                        "instructions": instructions,
                    },
                )
                result_text = f"Redirect sent to {worker_id_val} for task {task_id}."

        elif tu.name == "cancel_task":
            task_id = inp["task_id"]
            reason = inp.get("reason", "")

            state_resp = await client.get(f"/guilds/{guild_id}/foreman/state")
            state_resp.raise_for_status()
            state_data = state_resp.json()
            task_info = next((t for t in state_data.get("tasks", []) if t["id"] == task_id), None)
            if not task_info:
                result_text = f"Task {task_id} not found in active tasks."
                is_error = True
            else:
                state = task_info.get("state", "")
                if state in ("done", "failed", "cancelled"):
                    result_text = f"Task {task_id} is already {state}."
                else:
                    worker_id_val = task_info.get("worker_id")
                    finished_at = datetime.now(UTC).isoformat()
                    patch_resp = await client.patch(
                        f"/guilds/{guild_id}/tasks/{task_id}",
                        json={"state": "cancelled", "finished_at": finished_at},
                    )
                    patch_resp.raise_for_status()
                    await broadcast_fn(
                        guild_id,
                        {"type": "task-cancel", "workerId": worker_id_val, "taskId": task_id},
                    )
                    result_text = f"Task {task_id} cancelled." + (
                        f" Reason: {reason}" if reason else ""
                    )

        elif tu.name == "shutdown_worker":
            wid = inp["worker_id"]
            reason = inp.get("reason", "")
            msg: dict = {"type": "worker-shutdown", "workerId": wid}
            if reason:
                msg["reason"] = reason
            await broadcast_fn(guild_id, msg)
            result_text = f"Shutdown signal sent to {wid}." + (
                f" Reason: {reason}" if reason else ""
            )

        elif tu.name == "get_task_status":
            task_id = inp["task_id"]
            # Use foreman state endpoint (worker auth); log lines not available in Phase 3
            state_resp = await client.get(f"/guilds/{guild_id}/foreman/state")
            state_resp.raise_for_status()
            state_data = state_resp.json()
            task_info = next((t for t in state_data.get("tasks", []) if t["id"] == task_id), None)
            if not task_info:
                result_text = f"Task {task_id} not found in active tasks (may be terminal/deleted)."
            else:
                result_text = json.dumps(
                    {
                        "id": task_info.get("id"),
                        "name": task_info.get("name"),
                        "state": task_info.get("state"),
                        "phase": task_info.get("phase"),
                        "worker_id": task_info.get("worker_id"),
                        "branch": task_info.get("branch"),
                        "pr_url": task_info.get("pr_url"),
                        "created_at": task_info.get("created_at"),
                        "finished_at": task_info.get("finished_at"),
                        "note": "Log lines not available in standalone foreman (Phase 3).",
                    }
                )

        # ── GitHub tools ──────────────────────────────────────────────────

        if tu.name in (
            "list_github_issues",
            "get_github_issue",
            "list_github_prs",
            "claim_github_issue",
            "create_github_issue",
            "search_github_issues",
            "get_pr_status",
            "review_pr",
            "review_pr_internal",
        ):
            logger.info("Executing GitHub tool %s", tu.name)
            creds = await _guild_github_token(guild_id, client, github_token)
            if not creds:
                result_text = (
                    "No GitHub token found for this guild — user must connect GitHub first, "
                    "or set GITHUB_TOKEN in the foreman's environment."
                )
                is_error = True
            else:
                token, username = creds
                try:
                    if tu.name == "list_github_issues":
                        repo = inp["repo"]
                        state = inp.get("state", "open")
                        limit = min(int(inp.get("limit", 20)), 50)
                        issues = await asyncio.to_thread(
                            _gh_api,
                            f"/repos/{repo}/issues?state={state}&per_page={limit}",
                            token,
                        )
                        result_text = json.dumps(
                            [
                                {
                                    "number": i["number"],
                                    "title": i["title"],
                                    "state": i["state"],
                                    "labels": [l["name"] for l in i.get("labels", [])],
                                    "assignees": [a["login"] for a in i.get("assignees", [])],
                                    "created_at": i["created_at"],
                                }
                                for i in issues
                                if "pull_request" not in i
                            ]
                        )

                    elif tu.name == "get_github_issue":
                        repo = inp["repo"]
                        num = int(inp["issue_number"])
                        issue, comments_raw = await asyncio.gather(
                            asyncio.to_thread(_gh_api, f"/repos/{repo}/issues/{num}", token),
                            asyncio.to_thread(
                                _gh_api, f"/repos/{repo}/issues/{num}/comments?per_page=20", token
                            ),
                        )
                        result_text = json.dumps(
                            {
                                "number": issue["number"],
                                "title": issue["title"],
                                "state": issue["state"],
                                "body": (issue.get("body") or "")[:2000],
                                "labels": [l["name"] for l in issue.get("labels", [])],
                                "comments": [
                                    {
                                        "author": c["user"]["login"],
                                        "body": (c.get("body") or "")[:500],
                                    }
                                    for c in comments_raw
                                ],
                            }
                        )

                    elif tu.name == "list_github_prs":
                        repo = inp["repo"]
                        state = inp.get("state", "open")
                        prs = await asyncio.to_thread(
                            _gh_api, f"/repos/{repo}/pulls?state={state}&per_page=20", token
                        )
                        result_text = json.dumps(
                            [
                                {
                                    "number": p["number"],
                                    "title": p["title"],
                                    "state": p["state"],
                                    "head": p["head"]["ref"],
                                    "draft": p.get("draft", False),
                                }
                                for p in prs
                            ]
                        )

                    elif tu.name == "claim_github_issue":
                        repo = inp["repo"]
                        num = int(inp["issue_number"])
                        await asyncio.to_thread(
                            _gh_api_post,
                            f"/repos/{repo}/issues/{num}/assignees",
                            token,
                            {"assignees": [username]},
                        )
                        result_text = f"Issue #{num} in {repo} assigned to {username}."

                    elif tu.name == "create_github_issue":
                        repo = inp["repo"]
                        payload: dict = {"title": inp["title"], "body": inp.get("body", "")}
                        if inp.get("labels"):
                            payload["labels"] = inp["labels"]
                        issue = await asyncio.to_thread(
                            _gh_api_post, f"/repos/{repo}/issues", token, payload
                        )
                        result_text = json.dumps(
                            {
                                "number": issue["number"],
                                "url": issue["html_url"],
                                "title": issue["title"],
                            }
                        )

                    elif tu.name == "search_github_issues":
                        repo = inp["repo"]
                        query = inp["query"]
                        state = inp.get("state", "open")
                        state_q = "" if state == "all" else f"+state:{state}"
                        search_url = (
                            f"/search/issues?q={urllib.parse.quote(query)}"
                            f"+repo:{repo}{state_q}&per_page=10&sort=created&order=desc"
                        )
                        data = await asyncio.to_thread(_gh_api, search_url, token)
                        items = data.get("items", []) if isinstance(data, dict) else data
                        result_text = json.dumps(
                            [
                                {
                                    "number": i["number"],
                                    "title": i["title"],
                                    "state": i["state"],
                                    "url": i["html_url"],
                                    "labels": [l["name"] for l in i.get("labels", [])],
                                }
                                for i in items
                            ]
                        )

                    elif tu.name == "get_pr_status":
                        repo = inp["repo"]
                        num = int(inp["pr_number"])
                        pr, reviews_raw = await asyncio.gather(
                            asyncio.to_thread(_gh_api, f"/repos/{repo}/pulls/{num}", token),
                            asyncio.to_thread(
                                _gh_api,
                                f"/repos/{repo}/pulls/{num}/reviews?per_page=20",
                                token,
                            ),
                        )
                        head_sha = (pr.get("head") or {}).get("sha")
                        check_runs: list = []
                        if head_sha:
                            crs = await asyncio.to_thread(
                                _gh_api,
                                f"/repos/{repo}/commits/{head_sha}/check-runs?per_page=30",
                                token,
                            )
                            if isinstance(crs, dict):
                                check_runs = crs.get("check_runs", []) or []
                        result_text = json.dumps(
                            {
                                "number": pr["number"],
                                "state": pr["state"],
                                "merged": pr.get("merged", False),
                                "mergeable": pr.get("mergeable"),
                                "draft": pr.get("draft", False),
                                "head_sha": head_sha,
                                "reviews": [
                                    {
                                        "user": (r.get("user") or {}).get("login"),
                                        "state": r.get("state"),
                                        "body": (r.get("body") or "")[:300],
                                        "submitted_at": r.get("submitted_at"),
                                    }
                                    for r in reviews_raw
                                ],
                                "checks": [
                                    {
                                        "name": cr.get("name"),
                                        "status": cr.get("status"),
                                        "conclusion": cr.get("conclusion"),
                                        "summary": ((cr.get("output") or {}).get("summary") or "")[
                                            :300
                                        ],
                                    }
                                    for cr in check_runs
                                ],
                            }
                        )

                    elif tu.name == "review_pr":
                        pr_url = inp["pr_url"]
                        logger.info("review_pr: pr_url=%s", pr_url)
                        pr_match = _PR_URL_RE.match(pr_url.rstrip("/"))
                        if not pr_match:
                            result_text = (
                                f"Invalid GitHub PR URL: {pr_url!r}. "
                                "Expected https://github.com/owner/repo/pull/N"
                            )
                            is_error = True
                        else:
                            pr_repo = pr_match.group(1)
                            pr_number = int(pr_match.group(2))
                            review_agent = os.environ.get(
                                "REVIEWER_AGENT_URL", "https://agent.meyers.life"
                            )
                            card_url = f"{review_agent.rstrip('/')}/.well-known/agent.json"
                            card = await asyncio.to_thread(_fetch_agent_card, card_url)
                            task_body = json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "method": "tasks/send",
                                    "params": {
                                        "skill_id": "review_pr",
                                        "message": {"parts": [{"type": "text", "text": pr_url}]},
                                    },
                                    "id": 1,
                                }
                            ).encode()
                            a2a_result = await asyncio.to_thread(
                                _post_agent_task,
                                f"{review_agent.rstrip('/')}/jsonrpc",
                                task_body,
                            )
                            github_event, review_body = _extract_review_data(a2a_result)
                            review_data = await asyncio.to_thread(
                                _gh_api_post,
                                f"/repos/{pr_repo}/pulls/{pr_number}/reviews",
                                token,
                                {"body": review_body, "event": github_event},
                            )
                            result_text = json.dumps(
                                {
                                    "pr_url": pr_url,
                                    "verdict": github_event,
                                    "review_id": review_data.get("id"),
                                    "review_posted": True,
                                    "summary": review_body[:400],
                                }
                            )

                    elif tu.name == "review_pr_internal":
                        pr_url = inp["pr_url"]
                        action = (inp.get("action") or "COMMENT").upper()
                        if action not in ("APPROVE", "REQUEST_CHANGES", "COMMENT"):
                            action = "COMMENT"
                        pr_match = _PR_URL_RE.match(pr_url.rstrip("/"))
                        if not pr_match:
                            result_text = (
                                f"Invalid GitHub PR URL: {pr_url!r}. "
                                "Expected https://github.com/owner/repo/pull/N"
                            )
                            is_error = True
                        else:
                            pr_repo = pr_match.group(1)
                            pr_number = int(pr_match.group(2))
                            pr_data, diff_text = await asyncio.gather(
                                asyncio.to_thread(
                                    _gh_api, f"/repos/{pr_repo}/pulls/{pr_number}", token
                                ),
                                asyncio.to_thread(
                                    _gh_api_diff, f"/repos/{pr_repo}/pulls/{pr_number}", token
                                ),
                            )
                            pr_title = pr_data.get("title", "")
                            pr_body_text = pr_data.get("body") or "(no description)"
                            base_ref = (pr_data.get("base") or {}).get("ref", "")
                            head_ref = (pr_data.get("head") or {}).get("ref", "")
                            try:
                                import anthropic as _anthropic

                                _ai = _anthropic.AsyncAnthropic()
                                review_prompt = (
                                    "You are a thorough code reviewer. Review the following "
                                    "GitHub pull request and provide structured feedback.\n\n"
                                    f"PR: {pr_title}\n"
                                    f"Base: {base_ref} ← Head: {head_ref}\n"
                                    f"Description: {pr_body_text[:1000]}\n\n"
                                    f"Diff (up to 40 000 chars):\n{diff_text[:40000]}\n\n"
                                    "Respond with a JSON object only (no markdown fences) "
                                    "with exactly these fields:\n"
                                    '{"summary": "3-5 markdown bullet points (use - prefix)", '
                                    '"comments": [{"path": "file.py", "line": 42, '
                                    '"side": "RIGHT", "body": "concise comment"}]}\n\n'
                                    "Rules:\n"
                                    "- summary: 3-5 bullet points covering key findings\n"
                                    "- comments: 0-5 objects for the most important issues\n"
                                    "- line: line number in the NEW file version (RIGHT side)\n"
                                    "- Only comment on lines present in the diff\n"
                                    "- Focus on bugs, security issues, and significant design problems\n"
                                    "- Keep each comment concise (1-3 sentences)"
                                )
                                ai_msg = await _ai.messages.create(
                                    model=os.environ.get("FOREMAN_MODEL", "claude-sonnet-4-6"),
                                    max_tokens=2048,
                                    messages=[{"role": "user", "content": review_prompt}],
                                )
                                review_json = _parse_review_from_claude(ai_msg.content[0].text)
                            except Exception as exc:
                                logger.error("review_pr_internal: AI generation failed: %s", exc)
                                review_json = {
                                    "summary": "Review could not be generated by the AI agent.",
                                    "comments": [],
                                }

                            summary_text = review_json.get("summary", "(no summary)")
                            raw_comments = review_json.get("comments") or []
                            gh_comments = [
                                {
                                    "path": c["path"],
                                    "line": int(c["line"]),
                                    "side": c.get("side", "RIGHT"),
                                    "body": c["body"],
                                }
                                for c in raw_comments
                                if c.get("path") and c.get("line") and c.get("body")
                            ][:5]
                            try:
                                review_data = await asyncio.to_thread(
                                    _gh_api_post,
                                    f"/repos/{pr_repo}/pulls/{pr_number}/reviews",
                                    token,
                                    {
                                        "body": summary_text,
                                        "event": action,
                                        "comments": gh_comments,
                                    },
                                )
                            except urllib.error.HTTPError:
                                review_data = await asyncio.to_thread(
                                    _gh_api_post,
                                    f"/repos/{pr_repo}/pulls/{pr_number}/reviews",
                                    token,
                                    {"body": summary_text, "event": action, "comments": []},
                                )
                                gh_comments = []
                            result_text = json.dumps(
                                {
                                    "pr_url": pr_url,
                                    "verdict": action,
                                    "review_id": review_data.get("id"),
                                    "review_posted": True,
                                    "inline_comments_posted": len(gh_comments),
                                    "summary": summary_text[:400],
                                }
                            )

                except urllib.error.HTTPError as exc:
                    result_text = f"GitHub API error: {exc.code} {exc.reason}"
                    is_error = True
                except Exception as exc:
                    result_text = f"GitHub error: {exc}"
                    is_error = True

        # ── dnsid ─────────────────────────────────────────────────────────

        if tu.name == "dnsid":
            command = inp.get("command", "")
            if not command:
                result_text = "dnsid requires command (resolve, sign, verify)"
                is_error = True
            else:
                try:
                    pem: str | None = None
                    if command == "sign":
                        pem = await _guild_private_key_pem(guild_id, client)
                    result_text = json.dumps(await _run_dnsid(command, inp, pem))
                except (ValueError, RuntimeError) as exc:
                    result_text = str(exc)
                    is_error = True
                except Exception as exc:
                    result_text = f"dnsid {command} failed: {exc}"
                    is_error = True

        # ── A2A call_agent ─────────────────────────────────────────────────

        if tu.name == "call_agent":
            agent_url = (inp.get("agent_url") or "").rstrip("/")
            skill_id = inp.get("skill") or ""
            params = inp.get("params") or {}
            if not agent_url:
                result_text = "call_agent requires agent_url"
                is_error = True
            elif not skill_id:
                result_text = "call_agent requires skill"
                is_error = True
            else:
                try:
                    card_url = f"{agent_url}/.well-known/agent.json"
                    card = await asyncio.to_thread(_fetch_agent_card, card_url)
                    skills = card.get("skills", [])
                    skill_ids = [s.get("id", "") for s in skills]
                    if skills and skill_id not in skill_ids:
                        result_text = (
                            f"Skill {skill_id!r} not found on agent at {agent_url}. "
                            f"Available skills: {', '.join(skill_ids)}"
                        )
                        is_error = True
                    else:
                        task_body = json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": "tasks/send",
                                "params": {
                                    "skill_id": skill_id,
                                    "message": {
                                        "parts": [{"type": "text", "text": json.dumps(params)}]
                                    },
                                },
                                "id": 1,
                            }
                        ).encode()
                        response = await asyncio.to_thread(
                            _post_agent_task, f"{agent_url}/jsonrpc", task_body
                        )
                        result_text = json.dumps(
                            {
                                "agent_url": agent_url,
                                "skill": skill_id,
                                "agent_name": card.get("name", ""),
                                "response": response,
                            }
                        )
                except urllib.error.HTTPError as exc:
                    result_text = f"Agent HTTP error {exc.code}: {exc.reason}"
                    is_error = True
                except Exception as exc:
                    result_text = f"Agent call failed: {exc}"
                    is_error = True

    except Exception as exc:
        result_text = f"Tool {tu.name} failed: {exc}"
        is_error = True

    block: dict = {"type": "tool_result", "tool_use_id": tu.id, "content": result_text}
    if is_error:
        block["is_error"] = True
    return block
