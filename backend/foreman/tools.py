"""Foreman tool definitions (embedded), GitHub API helpers, and tool-call executor.

FOREMAN_TOOLS is imported from backend.foreman_core.tools_schema — the single source
of truth shared with the standalone foreman.  This module owns the embedded executor
(exec_tools) and all GitHub/DB helpers.
"""

import asyncio
import contextvars
import json
import logging
import os
import random
import re
import secrets
import string
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from database import get_db
from events import broadcast, broadcast_msg, emit_terminal_line
from foreman_core.llm import get_foreman_model, make_anthropic_client
from foreman_core.message_utils import _json_default
from foreman_core.tools_schema import (
    FOREMAN_TOOLS,  # noqa: F401 — re-exported for test compatibility
)
from lock_service import LockService
from models import (
    Agent,
    ClaudeCredentials,
    GithubToken,
    Guild,
    GuildKey,
    GuildMember,
    Task,
    TaskEvent,
    TaskLog,
    Worker,
)
from sqlalchemy import delete, update
from sqlmodel import col, select
from utils import build_spawn_worker_env, decode_claude_oauth_token, worker_display_name
from ws_types import (
    TaskAssignedMsg,
    TaskCancelMsg,
    TaskCreatedMsg,
    TaskFinalizeMsg,
    TaskFollowupMsg,
    TaskRedirectMsg,
    TaskUpdateMsg,
    WorkerMessageMsg,
    WorkerShutdownMsg,
)

logger = logging.getLogger(__name__)

# Default soft-delete window (seconds) when finalize_task is called without
# an explicit expiry. Mirrors backend.main.DEFAULT_FINALIZE_TTL.
DEFAULT_FINALIZE_TTL_SECONDS = 3 * 24 * 60 * 60  # 3 days

# Hard cap on any single blocking external-service call (GitHub API, Docker, A2A
# agent).  Wrapping asyncio.to_thread with asyncio.wait_for(timeout=...) means a
# hung upstream can stall at most this many seconds instead of freezing the whole
# event loop indefinitely.
EXTERNAL_CALL_TIMEOUT = 30

# Separate, larger timeout for docker_client.containers.run (detach=True).
# The Docker API call itself should complete once the daemon confirms the container
# started, but can legitimately take longer than a GitHub REST call on a loaded host.
CONTAINER_RUN_TIMEOUT = 600

# Module-level Docker client — created once per process to reuse the Unix-socket
# connection across repeated spawn_worker calls instead of opening a new socket
# each time.  None until first successful from_env(); tests can patch
# _get_docker_client() directly to avoid touching sys.modules.
_docker_client: Any = None

# Per-tool-call collector for GitHub API response metadata (request IDs, status codes).
# Each _exec_one_tool invocation sets a fresh list via _api_calls_ctx.set([]) using the
# token-reset pattern so concurrent coroutines never share a list.  asyncio.to_thread
# copies the context so thread-pool calls can also append to the same list.
_api_calls_ctx: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "_api_calls_ctx", default=None
)


def _record_api_call(path: str, status: int, headers: Any) -> None:
    """Append one HTTP-call record to the per-tool-call collector (no-op if unset)."""
    calls = _api_calls_ctx.get(None)
    if calls is None:
        return
    entry: dict = {"path": path, "status": status, "ts": datetime.now(UTC).isoformat()}
    if headers:
        rid = headers.get("x-request-id")
        ghrid = headers.get("x-github-request-id")
        if rid:
            entry["x_request_id"] = rid
        if ghrid:
            entry["x_github_request_id"] = ghrid
    calls.append(entry)


async def _get_docker_client() -> Any:
    """Return the cached Docker client, importing the SDK and calling from_env() once.

    Skips the thread hop on subsequent calls once the client is cached.
    """
    global _docker_client
    if _docker_client is not None:
        return _docker_client
    import docker  # ImportError propagated to caller if SDK not installed

    _docker_client = await _to_thread(docker.from_env)
    return _docker_client


async def _to_thread(fn, /, *args, **kwargs):
    """Run a blocking callable in a thread pool with a global timeout.

    WARNING: asyncio cancellation/timeout does NOT interrupt the underlying thread.
    If fn has side effects (e.g. spawning containers, open sockets), callers must
    handle cleanup in an except block — the thread will continue running to
    completion even after a TimeoutError is raised to the caller.
    """
    return await asyncio.wait_for(
        asyncio.to_thread(fn, *args, **kwargs),
        timeout=EXTERNAL_CALL_TIMEOUT,
    )


def _resolve_finalize_deleted_at(inp: dict) -> tuple[datetime | None, str | None]:
    """Compute the soft-delete instant for a finalize_task tool call.

    Returns ``(deleted_at, error)`` — error is non-None when the inputs
    were malformed. Honours an explicit ``deleted_at`` first, then
    ``expires_in_seconds``, otherwise falls back to the default 3-day window.
    """
    raw = inp.get("deleted_at")
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError as exc:
            return None, f"Invalid deleted_at: {exc}"
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC), None
    seconds = inp.get("expires_in_seconds")
    if seconds is not None:
        try:
            secs = int(seconds)
        except (TypeError, ValueError):
            return None, f"Invalid expires_in_seconds: {seconds!r}"
        if secs < 0:
            return None, "expires_in_seconds must be >= 0"
        return datetime.now(UTC) + timedelta(seconds=secs), None
    return datetime.now(UTC) + timedelta(seconds=DEFAULT_FINALIZE_TTL_SECONDS), None


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _gh_api(path: str, token: str) -> Any:
    """GET a GitHub API path and return parsed JSON."""
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            _record_api_call(path, resp.status, resp.headers)
            return data
    except urllib.error.HTTPError as exc:
        _record_api_call(path, exc.code, exc.headers)
        raise


def _gh_api_post(path: str, token: str, payload: dict, method: str = "POST") -> Any:
    """POST/PATCH a GitHub API path with a JSON body and return parsed JSON."""
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
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            _record_api_call(path, resp.status, resp.headers)
            return data
    except urllib.error.HTTPError as exc:
        _record_api_call(path, exc.code, exc.headers)
        raise


def _gh_api_diff(path: str, token: str) -> str:
    """GET a GitHub API path and return the raw unified diff text."""
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.diff",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            _record_api_call(path, resp.status, resp.headers)
            return text
    except urllib.error.HTTPError as exc:
        _record_api_call(path, exc.code, exc.headers)
        raise


def _gh_graphql(token: str, query: str, variables: dict) -> dict:
    """Execute a GitHub GraphQL query/mutation and return the parsed response."""
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
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            _record_api_call("/graphql", resp.status, resp.headers)
            return data
    except urllib.error.HTTPError as exc:
        _record_api_call("/graphql", exc.code, exc.headers)
        raise


