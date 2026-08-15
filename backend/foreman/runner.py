"""Foreman AI runner: conversation management and main loop (embedded)."""

import asyncio
import json
import logging
import os
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import discord_notifier
from auth_deps import get_guild_pk
from database import AsyncSessionLocal, get_db
from events import broadcast_msg
from foreman.constants import (
    _24H_SECS,
    _HUMAN_TURN_WINDOW,
    _TERMINAL_STATES,
    MAX_FOREMAN_ROUNDS,
)
from foreman.github_url_parser import annotate_message as _annotate_github_urls
from foreman.llm import (
    ANTHROPIC_SDK_PROVIDERS,
    HAS_ANTHROPIC,
    BedrockModelNotConfiguredError,
    call_anthropic,
    get_foreman_model,
    make_anthropic_client,
)
from foreman.message_utils import (
    _inject_state_preamble,
    _json_default,
    _serialize_content,
    _stamp_message_cache_breakpoint,
    _summarize_task,
    prune_history,
    strip_orphaned_tool_results,
    truncate_tool_result,
)
from foreman.prompt import (
    build_child_state_preamble,
    build_child_system_blocks,
    build_state_preamble,
    build_system_blocks,
    build_system_prompt,
)
from foreman.proxy import call_foreman_api_proxy, has_foreman_proxy
from foreman.tools import exec_tools
from foreman.tools_schema import CHILD_FOREMAN_TOOLS, FOREMAN_TOOLS
from foreman.thread_service import resolve_thread_id
from models import (
    Agent,
    ApiRequestLog,
    ForemanTurn,
    GithubIssue,
    GithubPullRequest,
    Guild,
    GuildMember,
    Message,
    Task,
    Worker,
    live_tasks_filter,
)
from sqlalchemy import delete, func, tuple_
from sqlmodel import col, select
from util.tasks import spawn
from ws_types import ChatMsg, ForemanPollStatusMsg

logger = logging.getLogger(__name__)

POLL_MIN_SECS = 300  # initial poll interval: 5 minutes
# ponytail: raised 60→300s. Periodic checks were ~84% of foreman token spend;
# reset_foreman_poll slams the interval back to this floor on every event, so a
# busy day fired a full-context [periodic-check] every minute. 5min cuts that ~5×.
# Trade-off: stalled-task / devReady pickup latency goes 1min → 5min (fine).
# Also widens the _guild_active_recently window (below), which keys off this — a
# deliberate, benign coupling: "recently active" now means the last 5min.
POLL_MAX_SECS = 86400  # maximum poll interval: 24 hours

# Minimum age of a github_issues/github_pull_requests cache row before the
# stale-link refresh sweep (_refresh_stale_github_links) will refetch it. Keeps
# the sweep from re-hitting the GitHub API for the same row every poll tick.
_GITHUB_CACHE_REFRESH_STALE_SECS = 300

# Upper bound on rows fetched by _load_history before Python-side windowing,
# so query cost stays flat regardless of the table's total lifetime turn count.
_HISTORY_FETCH_LIMIT = 100

# Per-guild background poll task registry
_poll_tasks: dict[str, "asyncio.Task[None]"] = {}


# Per-guild run state, used to serialise foreman runs.  If a run is already in
# progress for a (guild, user) pair, new invocations are dropped rather than
# queued — the poll loop will re-trigger on the next tick.  Dropping (vs.
# queuing) keeps memory bounded and avoids stale snapshots piling up under load.
# Entries are popped in the finally block after each run so the dict stays small.
#
# Human-originated messages (Discord/web UI chat, user-requested follow-ups)
# are the one exception: they are never silently dropped. When the entry for
# their key is busy, they are appended to ``_human_queues`` instead, and
# drained (FIFO) once the in-flight run finishes — see ``run_foreman_ai``.
@dataclass
class _GuildRunLock:
    """Tracks whether a foreman run currently owns a given (guild, key) slot.

    ``busy`` is checked and flipped to True in the same synchronous step (no
    ``await`` in between) inside ``run_foreman_ai``, so two concurrent
    invocations can never both observe ``busy=False`` and both proceed:
    asyncio's single-threaded, cooperative scheduler only switches between
    coroutines at an ``await`` point, so nothing can interleave between the
    check and the claim. This is deliberately a plain flag rather than
    ``asyncio.Lock.locked()`` + ``await lock.acquire()`` — that pattern is
    *currently* just as atomic (acquiring an uncontended ``asyncio.Lock``
    never suspends), but it relies on ``Lock`` internals that a future edit
    could silently break (e.g. adding an ``await`` between the check and the
    acquire). A dedicated flag makes the invariant self-evident.
    """

    busy: bool = False


_guild_locks: dict[tuple[str, str | None], _GuildRunLock] = {}


def is_child_task_run_active(guild_id: str, task_id: str) -> bool:
    """Return True if a per-task child Foreman run is currently in-flight for *task_id*.

    Checks the same ``_guild_locks`` map used to serialise ``run_foreman_ai``
    invocations, keyed on ``(guild_id, "task:<task_id>")`` — the lock key a
    per-task child context claims for the duration of its run (see
    ``run_foreman_ai``). A *parent* run's task-mutating tool handlers
    (send_followup, redirect_task, cancel_task, finalize_task) call this
    before acting on a specific task_id so they can detect that task's own
    child context is mid-flight and skip rather than race it — closing the
    cross-context lock gap from issue #927 (parent and child runs otherwise
    key on different (guild_id, key) pairs and never serialise against each
    other for the same task).
    """
    state = _guild_locks.get((guild_id, f"task:{task_id}"))
    return bool(state and state.busy)


# Max number of queued human messages per (guild, user)/(guild, task) key.
# Bounded so a guild nobody is watching can't grow this without limit; the
# oldest entry is dropped (with a warning) once the cap is hit.
_HUMAN_QUEUE_MAX = 50

# FIFO queues of human messages that arrived while the foreman was busy,
# keyed by the same lock key used for ``_guild_locks``.
_human_queues: dict[tuple[str, str | None], "deque[_QueuedHumanTurn]"] = {}


@dataclass
class _QueuedHumanTurn:
    """One human message waiting for the busy foreman lock to free up."""

    guild_id: str
    human_message: str
    extra_context: str
    user_id: str | None
    task_id: str | None
    child: bool
    queued_at: str
    reply_channel_id: str | None = None
    trigger: str | None = None


# Monotonic timestamp (time.monotonic()) of the last foreman run that made at
# least one tool call, keyed by guild_id.  Used by reset_foreman_poll to decide
# whether to reset the backoff: only reset when the foreman was recently active.
_guild_last_action_at: dict[str, float] = {}


def _record_guild_action(guild_id: str) -> None:
    """Record that the foreman made tool calls for *guild_id* right now."""
    _guild_last_action_at[guild_id] = time.monotonic()


def _guild_active_recently(guild_id: str) -> bool:
    """Return True if the foreman made tool calls within the last POLL_MIN_SECS."""
    ts = _guild_last_action_at.get(guild_id)
    if ts is None:
        return False
    return (time.monotonic() - ts) <= POLL_MIN_SECS


# Module-level client — reused across calls so the underlying httpx connection
# pool isn't thrown away every invocation. Lazily initialised so import works
# without an API key (Anthropic) or AWS credentials (Bedrock).
_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = make_anthropic_client()
    return _anthropic_client


@dataclass
class _LLMCallResult:
    response: Any
    response_dict: dict[str, Any]
    request_id: str | None
    provider: str
    model: str


class _ProxyContentBlock(SimpleNamespace):
    """Attribute-access view of one content-block dict from a proxied response.

    The foreman API proxy returns plain JSON (dicts), not SDK model instances,
    so this mimics the Anthropic SDK's content-block attribute access
    (`.type`/`.text`/`.input`/...) for code shared with the local call path
    below, which gets real SDK objects from foreman.llm.call_anthropic.
    """

    def __init__(self, raw: dict[str, Any]):
        super().__init__(**raw)
        self._raw = raw

    def model_dump(self) -> dict[str, Any]:
        return self._raw


class _ProxyResponse(SimpleNamespace):
    """Anthropic-SDK-shaped wrapper around a proxy-normalised response dict."""

    def __init__(self, raw: dict[str, Any]):
        content = [_ProxyContentBlock(block) for block in raw.get("content") or []]
        usage = SimpleNamespace(**(raw.get("usage") or {}))
        super().__init__(
            id=raw.get("id"),
            type=raw.get("type", "message"),
            role=raw.get("role", "assistant"),
            model=raw.get("model"),
            content=content,
            stop_reason=raw.get("stop_reason"),
            stop_sequence=raw.get("stop_sequence"),
            usage=usage,
            _raw=raw,
        )

    def model_dump(self) -> dict[str, Any]:
        return self._raw


