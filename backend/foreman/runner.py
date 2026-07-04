"""Foreman AI runner: conversation management and main loop (embedded)."""

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime

import discord_notifier
from auth_deps import get_guild_pk
from database import AsyncSessionLocal, get_db
from events import broadcast_msg
from foreman.prompt import (
    build_child_state_preamble,
    build_child_system_blocks,
    build_state_preamble,
    build_system_blocks,
    build_system_prompt,
)
from foreman.tools import exec_tools
from foreman_core.constants import (
    _24H_SECS,
    _HUMAN_TURN_WINDOW,
    _TERMINAL_STATES,
    MAX_FOREMAN_ROUNDS,
)
from foreman_core.llm import HAS_ANTHROPIC, get_foreman_model, make_anthropic_client
from foreman_core.message_utils import (
    _inject_state_preamble,
    _json_default,
    _serialize_content,
    _stamp_message_cache_breakpoint,
    _summarize_task,
    prune_history,
    strip_orphaned_tool_results,
    truncate_tool_result,
)
from foreman_core.tools_schema import CHILD_FOREMAN_TOOLS, FOREMAN_TOOLS
from models import (
    Agent,
    ApiRequestLog,
    ForemanTurn,
    Guild,
    GuildMember,
    Message,
    Task,
    Worker,
    live_tasks_filter,
)
from sqlalchemy import delete, func
from sqlmodel import col, select
from util.tasks import spawn
from ws_types import ChatMsg, ForemanPollStatusMsg

logger = logging.getLogger(__name__)

POLL_MIN_SECS = 60  # initial poll interval: 1 minute
POLL_MAX_SECS = 14400  # maximum poll interval: 4 hours

# Upper bound on rows fetched by _load_history before Python-side windowing,
# so query cost stays flat regardless of the table's total lifetime turn count.
_HISTORY_FETCH_LIMIT = 100

# Per-guild background poll task registry
_poll_tasks: dict[str, "asyncio.Task[None]"] = {}
# Guilds whose embedded poll loop is suppressed while an external foreman owns polling.
_suppressed_poll_guilds: set[str] = set()

# Per-guild locks to serialise foreman runs.  If a run is already in progress
# for a (guild, user) pair, new invocations are dropped rather than queued —
# the poll loop will re-trigger on the next tick.  Dropping (vs. queuing) keeps
# memory bounded and avoids stale snapshots piling up under load.
# Entries are popped in the finally block after each run so the dict stays small.
_guild_locks: dict[tuple[str, str | None], asyncio.Lock] = {}

# Monotonic timestamp (time.monotonic()) of the last foreman run that made at
# least one tool call, keyed by guild_id.  Used by reset_foreman_poll to decide
# whether to reset the backoff: only reset when the foreman was recently active.
_guild_last_action_at: dict[str, float] = {}


def _record_guild_action(guild_id: str) -> None:
    """Record that the foreman made tool calls for *guild_id* right now."""
    _guild_last_action_at[guild_id] = time.monotonic()


def _guild_active_recently(guild_id: str) -> bool:
    """Return True if the foreman made tool calls within the last POLL_MIN_SECS."""
    return (time.monotonic() - _guild_last_action_at.get(guild_id, 0.0)) <= POLL_MIN_SECS


# Module-level client — reused across calls so the underlying httpx connection
# pool isn't thrown away every invocation. Lazily initialised so import works
# without an API key (Anthropic) or AWS credentials (Bedrock).
_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = make_anthropic_client()
    return _anthropic_client


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

    Mirrors the standalone foreman's ``child_contexts`` config flag. Defaults on;
    set ``FOREMAN_CHILD_CONTEXTS`` to a falsy value (0/false/no/off) to fall back
    to the legacy single whole-guild context. See docs/foreman-per-task-context.md.
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
        result = await db.exec(stmt.order_by(col(ForemanTurn.id).desc()).limit(_HISTORY_FETCH_LIMIT))
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
    api_calls: list | None = None,
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
            api_calls_json=json.dumps(api_calls) if api_calls else None,
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