def _parse_review_from_claude(text: str) -> dict:
    """Extract a review JSON object from Claude's response.

    Claude may wrap JSON in markdown code fences; this function strips them.
    Falls back to a minimal object if parsing fails entirely.
    """
    # Strip markdown fences if present
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass
    # Try the whole string
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Last resort: return a plain summary with no inline comments
    return {"summary": stripped[:2000], "comments": []}


async def _guild_github_token(guild_id: str) -> tuple[str, str] | None:
    """Return (access_token, github_username) for this guild, or None."""
    from auth_deps import get_guild_pk

    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            return None
        result = await db.exec(
            select(col(GithubToken.access_token), col(GithubToken.github_username))
            .join(GuildMember, col(GuildMember.user_id) == col(GithubToken.github_user_id))
            .where(col(GuildMember.guild_id) == guild_pk, col(GuildMember.role) == "owner")
            .limit(1)
        )
        row = result.first()
        return (row.access_token, row.github_username) if row else None
    finally:
        await db.close()


async def _guild_private_key_pem(guild_id: str) -> str | None:
    """Return the Ed25519 private key PEM for the guild, or None if not found."""
    from auth_deps import get_guild_pk

    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            return None
        result = await db.exec(
            select(col(GuildKey.private_key_pem)).where(col(GuildKey.guild_id) == guild_pk)
        )
        return result.one_or_none()
    finally:
        await db.close()


async def _select_followup_worker(
    db,
    *,
    guild_id: str,
    guild_pk: int | None = None,
    original_worker_id: str | None,
    preferred_worker_id: str | None = None,
) -> str | None:
    """Pick a worker to continue a task's branch.

    Order of preference:
      1. ``preferred_worker_id`` if it has at least one idle agent in the guild
      2. ``original_worker_id`` if it has at least one idle agent (worktree
         likely still on disk for free reuse)
      3. Any other worker in the guild with an idle agent

    Returns the chosen worker_id, or None if no idle worker is available.
    """

    async def _idle(worker_id: str) -> bool:
        if not worker_id:
            return False
        result = await db.exec(
            select(col(Agent.id))
            .where(col(Agent.worker_id) == worker_id, col(Agent.state) == "idle")
            .limit(1)
        )
        return result.one_or_none() is not None

    if preferred_worker_id and await _idle(preferred_worker_id):
        return preferred_worker_id
    if original_worker_id and await _idle(original_worker_id):
        return original_worker_id
    # Fallback: any other idle agent in the guild. Pick the worker_id of the
    # first idle agent we find — repos are configured per-worker, but for now
    # the foreman trusts that a guild's workers cover the same repo set.
    if guild_pk is None:
        from auth_deps import get_guild_pk

        guild_pk = await get_guild_pk(db, guild_id)
    result = await db.exec(
        select(col(Agent.worker_id))
        .where(
            col(Agent.guild_id) == guild_pk,
            col(Agent.state) == "idle",
            col(Agent.worker_id).is_not(None),
        )
        .limit(1)
    )
    return result.one_or_none()


async def maybe_post_plan_comment(guild_id: str, task_id: str, last_text: str) -> None:
    """Post plan output as a GitHub issue comment when a plan-phase task completes."""
    logger = logging.getLogger(__name__)
    try:
        db = await get_db()
        try:
            result = await db.exec(
                select(col(Task.phase), col(Task.issue_number), col(Task.issue_repo)).where(
                    col(Task.id) == task_id
                )
            )
            row = result.first()
        finally:
            await db.close()

        if not row or row.phase != "plan":
            return
        issue_number = row.issue_number
        issue_repo = row.issue_repo
        if not issue_number or not issue_repo:
            return
        if not last_text:
            logger.warning("plan comment: task %s has no output to post", task_id)
            return

        creds = await _guild_github_token(guild_id)
        if not creds:
            logger.warning("plan comment: no GitHub token for guild %s", guild_id)
            return
        token, _ = creds

        body = f"## \U0001f4cb Plan from task `{task_id}`\n\n{last_text}"
        await _to_thread(
            _gh_api_post,
            f"/repos/{issue_repo}/issues/{issue_number}/comments",
            token,
            {"body": body},
        )
        logger.info("plan comment posted to %s#%s for task %s", issue_repo, issue_number, task_id)
    except Exception as exc:
        logger.warning("plan comment failed for task %s: %s", task_id, exc)


# ---------------------------------------------------------------------------
# code-review-agent helpers
# ---------------------------------------------------------------------------

_PR_URL_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)/?$")

_VERDICT_TO_GH_EVENT = {
    "approved": "APPROVE",
    "changes-requested": "REQUEST_CHANGES",
    "comment": "COMMENT",
}

_REVIEW_REPORT_MIME = "application/vnd.code-review-agent.report+json"


def _extract_review_data(a2a_result: dict) -> tuple[str, str]:
    """Parse an A2A tasks/send result from the code-review-agent.

    Returns ``(github_event, review_body)`` where ``github_event`` is one of
    ``"APPROVE"``, ``"REQUEST_CHANGES"``, or ``"COMMENT"``.

    Inspects ``artifacts[*].parts[*]``: text parts supply the review body,
    a part with type ``application/vnd.code-review-agent.report+json`` supplies
    the structured verdict.
    """
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


_GQL_PR_THREADS = """
query GetPRThreads($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 10) {
            nodes {
              databaseId
              pullRequestReview { databaseId }
            }
          }
        }
      }
    }
  }
}
"""