async def _call_llm(
    guild_id: str,
    *,
    client,
    provider: str,
    model: str,
    max_tokens: int,
    system_blocks: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: dict[str, Any] | None = None,
) -> _LLMCallResult:
    """Execute one LLM request.

    Either locally, via foreman.llm.call_anthropic against our own client (for
    providers in ANTHROPIC_SDK_PROVIDERS), or through the connected foreman API
    proxy — which can reach any provider, including OpenAI-compatible ones this
    embedded foreman never calls directly. Request/response translation for
    every provider lives in foreman.llm, shared with the proxy; this function
    only picks which of those two call paths to use.
    """
    if has_foreman_proxy(guild_id):
        data = await call_foreman_api_proxy(
            guild_id,
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
        )
        response_dict = data["response"]
        proxy_model = data.get("model") or response_dict.get("model") or model
        proxy_provider = data.get("provider") or "proxy"
        return _LLMCallResult(
            response=_ProxyResponse(response_dict),
            response_dict=response_dict,
            request_id=data.get("apiRequestId"),
            provider=proxy_provider,
            model=proxy_model,
        )

    if client is None:
        raise RuntimeError("No Anthropic client available and no external foreman proxy connected")

    response, request_id = await call_anthropic(
        client,
        model=model,
        max_tokens=max_tokens,
        system=system_blocks,
        messages=messages,
        tools=tools,
        tool_choice=tool_choice,
    )
    return _LLMCallResult(
        response=response,
        response_dict=response.model_dump(),
        request_id=request_id,
        provider=provider,
        model=model,
    )


async def resolve_foreman_client(guild_id: str, guild_cfg: dict) -> tuple[Any, str, str]:
    """Resolve the (client, effective_provider, model) triple for one guild's inference calls.

    Shared by every call site that talks to the foreman's configured LLM —
    the main run_foreman_ai loop and any tool handler (e.g. review_pr_internal)
    that needs its own one-off completion — so a guild's provider/model/env-var
    overrides (set via the settings dialogue) are honored everywhere, not just
    in the main loop. Returns client=None when a connected foreman API proxy
    will make the call instead (see _call_llm); the proxy owns providers
    outside ANTHROPIC_SDK_PROVIDERS (e.g. "openai") that this embedded foreman
    can't reach directly.
    """
    cfg_provider: str | None = guild_cfg.get("provider")
    cfg_model: str | None = guild_cfg.get("model")
    # Guild-configured env vars (settings dialogue) — these are otherwise only
    # injected into spawned workers, never the foreman process. They carry the
    # AI provider credentials configured via the dialogue (AWS_* for Bedrock,
    # ANTHROPIC_* for the direct API), so pass them to the client factory or the
    # foreman's own client can't authenticate.
    cfg_env_vars: dict[str, str] = {
        e["key"]: e["value"]
        for e in (guild_cfg.get("env_vars") or [])
        if e.get("key") and e.get("value") is not None
    }

    # Use a per-guild client when the config overrides the provider or
    # supplies its own env vars (e.g. Bedrock AWS credentials/region from
    # the settings dialogue, which otherwise never reach this process).
    effective_provider = cfg_provider or os.environ.get("FOREMAN_PROVIDER", "anthropic").lower()
    # Resolve the model before building the client (and before any task is
    # dispatched) so a stale guild config — saved before the #817 fix added
    # save-time validation, with no model or a placeholder ARN — fails here
    # with a clear error instead of surfacing deep inside the API call.
    proxy_available = has_foreman_proxy(guild_id)
    try:
        model = cfg_model or get_foreman_model(provider=effective_provider)
    except BedrockModelNotConfiguredError:
        if not proxy_available:
            raise
        model = cfg_model or os.environ.get("FOREMAN_MODEL", "external-proxy")

    client = None
    if not proxy_available and effective_provider not in ANTHROPIC_SDK_PROVIDERS:
        # This embedded foreman only calls the Anthropic SDK directly
        # (ANTHROPIC_SDK_PROVIDERS); any other provider — e.g. an
        # OpenAI-compatible endpoint — is only reachable through a
        # connected foreman API proxy, which owns that provider's
        # request/response translation (foreman.llm.call_openai_compatible).
        raise RuntimeError(
            f"FOREMAN_PROVIDER={effective_provider!r} is not an embedded provider "
            f"({sorted(ANTHROPIC_SDK_PROVIDERS)}) and no foreman API proxy is connected"
        )
    if not proxy_available and (cfg_provider or cfg_env_vars):
        client = make_anthropic_client(
            provider=effective_provider,
            extra_env=cfg_env_vars or None,
            model=cfg_model,
        )
    elif not proxy_available:
        client = _get_anthropic_client()

    return client, effective_provider, model


async def _load_foreman_config(guild_id: str) -> dict:
    """Load per-guild foreman config from the DB. Returns {} if unset."""
    async with AsyncSessionLocal() as db:
        result = await db.exec(select(col(Guild.foreman_config)).where(col(Guild.slug) == guild_id))
        raw = result.one_or_none()
        return raw if isinstance(raw, dict) else {}


async def _get_guild_user_id(guild_id: str) -> str | None:
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            return None
        result = await db.exec(
            select(col(GuildMember.user_id))
            .where(col(GuildMember.guild_id) == guild_pk, col(GuildMember.role) == "owner")
            .limit(1)
        )
        return result.one_or_none()
    finally:
        await db.close()


def _child_contexts_enabled() -> bool:
    """Whether per-task child contexts are enabled for the embedded foreman.

    Defaults on; set ``FOREMAN_CHILD_CONTEXTS`` to a falsy value
    (0/false/no/off) to fall back to the legacy single whole-guild context.
    See docs/foreman-per-task-context.md.
    """
    return os.environ.get("FOREMAN_CHILD_CONTEXTS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "",
    )


async def _load_history(guild_id: str, user_id: str, task_id: str | None = None) -> list[dict]:
    """Load the last _HUMAN_TURN_WINDOW non-tool-response turns (plus all tool exchange
    turns between them) as a list of Anthropic-API-compatible message dicts.

    Because the cutoff always lands on a human-initiated user turn, every
    assistant-turn / tool_result-user-turn pair that follows it is guaranteed to
    be included intact — no orphaned tool_use blocks, no synthetic repairs needed.

    When ``task_id`` is set, only turns tagged with that task are loaded — the
    isolated conversation thread for a per-task child context.
    """
    db = await get_db()
    try:
        guild_pk_val = await get_guild_pk(db, guild_id)
        stmt = select(ForemanTurn).where(
            col(ForemanTurn.guild_id) == guild_pk_val, col(ForemanTurn.user_id) == user_id
        )
        if task_id is not None:
            stmt = stmt.where(col(ForemanTurn.task_id) == task_id)
        else:
            # Parent-mode reads must never re-absorb per-task child conversations.
            stmt = stmt.where(col(ForemanTurn.task_id).is_(None))
        # Fetch only the most recent rows at the SQL level so query cost doesn't
        # scale with the table's total lifetime turn count; the Python-side
        # windowing below then trims this small set down further.
        result = await db.exec(
            stmt.order_by(col(ForemanTurn.id).desc()).limit(_HISTORY_FETCH_LIMIT)
        )
        turns = list(reversed(result.all()))
    finally:
        await db.close()

    logger.debug(
        "guild=%s _load_history: %d total turns in DB for user=%s", guild_id, len(turns), user_id
    )

    if not turns:
        return []

    # Walk backwards: find the index of the 5th-from-last non-tool-response user turn.
    cutoff = 0
    human_count = 0
    for i in range(len(turns) - 1, -1, -1):
        t = turns[i]
        if t.role == "user" and not t.is_tool_response:
            human_count += 1
            if human_count >= _HUMAN_TURN_WINDOW:
                cutoff = i
                break

    # Exclude system turns — they are persisted for auditing but must not appear
    # in the messages array sent to the Anthropic API (system prompt is a top-level param).
    messages = [
        {"role": t.role, "content": json.loads(t.content_json)}
        for t in turns[cutoff:]
        if t.role != "system"
    ]

    # Anthropic API requires the first message to have role "user"
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    logger.debug(
        "guild=%s _load_history: cutoff at turn index %d → %d messages after trim (roles: %s)",
        guild_id,
        cutoff,
        len(messages),
        [m["role"] for m in messages],
    )
    return messages


async def _save_turn(
    guild_id: str,
    user_id: str,
    role: str,
    content,
    *,
    is_tool_response: bool = False,
    parent_id: int | None = None,
    api_log_id: int | None = None,
    task_id: str | None = None,
) -> int:
    """Persist one turn to the DB. Returns the new row's id."""
    db = await get_db()
    try:
        guild_pk_val = await get_guild_pk(db, guild_id)
        turn = ForemanTurn(
            guild_id=guild_pk_val or 0,
            user_id=user_id,
            role=role,
            content_json=_serialize_content(content),
            is_tool_response=1 if is_tool_response else 0,
            parent_id=parent_id,
            created_at=datetime.now(UTC),
            request_id=api_log_id,
            task_id=task_id,
        )
        db.add(turn)
        await db.commit()
        await db.refresh(turn)
        return turn.id or 0
    finally:
        await db.close()