async def _poll_loop(guild_id: str) -> None:
    """Background loop: check non-terminal tasks and call the foreman if any are found.

    Interval doubles each cycle from POLL_MIN_SECS up to POLL_MAX_SECS (or per-guild
    overrides). Cancelled (via reset_foreman_poll) whenever a significant event arrives.
    The foreman-poll-status broadcast is sent after each poll so the UI can
    display the countdown to the *next* check.
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

            # If an external foreman is connected for this guild it owns the
            # poll loop — skip the embedded run to avoid double-triggering.
            from events import foreman_connections

            if guild_id in foreman_connections:
                next_interval = min(interval * 2, poll_max)
                logger.debug(
                    "guild=%s external foreman connected, skipping embedded poll", guild_id
                )
                interval = next_interval
                await broadcast_msg(guild_id, ForemanPollStatusMsg(nextCheckIn=interval))
                continue

            db = await get_db()
            try:
                guild_pk_val = await get_guild_pk(db, guild_id)
                result = await db.exec(
                    select(col(Task.id), col(Task.state), col(Task.name)).where(
                        col(Task.guild_id) == guild_pk_val,
                        ~col(Task.state).in_(list(_TERMINAL_STATES)),
                        live_tasks_filter(),
                    )
                )
                active_tasks = [dict(r._mapping) for r in result.all()]

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

            if active_tasks:
                task_summary = "; ".join(f"{t['id']} ({t['state']})" for t in active_tasks)
                msg = (
                    f"[periodic-check] Automated status poll — {n} non-terminal "
                    f"task(s): {task_summary}. Check whether any are stalled. "
                    "Use get_task_status to inspect a task if it looks stuck. "
                    "If everything looks healthy, no action is needed."
                )
                spawn(run_foreman_ai(guild_id, msg), name=f"foreman.poll:{guild_id}")

            # Announce next check interval so the UI can display a countdown.
            interval = next_interval
            await broadcast_msg(guild_id, ForemanPollStatusMsg(nextCheckIn=interval))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("guild=%s _poll_loop iteration failed", guild_id)


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
    in_flight = any(k[0] == guild_id and lock.locked() for k, lock in _guild_locks.items())
    if in_flight:
        logger.debug("guild=%s reset_foreman_poll: run in-flight, skipping timer reset", guild_id)
        return

    if guild_id in _suppressed_poll_guilds:
        logger.debug("guild=%s reset_foreman_poll: suppressed (external foreman active)", guild_id)
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
        existing = _poll_tasks.get(guild_id)
        if existing is None or existing.done():
            task = spawn(_poll_loop(guild_id), name=f"foreman.poll-loop:{guild_id}")
            _poll_tasks[guild_id] = task


def suppress_foreman_poll(guild_id: str) -> None:
    """Pause the embedded poll loop for *guild_id* while an external foreman is active."""
    _suppressed_poll_guilds.add(guild_id)
    old = _poll_tasks.pop(guild_id, None)
    if old and not old.done():
        old.cancel()


def resume_foreman_poll(guild_id: str) -> None:
    """Resume the embedded poll loop for *guild_id* after external foreman disconnect."""
    _suppressed_poll_guilds.discard(guild_id)
    reset_foreman_poll(guild_id)


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


async def _emit_foreman_chat(guild_id: str, content: str, created_at: str) -> None:
    """Broadcast a Foreman -> user narration line and mirror it into Discord.

    Every plain-text line the Foreman sends to the user (not tool_use/tool_result
    traces) goes through here so the Discord thread mirror in discord_notifier
    stays in sync with the WS chat stream.
    """
    await broadcast_msg(
        guild_id,
        ChatMsg(from_="foreman", to="user", content=content, createdAt=created_at),
    )
    spawn(
        discord_notifier.notify_foreman_chat(guild_id, content),
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
) -> None:
    """Serialise per-context and delegate to ``_run_foreman_ai``.

    Uses an ``asyncio.Lock`` stored in ``_guild_locks``.  If the lock is already
    held the invocation is dropped; the poll loop re-triggers on the next tick.
    Dropping is preferred over queuing to avoid unbounded build-up.

    Whole-guild (parent) runs key the lock on ``(guild_id, user_id)``.  Per-task
    child runs (``child=True`` with a ``task_id``, gated by FOREMAN_CHILD_CONTEXTS)
    key on ``(guild_id, task_id)`` so different tasks run concurrently while a
    single task's review loop still serialises against itself.  See
    docs/foreman-per-task-context.md.

    lock.locked() + lock.acquire() is atomic here: asyncio is single-threaded
    and cooperative, so no other coroutine can run between the check and the
    acquire (there is no ``await`` between them).
    """
    use_child = child and bool(task_id) and _child_contexts_enabled()
    # When user_id is None (system-triggered/poll runs), all such invocations
    # within the same guild share key (guild_id, None) and serialize against
    # each other.  This is intentional — system runs are per-guild work and
    # should not overlap with themselves.
    lock_key = (guild_id, f"task:{task_id}") if use_child else (guild_id, user_id)
    lock = _guild_locks.setdefault(lock_key, asyncio.Lock())
    if lock.locked():
        logger.info(
            "guild=%s key=%s run_foreman_ai: dropping concurrent invocation (already running)",
            guild_id,
            lock_key[1],
        )
        return
    await lock.acquire()
    try:
        await _run_foreman_ai(
            guild_id, human_message, extra_context, user_id, task_id=task_id, child=use_child
        )
    finally:
        lock.release()
        _guild_locks.pop(lock_key, None)


async def _run_foreman_ai(
    guild_id: str,
    human_message: str,
    extra_context: str = "",
    user_id: str | None = None,
    task_id: str | None = None,
    *,
    child: bool = False,
):
    """Process a human message (or system escalation) through the Claude foreman AI.

    When ``child`` is True (with a ``task_id``) the run is scoped to a single task:
    worker/task rows, system prompt, state preamble, tool set, and loaded history
    are all narrowed to that one task. See docs/foreman-per-task-context.md.
    """
    if not HAS_ANTHROPIC:
        now = datetime.now(UTC).isoformat()
        await broadcast_msg(
            guild_id,
            ChatMsg(
                from_="foreman",
                to="user",
                content="Foreman AI offline (install `anthropic` package to enable).",
                createdAt=now,
            ),
        )
        return

    if not user_id:
        user_id = await _get_guild_user_id(guild_id) or guild_id

    # Load per-guild foreman config; fall back to env-var defaults for any unset field.
    guild_cfg = await _load_foreman_config(guild_id)
    cfg_provider: str | None = guild_cfg.get("provider")
    cfg_model: str | None = guild_cfg.get("model")
    cfg_system_prompt_suffix: str | None = guild_cfg.get("system_prompt_suffix")
    cfg_max_rounds: int = int(guild_cfg.get("max_rounds", MAX_FOREMAN_ROUNDS))
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
            _task_id: str | None = task_id
        elif child:
            _task_id = task_rows[0]["id"] if len(task_rows) == 1 else None
        else:
            # Parent runs (periodic-check, worker lifecycle, human chat) must
            # never tag turns with a child task_id, even when exactly one
            # non-terminal task happens to exist — those turns must stay
            # untagged (task_id IS NULL) so they don't pollute that task's
            # isolated child context on its next run.
            _task_id = None
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
        logger.debug("guild=%s tasks_block: %s", guild_id, tasks_block)

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

        # Use a per-guild client when the config overrides the provider or
        # supplies its own env vars (e.g. Bedrock AWS credentials/region from
        # the settings dialogue, which otherwise never reach this process).
        effective_provider = cfg_provider or os.environ.get("FOREMAN_PROVIDER", "anthropic").lower()
        if cfg_provider or cfg_env_vars:
            client = make_anthropic_client(
                provider=effective_provider,
                extra_env=cfg_env_vars or None,
            )
        else:
            client = _get_anthropic_client()
        foreman_model = cfg_model or get_foreman_model(provider=effective_provider)

        text_parts = []
        for round_num in range(cfg_max_rounds):
            messages = prune_history(messages)
            messages = strip_orphaned_tool_results(messages)
            _stamp_message_cache_breakpoint(messages)
            logger.info(
                "guild=%s run_foreman_ai round %d: sending %d messages to Claude",
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
            )
            _raw = await client.messages.with_raw_response.create(
                model=foreman_model,
                max_tokens=1024,
                system=system_blocks,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
                tools=tools,  # type: ignore[arg-type]
            )
            resp = _raw.parse()
            usage = resp.usage
            _input_tokens = getattr(usage, "input_tokens", 0) or 0
            _output_tokens = getattr(usage, "output_tokens", 0) or 0
            _cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            _cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
            _anthropic_request_id = _raw.headers.get("request-id")
            logger.info(
                "guild=%s run_foreman_ai round %d: stop_reason=%s content_blocks=%d "
                "input=%d cache_read=%d cache_write=%d output=%d request_id=%s",
                guild_id,
                round_num,
                resp.stop_reason,
                len(resp.content),
                _input_tokens,
                _cache_read,
                _cache_write,
                _output_tokens,
                _anthropic_request_id,
            )
            await _complete_api_request_log(
                api_log_id,
                request_id=_anthropic_request_id,
                response_dict=resp.model_dump(),
                input_tokens=_input_tokens,
                output_tokens=_output_tokens,
                cache_read_tokens=_cache_read,
                cache_write_tokens=_cache_write,
                stop_reason=resp.stop_reason,
            )

            _api_call_meta = {
                "request_id": _anthropic_request_id,
                "model": foreman_model,
                "input_tokens": _input_tokens,
                "output_tokens": _output_tokens,
                "cache_read_tokens": _cache_read,
                "cache_write_tokens": _cache_write,
                "ts": datetime.now(UTC).isoformat(),
            }

            # Persist assistant turn and append to local messages
            asst_turn_id = await _save_turn(
                guild_id,
                user_id,
                "assistant",
                resp.content,
                api_calls=[_api_call_meta],
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
                    await _emit_foreman_chat(guild_id, b.text.strip(), _now.isoformat())

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
                    ),
                )

            _tool_use_ts = _now  # capture before exec_tools may raise

            tool_results = await exec_tools(guild_id, tool_uses, user_id=user_id)
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
            )
            _wrap_raw = await client.messages.with_raw_response.create(
                model=foreman_model,
                max_tokens=1024,
                system=system_blocks,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
                tools=tools,  # type: ignore[arg-type]
                tool_choice={"type": "none"},  # type: ignore[arg-type]
            )
            wrap_resp = _wrap_raw.parse()
            wrap_usage = wrap_resp.usage
            _wrap_input = getattr(wrap_usage, "input_tokens", 0) or 0
            _wrap_output = getattr(wrap_usage, "output_tokens", 0) or 0
            _wrap_cache_read = getattr(wrap_usage, "cache_read_input_tokens", 0) or 0
            _wrap_cache_write = getattr(wrap_usage, "cache_creation_input_tokens", 0) or 0
            _wrap_request_id = _wrap_raw.headers.get("request-id")
            logger.info(
                "guild=%s run_foreman_ai wrap-up: stop_reason=%s "
                "input=%d cache_read=%d cache_write=%d output=%d request_id=%s",
                guild_id,
                wrap_resp.stop_reason,
                _wrap_input,
                _wrap_cache_read,
                _wrap_cache_write,
                _wrap_output,
                _wrap_request_id,
            )
            await _complete_api_request_log(
                _wrap_api_log_id,
                request_id=_wrap_request_id,
                response_dict=wrap_resp.model_dump(),
                input_tokens=_wrap_input,
                output_tokens=_wrap_output,
                cache_read_tokens=_wrap_cache_read,
                cache_write_tokens=_wrap_cache_write,
                stop_reason=wrap_resp.stop_reason,
            )
            _wrap_api_meta = {
                "request_id": _wrap_request_id,
                "model": foreman_model,
                "input_tokens": _wrap_input,
                "output_tokens": _wrap_output,
                "cache_read_tokens": _wrap_cache_read,
                "cache_write_tokens": _wrap_cache_write,
                "ts": datetime.now(UTC).isoformat(),
            }
            await _save_turn(
                guild_id,
                user_id,
                "assistant",
                wrap_resp.content,
                api_calls=[_wrap_api_meta],
                api_log_id=_wrap_api_log_id,
                task_id=_task_id,
            )
            _now = datetime.now(UTC).isoformat()
            for b in wrap_resp.content:
                if b.type == "text" and b.text.strip():
                    text_parts.append(b.text.strip())
                    await _emit_foreman_chat(guild_id, b.text.strip(), _now)
            cap_note = f"_(Foreman hit {cfg_max_rounds}-round safety cap and stopped.)_"
            text_parts.append(cap_note)
            await _emit_foreman_chat(guild_id, cap_note, _now)

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
            "input_tokens": t.input_tokens,
            "output_tokens": t.output_tokens,
            "api_calls": json.loads(t.api_calls_json) if t.api_calls_json else None,
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