_GQL_RESOLVE_THREAD = """
mutation ResolveThread($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


async def _supersede_prior_bot_reviews(
    pr_repo: str, pr_number: int, bot_username: str, token: str
) -> int:
    """Resolve prior inline threads from previous bot reviews.

    Before a new review is posted this function:
    1. Fetches all existing reviews and filters to those authored by ``bot_username``.
    2. Resolves any unresolved inline threads that belong to those reviews via
       the GitHub GraphQL ``resolveReviewThread`` mutation.

    Returns the count of threads resolved. Errors in individual steps are
    logged as warnings rather than raised so that the main review post is never
    blocked by a cleanup failure.
    """
    reviews = await _to_thread(_gh_api, f"/repos/{pr_repo}/pulls/{pr_number}/reviews", token)
    if not isinstance(reviews, list):
        return 0

    bot_reviews = [r for r in reviews if (r.get("user") or {}).get("login") == bot_username]
    if not bot_reviews:
        return 0

    bot_review_ids = {r["id"] for r in bot_reviews}

    # Resolve unresolved inline threads that belong to our prior reviews.
    threads_resolved = 0
    owner, repo_name = pr_repo.split("/", 1)
    try:
        gql_result = await _to_thread(
            _gh_graphql,
            token,
            _GQL_PR_THREADS,
            {"owner": owner, "repo": repo_name, "number": pr_number},
        )
        threads = (
            gql_result.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        for thread in threads:
            if thread.get("isResolved"):
                continue
            comments = thread.get("comments", {}).get("nodes", [])
            if any(
                (c.get("pullRequestReview") or {}).get("databaseId") in bot_review_ids
                for c in comments
            ):
                try:
                    await _to_thread(
                        _gh_graphql,
                        token,
                        _GQL_RESOLVE_THREAD,
                        {"threadId": thread["id"]},
                    )
                    threads_resolved += 1
                    logger.info(
                        "review_pr: resolved thread %s on %s#%d",
                        thread["id"],
                        pr_repo,
                        pr_number,
                    )
                except Exception as exc:
                    logger.warning("review_pr: failed to resolve thread %s: %s", thread["id"], exc)
    except Exception as exc:
        logger.warning(
            "review_pr: GraphQL thread resolution failed for %s#%d: %s",
            pr_repo,
            pr_number,
            exc,
        )

    return threads_resolved


# ---------------------------------------------------------------------------
# A2A agent call helpers
# ---------------------------------------------------------------------------


def _dnsid_resolve(fqdn: str) -> dict:
    """Look up the _dnsid TXT record and JWKS for fqdn using the dnsid-py library."""
    import dns.resolver

    name = f"_dnsid.{fqdn}"
    try:
        answers = dns.resolver.resolve(name, "TXT")
        parts: list[str] = []
        for rdata in answers:
            for string in rdata.strings:
                parts.append(string.decode() if isinstance(string, bytes) else string)
        txt_data = "".join(parts)
    except Exception as exc:
        raise RuntimeError(f"dnsid resolve [{fqdn}]: DNS lookup failed: {exc}") from exc

    record: dict = {}
    for token in txt_data.split():
        if "=" in token:
            k, _, v = token.partition("=")
            record[k] = v

    jwks_url = f"https://{fqdn}/.well-known/jwks.json"
    req = urllib.request.Request(jwks_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            jwks = json.loads(resp.read())
    except Exception as exc:
        raise RuntimeError(f"dnsid resolve: JWKS fetch failed for {fqdn}: {exc}") from exc

    return {"ok": True, "fqdn": fqdn, "record": record, "keys": jwks.get("keys", [])}


def _dnsid_verify(jwt_token: str, expected_aud: str, expected_nonce: str | None = None) -> dict:
    """Verify a JWT against its DNSid record using the dnsid-py library."""
    import time as _time

    from foreman.oidc import _decode_jwt_parts, _fetch_jwks, _find_jwk, _verify_ed25519

    header, payload, signing_input, signature = _decode_jwt_parts(jwt_token)

    exp = payload.get("exp")
    if exp is not None and _time.time() > int(exp):
        raise RuntimeError("dnsid verify: token has expired")

    aud = payload.get("aud")
    if aud is not None:
        aud_list: list = [aud] if isinstance(aud, str) else list(aud)
        if expected_aud not in aud_list:
            raise RuntimeError(
                f"dnsid verify: expected_aud {expected_aud!r} not in token aud {aud_list!r}"
            )

    if expected_nonce:
        token_nonce = payload.get("nonce") or payload.get("jti")
        if token_nonce != expected_nonce:
            raise RuntimeError("dnsid verify: nonce mismatch")

    iss = payload.get("iss")
    if not iss:
        raise RuntimeError("dnsid verify: token missing 'iss' claim")

    jwks_url = f"https://{iss}/.well-known/jwks.json"
    try:
        keys = _fetch_jwks(jwks_url)
    except Exception as exc:
        raise RuntimeError(f"dnsid verify: JWKS fetch failed: {exc}") from exc

    kid = header.get("kid")
    jwk = _find_jwk(keys, kid)
    if not jwk:
        raise RuntimeError(f"dnsid verify: no suitable key found in JWKS (kid={kid!r})")

    alg = header.get("alg", "")
    if alg in ("EdDSA", "Ed25519"):
        try:
            _verify_ed25519(jwk, signing_input, signature)
        except ValueError as exc:
            raise RuntimeError(f"dnsid verify: {exc}") from exc
    else:
        raise RuntimeError(f"dnsid verify: unsupported algorithm {alg!r}")

    return {"ok": True, "iss": iss, **payload}


async def _run_dnsid(command: str, inp: dict, private_key_pem: str | None = None) -> dict:
    """Run a dnsid operation using the dnsid-py library."""
    if command == "resolve":
        fqdn = inp.get("fqdn", "")
        if not fqdn:
            raise ValueError("dnsid resolve requires fqdn")
        return await _to_thread(_dnsid_resolve, fqdn)
    elif command == "sign":
        claims = inp.get("claims")
        if not isinstance(claims, dict):
            raise ValueError("dnsid sign requires claims object")
        if not private_key_pem:
            raise ValueError("dnsid sign requires a guild signing key (none found in DB)")
        from foreman.auth import _dnsid_sign_sync

        return {
            "ok": True,
            "jwt": await _to_thread(_dnsid_sign_sync, claims, private_key_pem),
        }
    elif command == "verify":
        jwt_token = inp.get("jwt", "")
        expected_aud = inp.get("expected_aud", "")
        if not jwt_token:
            raise ValueError("dnsid verify requires jwt")
        if not expected_aud:
            raise ValueError("dnsid verify requires expected_aud")
        return await _to_thread(_dnsid_verify, jwt_token, expected_aud, inp.get("expected_nonce"))
    else:
        raise ValueError(f"Unknown dnsid command: {command!r}")


def _fetch_agent_card(card_url: str) -> dict:
    """Fetch and parse an A2A agent card from a well-known URL."""
    req = urllib.request.Request(
        card_url,
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _post_agent_task(task_url: str, body: bytes) -> dict:
    """POST a JSON-RPC tasks/send payload to an A2A agent and return the result dict."""
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


# ---------------------------------------------------------------------------
# spawn_worker implementation — re-enabled in FOREMAN_TOOLS after #551, #564, #566, #728
# ---------------------------------------------------------------------------


async def spawn_worker(
    inp: dict,
    guild_id: str,
    guild_pk: int | None,
    db,
) -> tuple[str, bool]:
    """Spawn a new worker container.

    Returns (result_text, is_error).
    """
    repos: list = inp.get("repos") or []
    tools_list: list[str] = inp.get("tools") or []
    agent_count: int | None = inp.get("agent_count")
    custom_name: str | None = inp.get("name")
    if not repos:
        return "spawn_worker requires at least one repo in the 'repos' list.", True

    worker_id = "w-" + secrets.token_hex(3)
    auth_token = secrets.token_urlsafe(32)
    worker_name = custom_name or worker_display_name(worker_id, None)
    created_at = datetime.now(UTC)
    db.add(
        Worker(
            id=worker_id,
            guild_id=guild_pk or 0,
            repos=json.dumps(repos),
            state="offline",
            created_at=created_at,
            auth_token=auth_token,
            name=worker_name,
        )
    )
    await db.commit()

    creds_result = await db.exec(
        select(col(ClaudeCredentials.credentials_blob)).where(
            col(ClaudeCredentials.guild_id) == guild_pk
        )
    )
    stored_blob = creds_result.one_or_none()

    # Fetch guild-level env vars from foreman config and pass them to the worker.
    foreman_env_vars: dict[str, str] = {}
    if guild_pk is not None:
        guild_res = await db.exec(
            select(col(Guild.foreman_config)).where(col(Guild.id) == guild_pk)
        )
        guild_cfg = guild_res.one_or_none() or {}
        foreman_env_vars = {
            e["key"]: e["value"]
            for e in (guild_cfg.get("env_vars") or [])
            if e.get("key") and e.get("value") is not None
        }

    env = build_spawn_worker_env(
        guild_id=guild_id,
        repos=repos,
        worker_name=worker_name,
        source_env=dict(os.environ),
        claude_oauth_token=decode_claude_oauth_token(stored_blob),
        worker_id=worker_id,
        auth_token=auth_token,
        agent_count=agent_count,
        tools=tools_list or None,
        extra_env=foreman_env_vars or None,
    )

    try:
        docker_client = await _get_docker_client()
        image = os.environ.get("WORKER_IMAGE", "pioneer-square-worker")

        network = None
        try:
            me = docker_client.containers.get(os.environ.get("HOSTNAME", ""))
            network = next(iter(me.attrs["NetworkSettings"]["Networks"].keys()), None)
        except Exception as e:
            logger.warning(
                "Docker network detection failed, starting without explicit network: %s",
                e,
            )

        labels = {
            "com.pioneer.kind": "worker",
            "com.pioneer.guild": guild_id,
        }
        container_name = f"pioneer-worker-{guild_id}-{secrets.token_hex(3)}"
        run_kwargs: dict = dict(
            image=image,
            environment=env,
            detach=True,
            remove=True,
            labels=labels,
            name=container_name,
        )
        if network:
            run_kwargs["network"] = network

        container = await asyncio.to_thread(docker_client.containers.run, **run_kwargs)
        # Persist container id and version so the lifecycle module can force-kill
        # this container if the backend is redeployed with a different version.
        from worker_lifecycle import record_worker_spawn as _record_worker_spawn  # noqa: PLC0415

        await _record_worker_spawn(db, worker_id, container.id)
        result_text = json.dumps(
            {
                "worker_id": worker_id,
                "container_id": container.id[:12],
                "repos": repos,
                "name": worker_name,
            }
        )
        logger.info(
            "spawn_worker: guild=%s worker_id=%s container=%s repos=%s",
            guild_id,
            worker_id,
            container.id[:12],
            repos,
        )
        return result_text, False
    except ImportError:
        await db.exec(
            update(Worker).where(col(Worker.id) == worker_id).values(state="spawn_failed")
        )
        await db.commit()
        return (
            f"Worker pre-registered as {worker_id} but the Docker SDK is not "
            "installed on the backend — container was NOT started. "
            f"To start manually: PIONEER_WORKER_ID={worker_id} "
            f"PIONEER_AUTH_TOKEN={auth_token} "
            "<worker-start-command>"
        ), True
    except Exception as exc:
        await db.exec(
            update(Worker).where(col(Worker.id) == worker_id).values(state="spawn_failed")
        )
        await db.commit()
        return (f"Worker pre-registered as {worker_id} but failed to start container: {exc}"), True


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------


async def exec_tools(guild_id: str, tool_uses: list, user_id: str | None = None) -> list:
    """Execute tool calls from the foreman AI and return tool-result blocks.

    Independent tool calls in the same batch run concurrently — each opens its
    own DB session and the GitHub helpers already hop to a thread pool, so
    parallelism is safe and reduces user-visible latency when Claude emits
    several tools in one turn (a common case for read-only lookups).
    Results are returned in the same order as *tool_uses* to match the
    Anthropic API's tool_result contract.

    *user_id* identifies the human whose foreman session is running. It's
    stamped onto any tasks created by ``create_task`` / ``assign_task`` so
    worker-driven events later route back to the same user thread.
    """
    coros = [_exec_one_tool(guild_id, tu, user_id) for tu in tool_uses]
    return list(await asyncio.gather(*coros))


async def _exec_one_tool(guild_id: str, tu, user_id: str | None = None) -> dict:
    """Execute a single tool call and return its tool_result block."""
    inp = tu.input
    result_text = ""
    is_error = False
    _ctx_token = _api_calls_ctx.set([])
    try:
        db = await get_db()
        try:
            from auth_deps import get_guild_pk

            guild_pk = await get_guild_pk(db, guild_id)
            if tu.name == "create_task":
                # No lock needed: creates an unassigned task (worker_id=None) so
                # there is no worker state to race on. Task ID collisions are
                # statistically negligible and caught by the DB unique constraint.
                name = (inp.get("name") or "")[:80]
                desc = inp.get("description", name)
                phase = inp.get("phase", "execute")
                task_id = "t-" + "".join(
                    random.choices(string.ascii_lowercase + string.digits, k=6)
                )
                created_at = datetime.now(UTC)
                db.add(
                    Task(
                        id=task_id,
                        worker_id=None,
                        guild_id=guild_pk or 0,
                        name=name,
                        description=desc,
                        tool="claude",
                        state="pending",
                        phase=phase,
                        created_at=created_at,
                        user_id=user_id,
                    )
                )
                await db.commit()
                await broadcast(
                    guild_id,
                    TaskCreatedMsg(
                        taskId=task_id,
                        name=name,
                        description=desc,
                        phase=phase,
                        state="pending",
                        createdAt=created_at.isoformat(),
                    ).model_dump(by_alias=True, exclude_none=True),
                )
                result_text = (
                    f"Task {task_id} created: '{name}'. Reference this task_id in assign_task."
                )

            elif tu.name == "assign_task":
                wid = inp["worker_id"]
                desc = inp.get("description", "")
                phase = inp.get("phase", "execute")
                requested_tool: str | None = inp.get("tool")
                tool = requested_tool or "claude"  # may be replaced below during tool validation
                model = inp.get("model") or None
                provider = inp.get("provider") or None
                # Always compute the tier from phase+tool upfront so it can be
                # persisted on the task row regardless of whether model is
                # auto-selected or caller-specified.
                from util.model_tiers import select_model_tier as _select_tier  # noqa: PLC0415

                model_tier = _select_tier(phase, tool)
                existing_task_id = inp.get("task_id")
                guild_result = await db.exec(
                    select(col(Guild.primary_repo)).where(col(Guild.id) == guild_pk)
                )
                primary_repo: str | None = guild_result.one_or_none()
                repos: list[str] = inp.get("repos") or ([primary_repo] if primary_repo else [])
                worker_result = await db.exec(
                    select(
                        col(Worker.id),
                        col(Worker.repos),
                        col(Worker.org),
                        col(Worker.tools),
                        col(Worker.provider),
                    ).where(col(Worker.id) == wid, col(Worker.guild_id) == guild_pk)
                )
                worker_row = worker_result.one_or_none()
                if not worker_row:
                    result_text = f"Worker {wid} not found — task NOT queued."
                    is_error = True
                else:
                    worker_tools: list[str] = json.loads(worker_row.tools or "[]")
                    worker_provider: str | None = worker_row.provider
                    # Filter model selection to only provider-compatible models.
                    if worker_provider and not is_error:
                        from models import ModelCatalog  # noqa: PLC0415

                        if model:
                            catalog_check = await db.exec(
                                select(col(ModelCatalog.model_id)).where(
                                    col(ModelCatalog.provider) == worker_provider,
                                    col(ModelCatalog.model_id) == model,
                                )
                            )
                            if catalog_check.one_or_none() is None:
                                result_text = (
                                    f"Model {model!r} is not available for provider "
                                    f"{worker_provider!r}. Use GET /api/models to see "
                                    "available models for each provider."
                                )
                                is_error = True
                        else:
                            # Auto-select: use pre-computed model_tier → best model from catalog.
                            from util.model_tiers import get_model_for_tier  # noqa: PLC0415
                            from util.models_dev import get_providers_from_db  # noqa: PLC0415

                            catalog = await get_providers_from_db(db)
                            model = get_model_for_tier(model_tier, worker_provider, catalog)
                    # For Bedrock workers, resolve the short model ID to the
                    # canonical inference-profile ARN stored in the catalog.
                    # The Claude CLI on Bedrock requires the full ARN; short IDs
                    # from models.dev are rejected by the InvokeModel API.
                    if worker_provider == "bedrock" and model and not is_error:
                        from models import ModelCatalog  # noqa: PLC0415

                        bedrock_id_result = await db.exec(
                            select(col(ModelCatalog.bedrock_model_id)).where(
                                col(ModelCatalog.provider) == "bedrock",
                                col(ModelCatalog.model_id) == model,
                            )
                        )
                        resolved = bedrock_id_result.one_or_none()
                        if resolved:
                            model = resolved
                        else:
                            logger.warning(
                                "assign_task: no Bedrock ARN for model %r "
                                "(catalog may need a refresh with AWS credentials configured) "
                                "— using short ID as fallback; worker may fail at inference time",
                                model,
                            )

                    if requested_tool is None:
                        tool = worker_tools[0] if worker_tools else "claude"
                    elif worker_tools and requested_tool not in worker_tools:
                        available = ", ".join(worker_tools)
                        result_text = (
                            f"Worker {wid} does not support tool {requested_tool!r}. "
                            f"Available tools: {available}"
                        )
                        is_error = True
                    elif not worker_tools:
                        # legacy worker: no tools registered, accept any requested tool
                        tool = requested_tool
                    else:
                        tool = requested_tool
                    if not is_error and repos:
                        worker_repos: list[str] = json.loads(worker_row.repos or "[]")
                        worker_org: str | None = worker_row.org
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
                    # Prevent two concurrent foreman runs from double-assigning
                    # the same idle worker.  Lock covers the window from worker
                    # selection through task row written + worker notified.
                    assign_lock_key = f"assign_task:{wid}"
                    assign_lock_id = "".join(
                        random.choices(string.ascii_lowercase + string.digits, k=8)
                    )
                    lock_acquired = await LockService(db).acquire(
                        assign_lock_key, owner=assign_lock_id, ttl_seconds=60
                    )
                    await db.commit()
                    if not lock_acquired:
                        result_text = (
                            f"Worker {wid} is already being assigned a task by a concurrent "
                            "foreman run. Retry after the current assignment completes."
                        )
                        is_error = True
                    else:
                        try:
                            # Re-check worker availability inside the lock to close the
                            # TOCTOU window: two concurrent foremen may have both seen the
                            # worker as available before either acquired the lock.
                            worker_recheck = await db.exec(
                                select(col(Worker.id))
                                .where(
                                    col(Worker.id) == wid,
                                    col(Worker.state) != "offline",
                                )
                                .limit(1)
                            )
                            if not worker_recheck.one_or_none():
                                result_text = (
                                    f"Worker {wid} went offline — task NOT assigned. "
                                    "Pick a different worker and retry."
                                )
                                is_error = True
                            elif existing_task_id:
                                name_override = inp.get("name")
                                update_values: dict = {
                                    "worker_id": wid,
                                    "description": desc,
                                    "tool": tool,
                                    "model": model,
                                    "model_tier": model_tier,
                                    "provider": provider,
                                    "phase": phase,
                                    "state": "pending",
                                }
                                if name_override:
                                    update_values["name"] = name_override
                                if inp.get("issue_number") is not None:
                                    update_values["issue_number"] = inp["issue_number"]
                                if inp.get("issue_repo"):
                                    update_values["issue_repo"] = inp["issue_repo"]
                                await db.exec(
                                    update(Task)
                                    .where(
                                        col(Task.id) == existing_task_id,
                                        col(Task.guild_id) == guild_pk,
                                    )
                                    .values(**update_values)
                                )
                                await db.commit()
                                name_result = await db.exec(
                                    select(col(Task.name)).where(col(Task.id) == existing_task_id)
                                )
                                task_name = name_result.one_or_none() or desc[:60]
                                task_id = existing_task_id
                                await broadcast(
                                    guild_id,
                                    TaskAssignedMsg(
                                        workerId=wid,
                                        taskId=task_id,
                                        name=task_name,
                                        description=desc,
                                        tool=tool,
                                        model=model,
                                        provider=provider,
                                        phase=phase,
                                        issueNumber=inp.get("issue_number"),
                                        issueRepo=inp.get("issue_repo"),
                                        repos=repos,
                                    ).model_dump(by_alias=True, exclude_none=True),
                                )
                                result_text = f"Task {task_id} assigned to {wid}."
                            else:
                                name = inp.get("name") or desc[:60]
                                parent_task_id = inp.get("parent_task_id")
                                task_id = "t-" + "".join(
                                    random.choices(string.ascii_lowercase + string.digits, k=6)
                                )
                                created_at = datetime.now(UTC)
                                db.add(
                                    Task(
                                        id=task_id,
                                        worker_id=wid,
                                        guild_id=guild_pk or 0,
                                        name=name,
                                        description=desc,
                                        tool=tool,
                                        model=model,
                                        model_tier=model_tier,
                                        provider=provider,
                                        issue_number=inp.get("issue_number"),
                                        issue_repo=inp.get("issue_repo"),
                                        state="pending",
                                        phase=phase,
                                        parent_task_id=parent_task_id,
                                        created_at=created_at,
                                        user_id=user_id,
                                    )
                                )
                                await db.commit()
                                await broadcast(
                                    guild_id,
                                    TaskAssignedMsg(
                                        workerId=wid,
                                        taskId=task_id,
                                        name=name,
                                        description=desc,
                                        tool=tool,
                                        model=model,
                                        provider=provider,
                                        phase=phase,
                                        parentTaskId=parent_task_id,
                                        issueNumber=inp.get("issue_number"),
                                        issueRepo=inp.get("issue_repo"),
                                        repos=repos,
                                    ).model_dump(by_alias=True, exclude_none=True),
                                )
                                result_text = f"Task {task_id} queued for {wid}."
                        finally:
                            await LockService(db).release(assign_lock_key, owner=assign_lock_id)
                            await db.commit()

            elif tu.name == "send_followup":
                task_id = inp["task_id"]
                instructions = inp["instructions"]
                preferred_worker_id = inp.get("preferred_worker_id")
                result = await db.exec(
                    select(
                        col(Task.worker_id),
                        col(Task.state),
                        col(Task.branch),
                        col(Task.description),
                        col(Task.name),
                        col(Task.tool),
                        col(Task.model),
                        col(Task.provider),
                        col(Task.issue_number),
                        col(Task.issue_repo),
                    ).where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
                )
                row = result.one_or_none()
                if not row:
                    result_text = f"Task {task_id} not found."
                else:
                    (
                        original_worker_id,
                        prior_state,
                        branch,
                        task_desc,
                        task_name,
                        task_tool,
                        task_model,
                        task_provider,
                        task_issue_number,
                        task_issue_repo,
                    ) = row
                    target_worker_id = await _select_followup_worker(
                        db,
                        guild_id=guild_id,
                        original_worker_id=original_worker_id,
                        preferred_worker_id=preferred_worker_id,
                    )
                    if not target_worker_id:
                        result_text = (
                            f"No idle worker available to continue task {task_id} on branch "
                            f"{branch or '<unknown>'}. Wait for one to come online or shut "
                            "down a busy worker before retrying."
                        )
                        is_error = True
                    elif not branch:
                        result_text = (
                            f"Task {task_id} has no branch recorded — can't dispatch a "
                            "follow-up. The task may have failed before its first push."
                        )
                        is_error = True
                    else:
                        # Atomically acquire the follow-up lock to prevent two
                        # concurrent foreman runs from both dispatching a worker.
                        lock_id = "".join(
                            random.choices(string.ascii_lowercase + string.digits, k=8)
                        )
                        lock_acquired = await LockService(db).acquire(
                            f"task:{task_id}", owner=lock_id
                        )
                        await db.commit()
                        if not lock_acquired:
                            # Task already locked by a concurrent follow-up —
                            # queue this request for replay when the lock releases.
                            db.add(
                                TaskEvent(
                                    task_id=task_id,
                                    event_type="pending-followup",
                                    payload_json=json.dumps(
                                        {
                                            "instructions": instructions,
                                            "preferred_worker_id": preferred_worker_id,
                                        }
                                    ),
                                    created_at=datetime.now(UTC),
                                )
                            )
                            await db.commit()
                            result_text = (
                                f"Task {task_id} is locked by an in-progress follow-up. "
                                "Instructions have been queued and will be passed to the "
                                "foreman when the current follow-up completes."
                            )
                        else:
                            update_vals: dict = {
                                "state": "working",
                                "phase": "followup",
                                "worker_id": target_worker_id,
                            }
                            if prior_state in ("done", "failed", "cancelled"):
                                # Re-opening a terminal task: clear soft-delete so it
                                # reappears in the live task list and isn't auto-purged.
                                update_vals["deleted_at"] = None
                            await db.exec(
                                update(Task).where(col(Task.id) == task_id).values(**update_vals)
                            )
                            await db.commit()
                            await broadcast(
                                guild_id,
                                TaskUpdateMsg(
                                    taskId=task_id,
                                    state="working",
                                    workerId=target_worker_id,
                                    deletedAt=None,
                                ).model_dump(by_alias=True, exclude_none=True),
                            )
                            followup_worker_result = await db.exec(
                                select(col(Worker.tools)).where(col(Worker.id) == target_worker_id)
                            )
                            followup_worker_tools_json = followup_worker_result.one_or_none()
                            if isinstance(followup_worker_tools_json, str):
                                followup_worker_tools: list[str] = json.loads(
                                    followup_worker_tools_json or "[]"
                                )
                            else:
                                followup_worker_tools = followup_worker_tools_json or []
                            await broadcast(
                                guild_id,
                                TaskFollowupMsg(
                                    workerId=target_worker_id,
                                    taskId=task_id,
                                    name=task_name or "",
                                    description=task_desc or "",
                                    tool=task_tool
                                    or (
                                        followup_worker_tools[0]
                                        if followup_worker_tools
                                        else "claude"
                                    ),
                                    model=task_model,
                                    provider=task_provider,
                                    branch=branch,
                                    instructions=instructions,
                                    issueNumber=task_issue_number,
                                    issueRepo=task_issue_repo,
                                ).model_dump(by_alias=True, exclude_none=True),
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
                raw_outcome = inp.get("outcome", "done")
                outcome = raw_outcome if raw_outcome in ("done", "failed") else "done"
                if outcome != raw_outcome:
                    logger.warning(
                        "finalize_task: unknown outcome %r, defaulting to 'done'", raw_outcome
                    )
                deleted_at, err = _resolve_finalize_deleted_at(inp)
                if err:
                    result_text = err
                    is_error = True
                else:
                    result = await db.exec(
                        select(Task)
                        .where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
                        .with_for_update()
                    )
                    task = result.one_or_none()
                    if not task:
                        result_text = f"Task {task_id} not found."
                    else:
                        await db.exec(
                            update(Task)
                            .where(col(Task.id) == task_id)
                            .values(
                                state=outcome,
                                deleted_at=deleted_at,
                            )
                        )
                        # Discard any queued follow-up events — the task is closed.
                        await db.exec(delete(TaskEvent).where(col(TaskEvent.task_id) == task_id))
                        await LockService(db).release(f"task:{task_id}")
                        await db.commit()
                        await broadcast_msg(
                            guild_id,
                            TaskFinalizeMsg(workerId=task.worker_id, taskId=task_id),
                        )
                        await broadcast_msg(
                            guild_id,
                            TaskUpdateMsg(
                                taskId=task_id,
                                state=outcome,
                                deletedAt=deleted_at.isoformat()
                                if deleted_at is not None
                                else None,
                            ),
                        )
                        result_text = (
                            f"Task {task_id} finalized as {outcome}; soft-delete at "
                            f"{deleted_at.isoformat() if deleted_at is not None else 'unknown'}."
                        )

            elif tu.name == "message_worker":
                wid = inp["worker_id"]
                msg = inp["message"]
                await emit_terminal_line(guild_id, wid, f"[foreman] {msg}")
                await broadcast_msg(guild_id, WorkerMessageMsg(workerId=wid, message=msg))
                result_text = f"Message delivered to {wid}."

            elif tu.name == "redirect_task":
                task_id = inp["task_id"]
                instructions = inp["instructions"]
                result = await db.exec(
                    select(col(Task.worker_id), col(Task.state)).where(
                        col(Task.id) == task_id, col(Task.guild_id) == guild_pk
                    )
                )
                row = result.one_or_none()
                if not row:
                    result_text = f"Task {task_id} not found."
                else:
                    worker_id_val, state = row
                    if state in ("done", "failed", "cancelled"):
                        result_text = f"Task {task_id} is {state} — cannot redirect."
                    else:
                        await db.exec(
                            update(Task).where(col(Task.id) == task_id).values(state="working")
                        )
                        await db.commit()
                        await broadcast_msg(
                            guild_id,
                            TaskRedirectMsg(
                                workerId=worker_id_val, taskId=task_id, instructions=instructions
                            ),
                        )
                        await broadcast_msg(
                            guild_id, TaskUpdateMsg(taskId=task_id, state="working")
                        )
                        result_text = f"Redirect sent to {worker_id_val} for task {task_id}."

            elif tu.name == "cancel_task":
                task_id = inp["task_id"]
                reason = inp.get("reason", "")
                result = await db.exec(
                    select(col(Task.worker_id), col(Task.state)).where(
                        col(Task.id) == task_id, col(Task.guild_id) == guild_pk
                    )
                )
                row = result.one_or_none()
                if not row:
                    result_text = f"Task {task_id} not found."
                else:
                    worker_id_val, state = row
                    if state in ("done", "failed", "cancelled"):
                        result_text = f"Task {task_id} is already {state}."
                    else:
                        deleted_at = datetime.now(UTC) + timedelta(
                            seconds=DEFAULT_FINALIZE_TTL_SECONDS
                        )
                        await db.exec(
                            update(Task)
                            .where(col(Task.id) == task_id)
                            .values(
                                state="cancelled",
                                deleted_at=deleted_at,
                            )
                        )
                        await LockService(db).release(f"task:{task_id}")
                        await db.commit()
                        await broadcast_msg(
                            guild_id,
                            TaskCancelMsg(workerId=worker_id_val, taskId=task_id),
                        )
                        await broadcast_msg(
                            guild_id,
                            TaskUpdateMsg(
                                taskId=task_id,
                                state="cancelled",
                                deletedAt=deleted_at.isoformat(),
                            ),
                        )
                        result_text = f"Task {task_id} cancelled." + (
                            f" Reason: {reason}" if reason else ""
                        )

            elif tu.name == "shutdown_worker":
                wid = inp["worker_id"]
                reason = inp.get("reason", "")
                worker_result = await db.exec(
                    select(col(Worker.id)).where(
                        col(Worker.id) == wid, col(Worker.guild_id) == guild_pk
                    )
                )
                if worker_result.one_or_none() is None:
                    result_text = f"Worker {wid} not found."
                else:
                    await broadcast_msg(
                        guild_id, WorkerShutdownMsg(workerId=wid, reason=reason or None)
                    )
                    await db.exec(update(Worker).where(col(Worker.id) == wid).values(disabled=True))
                    await db.commit()
                    result_text = f"Shutdown signal sent to {wid}." + (
                        f" Reason: {reason}" if reason else ""
                    )

            elif tu.name == "spawn_worker":
                result_text, is_error = await spawn_worker(inp, guild_id, guild_pk, db)

            elif tu.name == "get_task_status":
                task_id = inp["task_id"]
                limit = min(int(inp.get("log_lines", 10)), 50)
                task_result = await db.exec(
                    select(Task).where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
                )
                task = task_result.one_or_none()
                if not task:
                    result_text = f"Task {task_id} not found."
                else:
                    agent_info = None
                    if task.worker_id:
                        agent_result = await db.exec(
                            select(col(Agent.id), col(Agent.state))
                            .where(
                                col(Agent.worker_id) == task.worker_id,
                                col(Agent.state) != "offline",
                            )
                            .limit(1)
                        )
                        agent_row = agent_result.one_or_none()
                        if agent_row:
                            agent_info = {"agent_id": agent_row[0], "agent_state": agent_row[1]}
                    logs_result = await db.exec(
                        select(col(TaskLog.timestamp), col(TaskLog.line))
                        .where(col(TaskLog.task_id) == task_id)
                        .order_by(col(TaskLog.id).desc())
                        .limit(limit)
                    )
                    log_rows = list(reversed(logs_result.all()))
                    result_text = json.dumps(
                        {
                            "id": task.id,
                            "name": task.name,
                            "state": task.state,
                            "phase": task.phase,
                            "worker_id": task.worker_id,
                            "agent": agent_info,
                            "branch": task.branch,
                            "pr_url": task.pr_url,
                            "created_at": task.created_at,
                            "deleted_at": task.deleted_at,
                            "recent_logs": [{"time": r[0], "line": r[1]} for r in log_rows],
                        },
                        default=_json_default,
                    )
        finally:
            await db.close()

        # GitHub tools — use guild's OAuth token
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
            logger.info("Executing GitHub tool %s with input %s", tu.name, inp)
            creds = await _guild_github_token(guild_id)
            if not creds:
                result_text = (
                    "No GitHub token found for this guild — user must connect GitHub first."
                )
                is_error = True
            else:
                token, username = creds
                try:
                    if tu.name == "list_github_issues":
                        repo = inp["repo"]
                        state = inp.get("state", "open")
                        limit = min(int(inp.get("limit", 20)), 50)
                        issues = await _to_thread(
                            _gh_api,
                            f"/repos/{repo}/issues?state={state}&per_page={limit}",
                            token,
                        )
                        trimmed = [
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
                        result_text = json.dumps(trimmed)

                    elif tu.name == "get_github_issue":
                        repo = inp["repo"]
                        num = int(inp["issue_number"])
                        issue = await _to_thread(_gh_api, f"/repos/{repo}/issues/{num}", token)
                        comments_raw = await _to_thread(
                            _gh_api, f"/repos/{repo}/issues/{num}/comments?per_page=20", token
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
                        prs = await _to_thread(
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
                        await _to_thread(
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
                        issue = await _to_thread(
                            _gh_api_post, f"/repos/{repo}/issues", token, payload
                        )
                        result_text = json.dumps(
                            {
                                "number": issue["number"],
                                "url": issue["html_url"],
                                "title": issue["title"],
                            }
                        )

                    elif tu.name == "get_pr_status":
                        repo = inp["repo"]
                        num = int(inp["pr_number"])
                        pr = await _to_thread(_gh_api, f"/repos/{repo}/pulls/{num}", token)
                        reviews_raw = await _to_thread(
                            _gh_api,
                            f"/repos/{repo}/pulls/{num}/reviews?per_page=20",
                            token,
                        )
                        head_sha = (pr.get("head") or {}).get("sha")
                        check_runs: list = []
                        if head_sha:
                            crs = await _to_thread(
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

                    elif tu.name == "search_github_issues":
                        repo = inp["repo"]
                        query = inp["query"]
                        state = inp.get("state", "open")
                        state_q = "" if state == "all" else f"+state:{state}"
                        search_url = (
                            f"/search/issues?q={urllib.parse.quote(query)}"
                            f"+repo:{repo}{state_q}&per_page=10&sort=created&order=desc"
                        )
                        data = await _to_thread(_gh_api, search_url, token)
                        items = data.get("items", []) if isinstance(data, dict) else data
                        result_text = json.dumps(
                            [
                                {
                                    "number": i["number"],
                                    "title": i["title"],
                                    "state": i["state"],
                                    "url": i["html_url"],
                                    "labels": [l["name"] for l in i.get("labels", [])],
                                    "assignees": [a["login"] for a in i.get("assignees", [])],
                                }
                                for i in items
                            ]
                        )

                    elif tu.name == "review_pr":
                        pr_url = inp["pr_url"]
                        logger.info("guild=%s review_pr: pr_url=%s", guild_id, pr_url)
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
                            from foreman.a2a_client import A2AClient, _guild_caller_domain

                            review_agent = os.environ.get(
                                "REVIEWER_AGENT_URL", "https://agent.meyers.life"
                            )
                            client = A2AClient(f"{review_agent.rstrip('/')}/.well-known/agent.json")
                            try:
                                a2a_result = await client.review_pr(
                                    pr_url,
                                    caller_domain=_guild_caller_domain(guild_id),
                                    private_key_pem=await _guild_private_key_pem(guild_id),
                                )
                            except urllib.error.HTTPError as exc:
                                try:
                                    err_body = exc.read().decode(errors="replace")
                                except Exception:
                                    err_body = ""
                                logger.error(
                                    "guild=%s review_pr: mcp_request_failed pr_url=%s status=%d err_body=%.500s",
                                    guild_id,
                                    pr_url,
                                    exc.code,
                                    err_body,
                                    exc_info=True,
                                )
                                raise
                            except Exception:
                                logger.error(
                                    "guild=%s review_pr: mcp_request_failed pr_url=%s",
                                    guild_id,
                                    pr_url,
                                    exc_info=True,
                                )
                                raise
                            github_event, review_body = _extract_review_data(a2a_result)
                            logger.info(
                                "guild=%s review_pr: verdict=%s summary_preview=%.200s",
                                guild_id,
                                github_event,
                                review_body,
                            )
                            try:
                                threads_resolved = await _supersede_prior_bot_reviews(
                                    pr_repo, pr_number, username, token
                                )
                                if threads_resolved:
                                    logger.info(
                                        "guild=%s review_pr: resolved %d thread(s) from"
                                        " prior review(s) on %s#%d",
                                        guild_id,
                                        threads_resolved,
                                        pr_repo,
                                        pr_number,
                                    )
                            except Exception as _sup_exc:
                                logger.warning(
                                    "guild=%s review_pr: thread resolution step failed (non-fatal): %s",
                                    guild_id,
                                    _sup_exc,
                                )
                            review_data = await _to_thread(
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
                        logger.info(
                            "guild=%s review_pr_internal: pr_url=%s action=%s",
                            guild_id,
                            pr_url,
                            action,
                        )
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
                                _to_thread(
                                    _gh_api,
                                    f"/repos/{pr_repo}/pulls/{pr_number}",
                                    token,
                                ),
                                _to_thread(
                                    _gh_api_diff,
                                    f"/repos/{pr_repo}/pulls/{pr_number}",
                                    token,
                                ),
                            )
                            pr_title = pr_data.get("title", "")
                            pr_body_text = pr_data.get("body") or "(no description)"
                            base_ref = (pr_data.get("base") or {}).get("ref", "")
                            head_ref = (pr_data.get("head") or {}).get("ref", "")

                            try:
                                _ai = make_anthropic_client()
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
                                    model=get_foreman_model(),
                                    max_tokens=2048,
                                    messages=[{"role": "user", "content": review_prompt}],
                                )
                                review_json = _parse_review_from_claude(ai_msg.content[0].text)
                            except Exception as exc:
                                logger.error(
                                    "guild=%s review_pr_internal: ai generation failed: %s",
                                    guild_id,
                                    exc,
                                    exc_info=True,
                                )
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
                            ]

                            try:
                                threads_resolved = await _supersede_prior_bot_reviews(
                                    pr_repo, pr_number, username, token
                                )
                                if threads_resolved:
                                    logger.info(
                                        "guild=%s review_pr_internal: resolved %d thread(s)"
                                        " from prior review(s) on %s#%d",
                                        guild_id,
                                        threads_resolved,
                                        pr_repo,
                                        pr_number,
                                    )
                            except Exception as _sup_exc:
                                logger.warning(
                                    "guild=%s review_pr_internal: thread resolution step failed"
                                    " (non-fatal): %s",
                                    guild_id,
                                    _sup_exc,
                                )
                            try:
                                review_data = await _to_thread(
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
                                logger.warning(
                                    "guild=%s review_pr_internal: inline comments rejected, "
                                    "retrying without them",
                                    guild_id,
                                )
                                review_data = await _to_thread(
                                    _gh_api_post,
                                    f"/repos/{pr_repo}/pulls/{pr_number}/reviews",
                                    token,
                                    {"body": summary_text, "event": action, "comments": []},
                                )
                                gh_comments = []

                            logger.info(
                                "guild=%s review_pr_internal: review=%s verdict=%s comments=%d",
                                guild_id,
                                review_data.get("id"),
                                action,
                                len(gh_comments),
                            )
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

        # dnsid CLI — resolve / sign / verify
        if tu.name == "dnsid":
            logger.info("dnsid tool: input=%s", inp)
            command = inp.get("command", "")
            if not command:
                result_text = "dnsid requires command (resolve, sign, verify)"
                is_error = True
            else:
                try:
                    pem = await _guild_private_key_pem(guild_id) if command == "sign" else None
                    result_text = json.dumps(await _run_dnsid(command, inp, pem))
                except (ValueError, RuntimeError) as exc:
                    result_text = str(exc)
                    is_error = True
                except Exception as exc:
                    result_text = f"dnsid {command} failed: {exc}"
                    is_error = True

        # A2A agent call — no GitHub token or DB required
        if tu.name == "call_agent":
            logger.info("call_agent: input=%s", inp)
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
                    card = await _to_thread(_fetch_agent_card, card_url)
                    logger.debug("call_agent: fetched agent card from %s: %s", card_url, card)
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
                        response = await _to_thread(
                            _post_agent_task,
                            f"{agent_url}/jsonrpc",
                            task_body,
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
    finally:
        _api_call_log = _api_calls_ctx.get([])
        _api_calls_ctx.reset(_ctx_token)

    # Surface GitHub request IDs in error responses so failures can be correlated
    # with provider-side logs without digging through the database.
    if is_error and _api_call_log:
        req_ids = [c["x_github_request_id"] for c in _api_call_log if c.get("x_github_request_id")]
        if req_ids:
            result_text = f"{result_text}\n[x-github-request-id: {', '.join(req_ids)}]"

    block: dict = {"type": "tool_result", "tool_use_id": tu.id, "content": result_text}
    if is_error:
        block["is_error"] = True
    if _api_call_log:
        block["api_calls"] = _api_call_log
    return block