async def _create_api_request_log(
    model: str,
    system_blocks: list,
    messages: list,
    extra: dict,
    task_id: str | None = None,
    guild_id: str | None = None,
    user_id: str | None = None,
    trigger: str | None = None,
) -> int:
    """Insert an api_request_log row before making the Anthropic API call.

    Returns the new row's id so it can be updated after the call completes.
    """
    db = await get_db()
    try:
        log = ApiRequestLog(
            created_at=datetime.now(UTC),
            model=model,
            system=system_blocks,
            messages=messages,
            extra=extra or None,
            task_id=task_id,
            guild_id=await get_guild_pk(db, guild_id) if guild_id else None,
            user_id=user_id,
            trigger=trigger,
        )
        db.add(log)
        await db.commit()
        await db.refresh(log)
        if log.id is None:
            raise RuntimeError("api_request_log row has no id after commit")
        return log.id
    finally:
        await db.close()


async def _complete_api_request_log(
    log_id: int,
    *,
    request_id: str | None,
    response_dict: dict,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    stop_reason: str | None,
) -> None:
    """Update an api_request_log row after the Anthropic API call completes."""
    from sqlalchemy import update as sa_update

    db = await get_db()
    try:
        await db.exec(
            sa_update(ApiRequestLog)
            .where(col(ApiRequestLog.id) == log_id)
            .values(
                request_id=request_id,
                response=response_dict,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                stop_reason=stop_reason,
                completed_at=datetime.now(UTC),
            )
        )
        await db.commit()
    finally:
        await db.close()


async def _fetch_pr_status_lines(guild_id: str, pr_tasks: list[dict]) -> list[str]:
    """Fetch fresh GitHub PR status for each non-terminal task with an open PR.

    Called on every periodic-check cycle so the foreman can detect merges, CI
    failures, and review decisions without relying on webhook delivery. Best
    effort: an individual PR fetch failure (rate limit, deleted PR, missing
    token, etc.) is logged and skipped rather than aborting the whole poll.
    Also upserts each fetched PR into the ``github_pull_requests`` cache (see
    ``fetch_pr_status``) so a missed webhook doesn't leave the tasks/tree UI
    stale indefinitely.
    """
    if not pr_tasks:
        return []

    from foreman.tools import _PR_URL_RE, _guild_github_token, fetch_pr_status

    creds = await _guild_github_token(guild_id)
    if not creds:
        return []
    token, _username = creds

    lines: list[str] = []
    db = await get_db()
    try:
        for t in pr_tasks:
            pr_repo, pr_number = t.get("pr_repo"), t.get("pr_number")
            if not pr_repo or not pr_number:
                m = _PR_URL_RE.match((t.get("pr_url") or "").rstrip("/"))
                if not m:
                    continue
                pr_repo, pr_number = m.group(1), int(m.group(2))

            try:
                status = await fetch_pr_status(pr_repo, pr_number, token, db=db)
            except Exception:
                logger.warning(
                    "guild=%s task=%s failed to fetch PR status for %s#%s",
                    guild_id,
                    t["id"],
                    pr_repo,
                    pr_number,
                    exc_info=True,
                )
                continue

            checks = status.get("checks") or []
            check_summary = (
                ", ".join(f"{c['name']}={c['conclusion'] or c['status']}" for c in checks)
                if checks
                else "none"
            )
            reviews = status.get("reviews") or []
            review_summary = (
                ", ".join(f"{r['user']}={r['state']}" for r in reviews) if reviews else "none"
            )
            lines.append(
                f"- task {t['id']} PR {pr_repo}#{pr_number} ({t['pr_url']}): "
                f"state={status['state']} merged={status['merged']} "
                f"checks=[{check_summary}] reviews=[{review_summary}]"
            )
    finally:
        await db.close()
    return lines


# devReady label variants, checked case-insensitively (mirrors prompt.py step 3).
_DEVREADY_LABELS = ("devReady", "dev-ready", "ready-for-dev", "ready")


async def _fetch_devready_issues(guild_id: str, linked_issues: set[tuple[str, int]]) -> list[str]:
    """Pre-fetch unassigned devReady issues on the primary repo for pickup.

    Runs every periodic-check so the foreman acts on injected data instead of
    calling search_github_issues itself. Best effort: a search failure (rate
    limit, missing token, no primary repo) is logged and skipped. Already-linked
    issues (an existing non-terminal task references them) are filtered out here
    so the foreman only sees genuinely new work.
    """
    from foreman.tools import _gh_api, _guild_github_token, _to_thread

    creds = await _guild_github_token(guild_id)
    if not creds:
        return []
    token, _username = creds

    db = await get_db()
    try:
        row = await db.exec(select(col(Guild.primary_repo)).where(col(Guild.slug) == guild_id))
        primary_repo = row.one_or_none()
    finally:
        await db.close()
    if not primary_repo:
        return []

    # Merge results across label variants, deduping by issue number.
    seen: dict[int, dict] = {}
    for label in _DEVREADY_LABELS:
        query = urllib.parse.quote(f'label:"{label}" no:assignee')
        url = (
            f"/search/issues?q={query}+repo:{primary_repo}+state:open"
            "&per_page=10&sort=created&order=desc"
        )
        try:
            data = await _to_thread(_gh_api, url, token)
        except Exception:
            logger.warning(
                "guild=%s devReady search failed for label=%s", guild_id, label, exc_info=True
            )
            continue
        for i in data.get("items", []) if isinstance(data, dict) else data:
            seen.setdefault(i["number"], i)

    lines: list[str] = []
    for num, i in sorted(seen.items()):
        if (primary_repo, num) in linked_issues:
            continue  # an existing non-terminal task already owns this issue
        labels = ", ".join(l["name"] for l in i.get("labels", []))
        lines.append(f"- {primary_repo}#{num}: {i['title']} [labels: {labels}] ({i['html_url']})")
    return lines


async def _sweep_closed_issues(guild_id: str, linked_issues: set[tuple[str, int]]) -> None:
    """Sweep tasks whose linked GitHub issue has closed.

    Called on every periodic-check cycle alongside the PR-status refresh above,
    for every distinct ``(issue_repo, issue_number)`` linked by a non-terminal
    task (backed by the ``ix_tasks_issue_repo_issue_number`` index). This is a
    safety net for the ``issues`` webhook handler in ``routes.webhooks`` — it
    catches issues closed while a webhook delivery was missed — so it acts
    directly via ``finalize_closed_issue`` (no foreman/LLM round-trip needed)
    rather than merely nudging the foreman like the PR-status block does.
    Non-terminal linked tasks are never auto-closed; only legacy phase='issue'
    rows are finalized and already-terminal tasks swept. Best effort: an
    individual GitHub fetch failure is logged and skipped rather than aborting
    the sweep. Also upserts each fetched issue into the ``github_issues`` cache
    (see ``fetch_issue_state``) so a missed webhook doesn't leave the
    tasks/tree UI stale indefinitely.
    """
    if not linked_issues:
        return

    from foreman.tools import _guild_github_token, fetch_issue_state, finalize_closed_issue

    creds = await _guild_github_token(guild_id)
    if not creds:
        return
    token, _username = creds

    db = await get_db()
    try:
        guild_pk_val = await get_guild_pk(db, guild_id)
        for repo, number in sorted(linked_issues):
            try:
                state = await fetch_issue_state(repo, number, token, db=db)
            except Exception:
                logger.warning(
                    "guild=%s failed to fetch issue status for %s#%s",
                    guild_id,
                    repo,
                    number,
                    exc_info=True,
                )
                continue

            if state != "closed":
                continue

            finalized = await finalize_closed_issue(db, guild_pk_val, guild_id, repo, number)
            if finalized:
                logger.info(
                    "guild=%s finalized %d task(s) for closed issue %s#%s",
                    guild_id,
                    len(finalized),
                    repo,
                    number,
                )
    finally:
        await db.close()


async def _stale_cache_keys(
    db, model: type[GithubIssue] | type[GithubPullRequest], keys: set[tuple[str, int]]
) -> list[tuple[str, int]]:
    """Return the subset of *keys* whose cache row is missing or older than the staleness cutoff.

    A key with no cache row at all counts as stale (it has never been fetched).
    Shared by the issue and PR halves of ``_refresh_stale_github_links`` — the
    two cache tables share the same ``(repo, number)`` shape.
    """
    if not keys:
        return []
    cutoff = datetime.now(UTC) - timedelta(seconds=_GITHUB_CACHE_REFRESH_STALE_SECS)
    result = await db.exec(
        select(col(model.repo), col(model.number), col(model.last_refreshed_at)).where(
            tuple_(col(model.repo), col(model.number)).in_(keys)
        )
    )
    cached_at = {(repo, number): refreshed_at for repo, number, refreshed_at in result.all()}
    return sorted(k for k in keys if cached_at.get(k, cutoff) <= cutoff)


async def _refresh_stale_github_links(guild_id: str) -> None:
    """Refresh the github_issues/github_pull_requests cache for every linked task, terminal included.

    ``_sweep_closed_issues`` and ``_fetch_pr_status_lines`` above only refresh
    issues/PRs linked by *non-terminal* tasks, since those feed the foreman's
    own periodic-check narration. But the tasks/tree view renders every live
    task regardless of state — once a task's issue/PR closes and its last task
    goes terminal, those two sweeps stop looking at it and the cached status
    goes stale forever. This sweep scans every distinct issue/PR reference on
    live tasks (any state) and refetches cache rows older than
    ``_GITHUB_CACHE_REFRESH_STALE_SECS`` (or never fetched), independent of
    task state. Best effort: an individual fetch failure is logged and
    skipped rather than aborting the sweep.
    """
    from foreman.tools import _PR_URL_RE, _guild_github_token, fetch_issue_state, fetch_pr_status

    creds = await _guild_github_token(guild_id)
    if not creds:
        return
    token, _username = creds

    db = await get_db()
    try:
        guild_pk_val = await get_guild_pk(db, guild_id)
        result = await db.exec(
            select(
                col(Task.issue_repo),
                col(Task.issue_number),
                col(Task.pr_repo),
                col(Task.pr_number),
                col(Task.pr_url),
            ).where(col(Task.guild_id) == guild_pk_val, live_tasks_filter())
        )
        rows = result.all()

        issue_keys = {
            (r.issue_repo, r.issue_number) for r in rows if r.issue_repo and r.issue_number
        }
        pr_keys: set[tuple[str, int]] = set()
        for r in rows:
            if r.pr_repo and r.pr_number:
                pr_keys.add((r.pr_repo, r.pr_number))
            elif r.pr_url:
                m = _PR_URL_RE.match(r.pr_url.rstrip("/"))
                if m:
                    pr_keys.add((m.group(1), int(m.group(2))))

        stale_issues = await _stale_cache_keys(db, GithubIssue, issue_keys)
        stale_prs = await _stale_cache_keys(db, GithubPullRequest, pr_keys)
        if not stale_issues and not stale_prs:
            return

        logger.debug(
            "guild=%s _refresh_stale_github_links: refreshing %d issue(s), %d PR(s)",
            guild_id,
            len(stale_issues),
            len(stale_prs),
        )

        for repo, number in stale_issues:
            try:
                await fetch_issue_state(repo, number, token, db=db)
            except Exception:
                logger.warning(
                    "guild=%s failed to refresh cached issue status for %s#%s",
                    guild_id,
                    repo,
                    number,
                    exc_info=True,
                )

        for repo, number in stale_prs:
            try:
                await fetch_pr_status(repo, number, token, db=db)
            except Exception:
                logger.warning(
                    "guild=%s failed to refresh cached PR status for %s#%s",
                    guild_id,
                    repo,
                    number,
                    exc_info=True,
                )
    finally:
        await db.close()


async def _poll_loop(guild_id: str) -> None:
    """Background loop: check non-terminal tasks and trigger the foreman if any are found.

    Interval doubles each cycle from POLL_MIN_SECS up to POLL_MAX_SECS (or per-guild
    overrides). Cancelled (via reset_foreman_poll) whenever a significant event arrives.
    Runs for every guild regardless of whether an external foreman is connected —
    each tick is dispatched through ``_trigger_foreman``, which routes it to whichever
    foreman (embedded or external) currently owns this guild. The foreman-poll-status
    broadcast is sent after each poll so the UI can display the countdown to the
    *next* check.
    """
    interval = POLL_MIN_SECS
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

        # Bail if this task was superseded by a reset.
        if asyncio.current_task() is not _poll_tasks.get(guild_id):
            return

        try:
            # Reload config each cycle so guild owners see changes without restarting.
            cfg = await _load_foreman_config(guild_id)
            poll_max = int(cfg.get("poll_max_interval", POLL_MAX_SECS))

            db = await get_db()
            try:
                guild_pk_val = await get_guild_pk(db, guild_id)
                result = await db.exec(
                    select(
                        col(Task.id),
                        col(Task.state),
                        col(Task.name),
                        col(Task.pr_url),
                        col(Task.pr_number),
                        col(Task.pr_repo),
                        col(Task.phase),
                        col(Task.issue_repo),
                        col(Task.issue_number),
                    ).where(
                        col(Task.guild_id) == guild_pk_val,
                        ~col(Task.state).in_(list(_TERMINAL_STATES)),
                        live_tasks_filter(),
                    )
                )
                active_tasks = [dict(r._mapping) for r in result.all()]

                # Kept-alive done tasks (issue still open ⇒ deleted_at left NULL)
                # rely on the issue-close sweep to ever be stamped; a dropped
                # issues webhook would otherwise strand them live forever. Gather
                # their issues too so the sweep below covers them.
                # ponytail: re-polls each such open issue every cycle; add a
                # per-issue cooldown if the GitHub API budget bites.
                kept_alive_result = await db.exec(
                    select(col(Task.issue_repo), col(Task.issue_number))
                    .where(
                        col(Task.guild_id) == guild_pk_val,
                        col(Task.state).in_(list(_TERMINAL_STATES)),
                        col(Task.deleted_at).is_(None),
                        col(Task.issue_number).is_not(None),
                    )
                    .distinct()
                )
                kept_alive_issues = {(r[0], r[1]) for r in kept_alive_result.all() if r[0] and r[1]}

                # Opportunistically refresh the model catalog while we have a DB
                # session open. The function is a no-op when the catalog is fresh.
                from util.models_dev import refresh_model_catalog_if_stale as _refresh_catalog

                refreshed = await _refresh_catalog(db)
                if refreshed:
                    await db.commit()
            finally:
                await db.close()

            n = len(active_tasks)
            next_interval = min(interval * 2, poll_max)
            logger.debug(
                "guild=%s polling %d active tasks, next check in %.0fm",
                guild_id,
                n,
                next_interval / 60,
            )

            # Always fire a [periodic-check] — even with zero active tasks. An empty
            # board is exactly when the devReady sweep (which runs on every
            # periodic-check) needs to pull in new work; gating on active_tasks
            # silently stalled devReady pickup once a guild drained.
            linked_issues = {
                (t["issue_repo"], t["issue_number"])
                for t in active_tasks
                if t.get("issue_repo") and t.get("issue_number")
            } | kept_alive_issues
            # Sweep whenever any linked issue exists — kept-alive done tasks need
            # it even when there are no non-terminal tasks left on the board.
            if linked_issues:
                await _sweep_closed_issues(guild_id, linked_issues)

            # Independent of active_tasks/linked_issues above: refreshes the
            # github_issues/github_pull_requests cache for *every* live task's
            # linked issue/PR (terminal included), so the tasks/tree view never
            # shows indefinitely-stale status just because its tasks finished.
            await _refresh_stale_github_links(guild_id)

            # Thread-per-conversation health sweep (#1160/#1163): auto-archive
            # idle threads, auto-close idle-archived threads, and unlink dead
            # threads from non-terminal tasks. Best effort — never raises.
            from foreman.thread_maintenance import sweep_threads

            await sweep_threads(guild_id)

            if active_tasks:
                task_summary = "; ".join(f"{t['id']} ({t['state']})" for t in active_tasks)
                pr_tasks = [t for t in active_tasks if t.get("pr_url")]
                pr_status_lines = await _fetch_pr_status_lines(guild_id, pr_tasks)

                msg = (
                    f"[periodic-check] Automated status poll — {n} non-terminal "
                    f"task(s): {task_summary}. Check whether any are stalled. "
                    "Use get_task_status to inspect a task if it looks stuck."
                )
                if pr_status_lines:
                    msg += (
                        "\n\nFresh GitHub PR status (fetched this cycle for each "
                        "open-PR task):\n"
                        + "\n".join(pr_status_lines)
                        + "\nFor merged PRs, call finalize_task now. For CI failures or "
                        "requested changes on these PRs, call send_followup with concrete "
                        "fix instructions. Approved PRs with no open issues need no "
                        "further action."
                    )
                else:
                    msg += " If everything looks healthy, no action is needed."
            else:
                msg = "[periodic-check] Automated status poll — no non-terminal tasks."

            # Pre-fetched devReady issues ready for pickup (see prompt "Periodic
            # devReady issue pickup"). Injecting them here saves the foreman a
            # search_github_issues round-trip and guarantees the sweep runs.
            devready_lines = await _fetch_devready_issues(guild_id, linked_issues)
            if devready_lines:
                msg += (
                    "\n\nUnassigned devReady issues ready for pickup (fetched this "
                    "cycle, already deduped against active tasks):\n"
                    + "\n".join(devready_lines)
                    + "\nFor each, call claim_github_issue then create_task + assign_task "
                    "(passing issue_number and issue_repo on both). Skip any already "
                    "assigned to someone else."
                )

            # Deferred import: ws_handlers imports from the foreman package at
            # module load time, so importing it back at module scope here would
            # create a circular import.
            from ws_handlers import _trigger_foreman

            await _trigger_foreman(
                guild_id, "periodic-check", msg, task_name=f"foreman.poll:{guild_id}"
            )

            # Announce next check interval so the UI can display a countdown.
            interval = next_interval
            await broadcast_msg(guild_id, ForemanPollStatusMsg(nextCheckIn=interval))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("guild=%s _poll_loop iteration failed", guild_id)


def ensure_poll_loop(guild_id: str) -> None:
    """Guarantee a poll loop exists for this guild WITHOUT touching its backoff.

    For automated events (github webhooks post-debounce, worker connect/disconnect,
    agent/task state updates) that already fired their own foreman run. They must
    not reset the interval to the floor — during active development that pins the
    poll at POLL_MIN_SECS and dominates token spend — but the safety-net loop still
    has to be running. Never interrupts an existing loop's sleep, so the backoff
    keeps growing naturally.
    """
    existing = _poll_tasks.get(guild_id)
    if existing is None or existing.done():
        task = spawn(_poll_loop(guild_id), name=f"foreman.poll-loop:{guild_id}")
        _poll_tasks[guild_id] = task


def reset_foreman_poll(guild_id: str) -> None:
    """Ensure a poll loop is running for this guild, resetting the backoff only when appropriate.

    The backoff is only reset to POLL_MIN_SECS when the foreman was recently
    active (made at least one tool call within the last POLL_MIN_SECS seconds).
    If the foreman is idle, the existing loop is left undisturbed so the interval
    continues to grow — spurious events from worker completions/state changes will
    not spam the Claude API by repeatedly resetting the timer to 60 s.

    If a foreman run is already in-flight for this guild, the call is a no-op:
    the in-flight run was already triggered by _trigger_foreman and the poll loop
    will re-fire on its next tick anyway.
    """
    # Debounce: any in-flight run for this guild (regardless of user_id) means we
    # skip the reset entirely.  The run was already dispatched; another timer reset
    # would be wasteful and could mask the first run's backoff contribution.
    in_flight = any(k[0] == guild_id and state.busy for k, state in _guild_locks.items())
    if in_flight:
        logger.debug("guild=%s reset_foreman_poll: run in-flight, skipping timer reset", guild_id)
        return

    if _guild_active_recently(guild_id):
        # Foreman was active recently — cancel and restart at the minimum interval
        # so the next check happens in POLL_MIN_SECS.
        old = _poll_tasks.pop(guild_id, None)
        if old and not old.done():
            old.cancel()
        task = spawn(_poll_loop(guild_id), name=f"foreman.poll-loop:{guild_id}")
        _poll_tasks[guild_id] = task
    else:
        # Foreman is idle — only start a loop if none exists; do not interrupt
        # the current loop's sleep so the backoff continues to grow naturally.
        ensure_poll_loop(guild_id)


async def _fetch_online_workers(db, guild_id: str) -> list[dict]:
    """Return online workers for the Foreman, with their active agent count and summary.

    Offline workers can't accept task assignments, so they are excluded.
    Each row's ``agents`` field is a comma-separated ``agentId:state`` string
    (empty string when a worker has no non-offline agents).
    """
    guild_pk_val = await get_guild_pk(db, guild_id)
    result = await db.exec(
        select(
            col(Worker.id),
            col(Worker.repos),
            col(Worker.tools),
            col(Worker.org),
            col(Worker.state).label("worker_state"),
            func.count(col(Agent.id)).label("agent_count"),
            func.string_agg(col(Agent.id) + ":" + col(Agent.state), ",").label("agents"),
        )
        .outerjoin(
            Agent, (col(Agent.worker_id) == col(Worker.id)) & (col(Agent.state) != "offline")
        )
        .where(col(Worker.guild_id) == guild_pk_val, col(Worker.state) == "online")
        .group_by(col(Worker.id))
    )
    return [dict(r._mapping) for r in result.all()]


async def _emit_foreman_chat(
    guild_id: str,
    content: str,
    created_at: str,
    *,
    child_task_id: str | None = None,
    discord_task_id: str | None = None,
    discord_channel_id: str | None = None,
    user_id: str | None = None,
    thread_id: str | None = None,
) -> None:
    """Broadcast a Foreman -> user narration line and mirror it into Discord.

    Every plain-text line the Foreman sends to the user (not tool_use/tool_result
    traces) goes through here so the Discord thread mirror in discord_notifier
    stays in sync with the WS chat stream.

    The two ids are deliberately separate (see docs/foreman-per-task-context.md):

    - ``child_task_id`` tags the WS ``ChatMsg`` so the frontend can badge lines
      produced inside a per-task child context. It is None for parent runs even
      when the run concerns a task (e.g. a human chatting in a task's Discord
      thread stays on the parent conversation).
    - ``discord_task_id`` routes the Discord mirror to that task's thread. It is
      set whenever the run concerns a task — including the parent-context reply
      to a message posted in a task thread — so the reply always lands back in
      the thread the human is talking in rather than the guild's main channel.

    ``discord_channel_id`` pins the Discord mirror to one channel outright,
    overriding both of the above. Set only for a run triggered by an @-mention
    (see ``discord/router.py``), so the answer goes back to the channel or DM
    the mention came from.

    ``user_id`` is passed through to ``notify_foreman_chat`` so ad-hoc chat
    (no ``discord_task_id``/``discord_channel_id``) lands in that user's own
    per-conversation Discord thread (#1161) instead of the flat main channel.
    """
    await broadcast_msg(
        guild_id,
        ChatMsg(
            from_="foreman",
            to="user",
            content=content,
            createdAt=created_at,
            taskId=child_task_id,
            threadId=thread_id,
        ),
    )
    spawn(
        discord_notifier.notify_foreman_chat(
            guild_id,
            content,
            task_id=discord_task_id,
            channel_id=discord_channel_id,
            user_id=user_id,
        ),
        name=f"discord.foreman-chat:{guild_id}",
    )


async def run_foreman_ai(
    guild_id: str,
    human_message: str,
    extra_context: str = "",
    user_id: str | None = None,
    task_id: str | None = None,
    *,
    child: bool = False,
    is_human: bool = False,
    reply_channel_id: str | None = None,
    trigger: str | None = None,
) -> None:
    """Serialise per-context and delegate to ``_run_foreman_ai``.

    Uses the ``busy`` flag on the ``_GuildRunLock`` stored in ``_guild_locks``.
    If a run is already in progress, automated invocations (``is_human=False``
    — periodic-check, GitHub webhook events, worker lifecycle, task review
    escalations) are dropped; the poll loop re-triggers on the next tick.
    Dropping is preferred over queuing for these to avoid unbounded build-up
    of stale automated snapshots.

    Human-originated invocations (``is_human=True`` — Discord/web UI chat,
    user-requested follow-ups) are never dropped: when busy, they are appended
    to a small bounded FIFO queue (``_human_queues``) and drained in order once
    the in-flight run for that key finishes, before the slot is freed.

    Whole-guild (parent) runs key the lock on ``(guild_id, user_id)``.  Per-task
    child runs (``child=True`` with a ``task_id``, gated by FOREMAN_CHILD_CONTEXTS)
    key on ``(guild_id, task_id)`` so different tasks run concurrently while a
    single task's review loop still serialises against itself.  See
    docs/foreman-per-task-context.md.

    See ``_GuildRunLock`` for why the busy-check and claim below are atomic.
    """
    use_child = child and bool(task_id) and _child_contexts_enabled()
    # When user_id is None (system-triggered/poll runs), all such invocations
    # within the same guild share key (guild_id, None) and serialize against
    # each other.  This is intentional — system runs are per-guild work and
    # should not overlap with themselves.
    lock_key = (guild_id, f"task:{task_id}") if use_child else (guild_id, user_id)
    state = _guild_locks.setdefault(lock_key, _GuildRunLock())
    if state.busy:
        if is_human:
            _enqueue_human_turn(
                lock_key,
                guild_id,
                human_message,
                extra_context,
                user_id,
                task_id,
                use_child,
                reply_channel_id,
                trigger=trigger,
            )
        else:
            logger.info(
                "guild=%s key=%s run_foreman_ai: dropping concurrent invocation (already running)",
                guild_id,
                lock_key[1],
            )
        return
    state.busy = True
    try:
        await _run_foreman_ai(
            guild_id,
            human_message,
            extra_context,
            user_id,
            task_id=task_id,
            child=use_child,
            reply_channel_id=reply_channel_id,
            trigger=trigger,
        )
        # Drain any human messages that queued up while this turn ran, before
        # going idle — each drained turn can itself queue further messages, so
        # loop until the queue for this key is empty.
        await _drain_human_queue(lock_key)
    finally:
        state.busy = False
        _guild_locks.pop(lock_key, None)


def _enqueue_human_turn(
    lock_key: tuple[str, str | None],
    guild_id: str,
    human_message: str,
    extra_context: str,
    user_id: str | None,
    task_id: str | None,
    child: bool,
    reply_channel_id: str | None = None,
    trigger: str | None = None,
) -> None:
    """Append a human message to the busy queue for *lock_key*, bounded and FIFO.

    When the queue is already at capacity, the oldest queued message is
    dropped (with a warning) to make room — better to lose the stalest queued
    turn than to grow memory without bound or drop the newest arrival.
    """
    queue = _human_queues.setdefault(lock_key, deque())
    if len(queue) >= _HUMAN_QUEUE_MAX:
        dropped = queue.popleft()
        logger.warning(
            "guild=%s key=%s human message queue full (max=%d); dropping oldest "
            "queued message (queued_at=%s)",
            guild_id,
            lock_key[1],
            _HUMAN_QUEUE_MAX,
            dropped.queued_at,
        )
    queue.append(
        _QueuedHumanTurn(
            guild_id=guild_id,
            human_message=human_message,
            extra_context=extra_context,
            user_id=user_id,
            task_id=task_id,
            child=child,
            queued_at=datetime.now(UTC).isoformat(),
            reply_channel_id=reply_channel_id,
            trigger=trigger,
        )
    )
    logger.info(
        "guild=%s key=%s run_foreman_ai: foreman busy, queued human message (queue_len=%d)",
        guild_id,
        lock_key[1],
        len(queue),
    )


async def _drain_human_queue(lock_key: tuple[str, str | None]) -> None:
    """Process every human message queued for *lock_key*, in FIFO order.

    Called while the caller still holds the lock for *lock_key*, so queued
    turns run back-to-back without an automated trigger (periodic-check,
    github-event) sneaking in between them.

    Each pass snapshots the current queue by swapping in a fresh deque before
    processing it, rather than popping from the live deque while iterating.
    Messages enqueued by ``_enqueue_human_turn`` while a snapshot is being
    processed (e.g. because a queued turn's own side effects send another
    human message) land in the new deque and are left for the next pass, so
    the drain never consumes a message that arrived after this pass started.
    """
    while True:
        queue = _human_queues.get(lock_key)
        if not queue:
            return
        pending = queue
        _human_queues[lock_key] = deque()
        while pending:
            turn = pending.popleft()
            logger.info(
                "guild=%s key=%s draining queued human message (queued_at=%s, remaining=%d)",
                turn.guild_id,
                lock_key[1],
                turn.queued_at,
                len(pending),
            )
            annotated_message = (
                f"[queued at {turn.queued_at} while Foreman was busy]\n{turn.human_message}"
            )
            try:
                await _run_foreman_ai(
                    turn.guild_id,
                    annotated_message,
                    turn.extra_context,
                    turn.user_id,
                    task_id=turn.task_id,
                    child=turn.child,
                    reply_channel_id=turn.reply_channel_id,
                    trigger=turn.trigger,
                )
            except Exception as exc:
                logger.exception(
                    "guild=%s key=%s error processing queued human message",
                    turn.guild_id,
                    lock_key[1],
                )
                await _notify_queued_turn_failure(lock_key, turn, exc)
        if not _human_queues.get(lock_key):
            _human_queues.pop(lock_key, None)
            return


async def _notify_queued_turn_failure(
    lock_key: tuple[str, str | None], turn: "_QueuedHumanTurn", exc: Exception
) -> None:
    """Best-effort error reply to the human whose queued message failed to process.

    ``_drain_human_queue`` swallows exceptions from ``_run_foreman_ai`` so one
    bad queued turn doesn't abort the rest of the drain; without this, the
    human whose message failed would see no reply at all. Routed through
    ``_emit_foreman_chat`` so it reaches both the WS chat stream and, when the
    queued turn concerned a task, that task's Discord thread. Failures here
    are only logged — a broken notification path must not crash the drain.
    """
    try:
        await _emit_foreman_chat(
            turn.guild_id,
            f"Sorry, something went wrong processing your message: {exc}",
            datetime.now(UTC).isoformat(),
            child_task_id=turn.task_id if turn.child else None,
            discord_task_id=turn.task_id,
            discord_channel_id=turn.reply_channel_id,
            user_id=turn.user_id,
        )
    except Exception:
        logger.exception(
            "guild=%s key=%s failed to send error feedback for queued human message",
            turn.guild_id,
            lock_key[1],
        )


async def _run_foreman_ai(
    guild_id: str,
    human_message: str,
    extra_context: str = "",
    user_id: str | None = None,
    task_id: str | None = None,
    *,
    child: bool = False,
    reply_channel_id: str | None = None,
    trigger: str | None = None,
):
    """Process a human message (or system escalation) through the Claude foreman AI.

    When ``child`` is True (with a ``task_id``) the run is scoped to a single task:
    worker/task rows, system prompt, state preamble, tool set, and loaded history
    are all narrowed to that one task. See docs/foreman-per-task-context.md.
    """
    # Initialized up front (rather than only inside the child/parent branches
    # below) so it's always bound — including on any early return before task
    # scoping runs — for every ``task_id=_task_id`` use further down.
    _task_id: str | None = None

    proxy_available = has_foreman_proxy(guild_id)
    if not HAS_ANTHROPIC and not proxy_available:
        now = datetime.now(UTC).isoformat()
        await broadcast_msg(
            guild_id,
            ChatMsg(
                from_="foreman",
                to="user",
                content="Foreman AI offline (install `anthropic` package or connect a foreman proxy).",
                createdAt=now,
            ),
        )
        return

    if not user_id:
        user_id = await _get_guild_user_id(guild_id) or guild_id

    # Load per-guild foreman config; fall back to env-var defaults for any unset field.
    guild_cfg = await _load_foreman_config(guild_id)
    cfg_system_prompt_suffix: str | None = guild_cfg.get("system_prompt_suffix")
    cfg_max_rounds: int = int(guild_cfg.get("max_rounds", MAX_FOREMAN_ROUNDS))

    # Build live context for the system prompt.  The session is kept open until
    # the end of the function so the tool_use / tool_result Message rows and the
    # final chat Message can all be written without opening fresh connections.
    db = await get_db()
    try:
        guild_result = await db.exec(
            select(col(Guild.name), col(Guild.primary_repo)).where(col(Guild.slug) == guild_id)
        )
        guild_row = guild_result.one_or_none()
        primary_repo = guild_row.primary_repo if guild_row else None

        worker_rows = await _fetch_online_workers(db, guild_id)
        guild_pk_val = await get_guild_pk(db, guild_id)
        if guild_pk_val is None:
            raise ValueError(f"Guild not found: {guild_id}")
        task_result = await db.exec(
            select(
                col(Task.id),
                col(Task.worker_id),
                col(Task.name),
                col(Task.description),
                col(Task.state),
                col(Task.phase),
                col(Task.issue_repo),
                col(Task.branch),
                col(Task.pr_url),
                col(Task.deleted_at),
            )
            .where(
                col(Task.guild_id) == guild_pk_val,
                live_tasks_filter(),
            )
            .order_by(col(Task.created_at).desc())
        )
        task_rows = [
            {**dict(r._mapping), "description": dict(r._mapping).get("description") or ""}
            for r in task_result.all()
        ]
        # Per-task child context: narrow worker/task rows to just this task and
        # its assigned worker. Falls back to a minimal stub if the task is no
        # longer in the active set (e.g. just finalized).
        child_task_row: dict | None = None
        if child and task_id:
            child_task_row = next((t for t in task_rows if t.get("id") == task_id), None)
            child_worker_id = child_task_row.get("worker_id") if child_task_row else None
            worker_rows = [r for r in worker_rows if r["id"] == child_worker_id]
            task_rows = [child_task_row] if child_task_row else []
            _task_id = task_id
        elif child:
            _task_id = task_rows[0]["id"] if len(task_rows) == 1 else None
        else:
            # Parent runs (periodic-check, worker lifecycle, human chat) must
            # never tag turns with a child task_id, even when exactly one
            # non-terminal task happens to exist — those turns must stay
            # untagged (task_id IS NULL) so they don't pollute that task's
            # isolated child context on its next run.
            _task_id = None

        # Discord routing target for narration mirrored back to the human.
        # ``_task_id`` (history tag) is None for parent runs, but a parent run
        # can still concern a task — e.g. a human message posted in that task's
        # Discord thread arrives as an ``event="chat"`` trigger with a task_id
        # that stays on the parent conversation. Route the reply back into that
        # thread using the incoming ``task_id`` even though the turn isn't
        # tagged as child history. See docs/foreman-per-task-context.md.
        _discord_task_id: str | None = _task_id or task_id

        # Foreman-owned conversation thread (#1167) this run's messages belong
        # to — resolved once per turn and reused for every Message row/ChatMsg
        # broadcast this run makes. Prefers the task's thread (via
        # ``_discord_task_id``, same task used for Discord routing above), else
        # falls back to the user's current active thread. Read-only: never
        # creates a thread — see ``resolve_thread_id``.
        _thread_id: str | None = await resolve_thread_id(
            db, guild_pk_val, task_id=_discord_task_id, user_id=user_id
        )
    except Exception:
        await db.close()
        raise

    try:
        workers_block = json.dumps(
            [
                {
                    "id": r["id"],
                    "state": r["worker_state"] or "idle",
                    "repos": json.loads(r["repos"] or "[]"),
                    **({"org": r["org"]} if r.get("org") else {}),
                    "agent_count": r["agent_count"] or 0,
                    "tools": json.loads(r["tools"] or "[]"),
                }
                for r in worker_rows
            ],
            indent=2,
            default=_json_default,
        )
        cutoff_ts = datetime.now(UTC).timestamp() - _24H_SECS
        summarized_tasks = [
            s for row in task_rows if (s := _summarize_task(row, cutoff_ts)) is not None
        ]
        tasks_block = json.dumps(summarized_tasks, indent=2, default=_json_default)
        if child and task_id:
            _t = child_task_row or {}
            tools = CHILD_FOREMAN_TOOLS
            system_blocks = build_child_system_blocks(
                task_id=task_id,
                task_name=_t.get("name") or task_id,
                worker_id=_t.get("worker_id"),
                phase=_t.get("phase"),
                repo=_t.get("issue_repo") or primary_repo,
                system_prompt_suffix=cfg_system_prompt_suffix,
            )
            state_preamble = build_child_state_preamble(workers_block, tasks_block, extra_context)
        else:
            tools = FOREMAN_TOOLS
            system_blocks = build_system_blocks(
                primary_repo=primary_repo, system_prompt_suffix=cfg_system_prompt_suffix
            )
            state_preamble = build_state_preamble(workers_block, tasks_block, extra_context)
        # Legacy single-string render — persisted for audit only, not sent to the API.
        audit_system = build_system_prompt(
            workers_block,
            tasks_block,
            extra_context,
            primary_repo=primary_repo,
            system_prompt_suffix=cfg_system_prompt_suffix,
        )

        logger.info(
            "guild=%s run_foreman_ai: workers=%d tasks_in_context=%d "
            "system_chars=%d state_chars=%d extra_context_chars=%d",
            guild_id,
            len(worker_rows),
            len(summarized_tasks),
            len(system_blocks[0]["text"]),
            len(state_preamble),
            len(extra_context),
        )
        logger.debug("guild=%s workers_block: %s", guild_id, workers_block)
        # logger.debug("guild=%s tasks_block: %s", guild_id, tasks_block)

        # Annotate any GitHub URLs in the human message so the Foreman can
        # reference owner/repo and issue/PR numbers directly.
        human_message = _annotate_github_urls(human_message)

        # Persist the rendered prompt + human turn for auditing; the API receives
        # `system_blocks` (cacheable) and the state preamble injected at send time.
        await _save_turn(guild_id, user_id, "system", audit_system, task_id=_task_id)
        await _save_turn(guild_id, user_id, "user", human_message, task_id=_task_id)
        messages = await _load_history(guild_id, user_id, task_id=_task_id if child else None)

        logger.info(
            "guild=%s run_foreman_ai: %d messages loaded from history; human_message_chars=%d",
            guild_id,
            len(messages),
            len(human_message),
        )

        # Inject live state into the just-loaded current human turn so it travels
        # with this call only — the DB still holds just the human's literal text.
        _inject_state_preamble(messages, state_preamble)

        # Resolve the client/provider/model before building the client (and
        # before any task is dispatched) so a stale guild config — saved before
        # the #817 fix added save-time validation, with no model or a
        # placeholder ARN — fails here with a clear error instead of surfacing
        # deep inside the API call.
        client, effective_provider, foreman_model = await resolve_foreman_client(
            guild_id, guild_cfg
        )

        text_parts = []
        for round_num in range(cfg_max_rounds):
            messages = prune_history(messages)
            messages = strip_orphaned_tool_results(messages)
            _stamp_message_cache_breakpoint(messages)
            logger.info(
                "guild=%s run_foreman_ai round %d: sending %d messages to LLM",
                guild_id,
                round_num,
                len(messages),
            )
            api_log_id = await _create_api_request_log(
                model=foreman_model,
                system_blocks=system_blocks,
                messages=messages,
                extra={"max_tokens": 1024, "tools": tools},
                task_id=_task_id,
                guild_id=guild_id,
                user_id=user_id,
                trigger=trigger,
            )
            llm_result = await _call_llm(
                guild_id,
                client=client,
                provider=effective_provider,
                model=foreman_model,
                max_tokens=1024,
                system_blocks=system_blocks,
                messages=messages,
                tools=tools,
            )
            resp = llm_result.response
            usage = resp.usage
            _input_tokens = getattr(usage, "input_tokens", 0) or 0
            _output_tokens = getattr(usage, "output_tokens", 0) or 0
            _cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            _cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
            _api_request_id = llm_result.request_id
            logger.info(
                "guild=%s run_foreman_ai round %d: stop_reason=%s content_blocks=%d "
                "input=%d cache_read=%d cache_write=%d output=%d request_id=%s provider=%s",
                guild_id,
                round_num,
                resp.stop_reason,
                len(resp.content),
                _input_tokens,
                _cache_read,
                _cache_write,
                _output_tokens,
                _api_request_id,
                llm_result.provider,
            )
            await _complete_api_request_log(
                api_log_id,
                request_id=_api_request_id,
                response_dict=llm_result.response_dict,
                input_tokens=_input_tokens,
                output_tokens=_output_tokens,
                cache_read_tokens=_cache_read,
                cache_write_tokens=_cache_write,
                stop_reason=resp.stop_reason,
            )

            # Persist assistant turn and append to local messages
            asst_turn_id = await _save_turn(
                guild_id,
                user_id,
                "assistant",
                resp.content,
                api_log_id=api_log_id,
                task_id=_task_id,
            )
            messages.append({"role": "assistant", "content": _serialize_content(resp.content)})
            # Re-parse so messages stays as plain dicts (not SDK objects)
            messages[-1]["content"] = json.loads(messages[-1]["content"])

            # Emit text blocks immediately so narration appears inline with tool calls,
            # not batched at the end of the turn.
            _now = datetime.now(UTC)
            for b in resp.content:
                if b.type == "text" and b.text.strip():
                    text_parts.append(b.text.strip())
                    await _emit_foreman_chat(
                        guild_id,
                        b.text.strip(),
                        _now.isoformat(),
                        child_task_id=_task_id,
                        discord_task_id=_discord_task_id,
                        discord_channel_id=reply_channel_id,
                        user_id=user_id,
                    )

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break  # end_turn — foreman is done

            _record_guild_action(guild_id)

            # Broadcast tool-use events so the frontend chat shows them live
            for tu in tool_uses:
                await broadcast_msg(
                    guild_id,
                    ChatMsg(
                        from_="foreman",
                        role="tool_use",
                        to="user",
                        content=f"▶ {tu.name}",
                        toolName=tu.name,
                        toolInput=dict(tu.input) if tu.input else {},
                        toolId=tu.id,
                        createdAt=_now.isoformat(),
                        taskId=_task_id,
                    ),
                )

            _tool_use_ts = _now  # capture before exec_tools may raise

            tool_results = await exec_tools(
                guild_id, tool_uses, user_id=user_id, own_task_id=_task_id
            )
            # Truncate verbose results; filter to only IDs in the current batch so
            # stale results that survived history trimming are never persisted.
            current_tool_use_ids = {tu.id for tu in tool_uses}
            trimmed: list[dict] = []
            for r in tool_results:
                if r.get("tool_use_id") not in current_tool_use_ids:
                    continue
                entry = {k: v for k, v in r.items() if k != "api_calls"}
                if entry.get("content"):
                    entry = {**entry, "content": truncate_tool_result(entry["content"])}
                trimmed.append(entry)

            # Broadcast tool-result events
            _now = datetime.now(UTC)
            for result in trimmed:
                await broadcast_msg(
                    guild_id,
                    ChatMsg(
                        from_="foreman",
                        role="tool_result",
                        to="user",
                        content=result.get("content", ""),
                        toolId=result.get("tool_use_id"),
                        toolOutput=result.get("content", ""),
                        isError=result.get("is_error", False),
                        createdAt=_now.isoformat(),
                        taskId=_task_id,
                    ),
                )

            # Persist tool_use and tool_result together in one transaction
            # so exec_tools raising never leaves tool_use rows without their results.
            try:
                for tu in tool_uses:
                    db.add(
                        Message(
                            guild_id=guild_pk_val,
                            from_agent="foreman",
                            to_agent="user",
                            content=f"▶ {tu.name}",
                            message_type="chat",
                            role="tool_use",
                            meta=json.dumps(
                                {
                                    "toolId": tu.id,
                                    "toolName": tu.name,
                                    "toolInput": dict(tu.input) if tu.input else {},
                                }
                            ),
                            created_at=_tool_use_ts,
                            task_id=_task_id,
                        )
                    )
                for result in trimmed:
                    db.add(
                        Message(
                            guild_id=guild_pk_val,
                            from_agent="foreman",
                            to_agent="user",
                            content=result.get("content", "") or "",
                            message_type="chat",
                            role="tool_result",
                            meta=json.dumps(
                                {
                                    "toolId": result.get("tool_use_id"),
                                    "isError": result.get("is_error", False),
                                }
                            ),
                            created_at=_now,
                            task_id=_task_id,
                        )
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

            # Persist tool_result turn as a child of the assistant turn
            await _save_turn(
                guild_id,
                user_id,
                "user",
                trimmed,
                is_tool_response=True,
                parent_id=asst_turn_id,
                task_id=_task_id,
            )
            logger.info(
                "guild=%s round %d: %d tool call(s) dispatched: %s",
                guild_id,
                round_num,
                len(trimmed),
                [
                    {"tool_use_id": r["tool_use_id"], "is_error": r.get("is_error", False)}
                    for r in trimmed
                ],
            )
            messages.append({"role": "user", "content": trimmed})
        else:
            # Loop exhausted: round MAX_FOREMAN_ROUNDS-1 returned tool_uses and
            # we just executed them, but have no rounds left to send results
            # back. Force a final tool-free wrap-up so the conversation ends
            # cleanly (no orphaned tool_use, no consecutive user turns) and
            # the human gets a summary of what the foreman accomplished.
            logger.warning(
                "guild=%s run_foreman_ai: hit %d-round safety cap, forcing wrap-up",
                guild_id,
                cfg_max_rounds,
            )
            messages = prune_history(messages)
            messages = strip_orphaned_tool_results(messages)
            _stamp_message_cache_breakpoint(messages)
            _wrap_api_log_id = await _create_api_request_log(
                model=foreman_model,
                system_blocks=system_blocks,
                messages=messages,
                extra={
                    "max_tokens": 1024,
                    "tools": tools,
                    "tool_choice": {"type": "none"},
                },
                task_id=_task_id,
                guild_id=guild_id,
                user_id=user_id,
                trigger=trigger,
            )
            wrap_llm_result = await _call_llm(
                guild_id,
                client=client,
                provider=effective_provider,
                model=foreman_model,
                max_tokens=1024,
                system_blocks=system_blocks,
                messages=messages,
                tools=tools,
                tool_choice={"type": "none"},
            )
            wrap_resp = wrap_llm_result.response
            wrap_usage = wrap_resp.usage
            _wrap_input = getattr(wrap_usage, "input_tokens", 0) or 0
            _wrap_output = getattr(wrap_usage, "output_tokens", 0) or 0
            _wrap_cache_read = getattr(wrap_usage, "cache_read_input_tokens", 0) or 0
            _wrap_cache_write = getattr(wrap_usage, "cache_creation_input_tokens", 0) or 0
            _wrap_request_id = wrap_llm_result.request_id
            logger.info(
                "guild=%s run_foreman_ai wrap-up: stop_reason=%s "
                "input=%d cache_read=%d cache_write=%d output=%d request_id=%s provider=%s",
                guild_id,
                wrap_resp.stop_reason,
                _wrap_input,
                _wrap_cache_read,
                _wrap_cache_write,
                _wrap_output,
                _wrap_request_id,
                wrap_llm_result.provider,
            )
            await _complete_api_request_log(
                _wrap_api_log_id,
                request_id=_wrap_request_id,
                response_dict=wrap_llm_result.response_dict,
                input_tokens=_wrap_input,
                output_tokens=_wrap_output,
                cache_read_tokens=_wrap_cache_read,
                cache_write_tokens=_wrap_cache_write,
                stop_reason=wrap_resp.stop_reason,
            )
            await _save_turn(
                guild_id,
                user_id,
                "assistant",
                wrap_resp.content,
                api_log_id=_wrap_api_log_id,
                task_id=_task_id,
            )
            _now = datetime.now(UTC).isoformat()
            for b in wrap_resp.content:
                if b.type == "text" and b.text.strip():
                    text_parts.append(b.text.strip())
                    await _emit_foreman_chat(
                        guild_id,
                        b.text.strip(),
                        _now,
                        child_task_id=_task_id,
                        discord_task_id=_discord_task_id,
                        discord_channel_id=reply_channel_id,
                        user_id=user_id,
                    )
            cap_note = f"_(Foreman hit {cfg_max_rounds}-round safety cap and stopped.)_"
            text_parts.append(cap_note)
            await _emit_foreman_chat(
                guild_id,
                cap_note,
                _now,
                child_task_id=_task_id,
                discord_task_id=_discord_task_id,
                discord_channel_id=reply_channel_id,
                user_id=user_id,
            )

        response_text = "\n".join(text_parts).strip()
        if response_text:
            now = datetime.now(UTC)
            db.add(
                Message(
                    guild_id=guild_pk_val,
                    from_agent="foreman",
                    to_agent="user",
                    content=response_text,
                    message_type="chat",
                    created_at=now,
                    task_id=_task_id,
                    user_id=user_id,
                    source="a2a" if user_id and "." in user_id else "web",
                )
            )
            await db.commit()

    except Exception as exc:
        now = datetime.now(UTC).isoformat()
        await broadcast_msg(
            guild_id,
            ChatMsg(from_="foreman", to="user", content=f"Foreman error: {exc}", createdAt=now),
        )
    finally:
        await db.close()


async def clear_foreman_history(guild_id: str, user_id: str) -> int:
    """Delete all stored turns for this guild+user. Returns count removed."""
    db = await get_db()
    try:
        guild_pk_val = await get_guild_pk(db, guild_id)
        result = await db.exec(
            delete(ForemanTurn).where(
                col(ForemanTurn.guild_id) == guild_pk_val, col(ForemanTurn.user_id) == user_id
            )
        )
        await db.commit()
        return getattr(result, "rowcount", 0) or 0
    finally:
        await db.close()


async def get_foreman_history(guild_id: str, user_id: str) -> dict:
    """Return stored turns structured for the debug view.

    Applies the same windowing pipeline used before each real API call so the
    debug pane shows exactly the messages that would be submitted next:
      1. ``_HUMAN_TURN_WINDOW`` sliding-window cutoff (same as ``_load_history``)
      2. ``prune_history`` cap (``MAX_HISTORY_MESSAGES``)
      3. ``strip_orphaned_tool_results`` cleanup

    Returns::

        {
            "system": <str | None>,   # most-recent audit system prompt text
            "messages": [...],        # windowed messages (what would be sent next)
            "total": <int>,           # total non-system turns stored (before windowing)
        }
    """
    db = await get_db()
    try:
        guild_pk_val = await get_guild_pk(db, guild_id)
        result = await db.exec(
            select(ForemanTurn)
            .where(col(ForemanTurn.guild_id) == guild_pk_val, col(ForemanTurn.user_id) == user_id)
            .order_by(col(ForemanTurn.id))
        )
        turns = result.all()
    finally:
        await db.close()

    # Grab the most-recent system turn (there's one per invocation; we only
    # show the latest to avoid duplicates in the debug pane).
    system_content: str | None = None
    for t in reversed(turns):
        if t.role == "system":
            raw = json.loads(t.content_json)
            system_content = raw if isinstance(raw, str) else json.dumps(raw)
            break

    # Count total non-system turns before any windowing for the summary line.
    total = sum(1 for t in turns if t.role != "system")

    if not turns:
        return {"system": system_content, "messages": [], "total": 0}

    # Apply the same human-turn-window cutoff as _load_history().
    cutoff = 0
    human_count = 0
    for i in range(len(turns) - 1, -1, -1):
        t = turns[i]
        if t.role == "user" and not t.is_tool_response:
            human_count += 1
            if human_count >= _HUMAN_TURN_WINDOW:
                cutoff = i
                break

    # Build metadata-rich messages from the windowed slice (system turns excluded).
    messages: list[dict] = [
        {
            "id": t.id,
            "role": t.role,
            "is_tool_response": bool(t.is_tool_response),
            "parent_id": t.parent_id,
            "content": json.loads(t.content_json),
            "created_at": t.created_at,
        }
        for t in turns[cutoff:]
        if t.role != "system"
    ]

    # Ensure the list starts with a user turn (mirrors _load_history).
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    # Apply the same post-load trimming used before every real API call.
    messages = prune_history(messages)
    messages = strip_orphaned_tool_results(messages)

    return {"system": system_content, "messages": messages, "total": total}
