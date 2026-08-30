"""Foreman trigger dispatch and message-formatting policy.

Centralizes what used to be ``ws_handlers._trigger_foreman`` — the single
entry point every worker/websocket/Discord/A2A event goes through to hand a
message to the embedded Foreman runner — plus the per-event message builders
that were duplicated as inline f-string blocks across ``ws_handlers.py``,
``discord/router.py``, ``routes/a2a.py``, ``routes/websocket.py``, and
``routes/tasks.py``. Those blocks hard-coded Foreman tool names
(``send_followup``, ``finalize_task``, ...) and policy ("DO NOT call
finalize_task now", ...); keeping them here means that policy only needs to
change in one place.

``event`` mirrors the trigger-type vocabulary: ``chat``, ``task-complete``,
``followup-done``, ``needs-input``, ``task-rejected``, ``task-error``,
``worker-online``, ``worker-offline``, ``periodic-check``, ``user-followup``.
See ``foreman.classify`` for the human/automated split.

Note: ``foreman.runner`` cannot import this module at load time —
``trigger_foreman`` needs ``run_foreman_ai`` from ``foreman.runner``, so the
dependency only runs one way (this module depends on ``foreman.runner``, never
the reverse). ``foreman/runner.py``'s own periodic-check trigger (inside
``_poll_loop``) calls ``run_foreman_ai`` directly instead of going through
``trigger_foreman`` for this reason — periodic-check is never a
human-originated event (see ``foreman.classify.is_human_event``), so it never
needs the thread-ensure side effect ``trigger_foreman`` provides, and
importing this module back into ``foreman.runner`` would recreate the very
cycle this module exists to break.
"""

from __future__ import annotations

from foreman.classify import is_human_event
from foreman.runner import run_foreman_ai
from foreman.thread_service import ensure_conversation_thread
from util.tasks import spawn

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def trigger_foreman(
    guild_id: str,
    event: str,
    human_message: str,
    *,
    user_id: str | None = None,
    task_id: str | None = None,
    task_name: str = "foreman.unknown",
    reply_channel_id: str | None = None,
    skip_thread_ensure: bool = False,
) -> None:
    """Dispatch a trigger into the embedded Foreman runner.

    The standalone process no longer receives trigger events. It is only an API
    proxy used by ``backend.foreman.runner`` at the LLM-call boundary.

    ``reply_channel_id`` pins the Discord destination for this run's narration
    to a specific channel — set by ``discord/router.py`` so a reply to an
    @-mention lands back where it was asked. None (every other caller) keeps
    the default routing in ``discord_notifier.notify_foreman_chat``.

    ``skip_thread_ensure`` lets a caller that already resolved/created the
    conversation thread for this message skip the redundant
    ``ensure_conversation_thread`` round-trip below — ``ws_handlers.handle_chat``
    does this, since it already ensures the thread (and stamps
    ``Message.thread_id``) before this dispatcher ever runs.
    """
    # See foreman.classify for the human/automated event classification shared
    # with routes.tasks.create_task_followup's REST follow-up path.
    is_human = is_human_event(event)

    # Foreman-owned thread lifecycle (#1167): a brand-new human message with
    # no task_id yet is the start (or continuation) of a conversation — the
    # Foreman creates/reuses that conversation's Thread here, as a side
    # effect of handling the message, never something Discord or the
    # frontend originates. Worker-driven events (task-complete, etc.) already
    # carry an existing task_id whose Thread was stamped at task-creation
    # time (see foreman.tools' create_task/assign_task), so nothing to do here.
    if is_human and task_id is None and user_id and not skip_thread_ensure:
        await ensure_conversation_thread(guild_id, user_id, human_message)

    spawn(
        run_foreman_ai(
            guild_id,
            human_message,
            user_id=user_id,
            task_id=task_id,
            is_human=is_human,
            reply_channel_id=reply_channel_id,
            trigger=event,
        ),
        name=task_name,
    )


# ---------------------------------------------------------------------------
# Shared text helpers
# ---------------------------------------------------------------------------


def format_last_output(text: str, max_chars: int = 4000) -> str:
    """Truncate worker output before it's embedded in a Foreman trigger message.

    Foreman messages are delivered over the WebSocket and fed into the LLM's
    context, so we still cap length — just far more generously than the old
    200-char slice, which was cutting off Discord message content.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "... [truncated]"


def format_queued_followup(index: int, payload: dict) -> str:
    """Render one queued pending-followup payload for a foreman trigger message.

    Includes tool/model/provider overrides (when present) so the foreman can
    reissue an equivalent send_followup call instead of losing the override.
    """
    overrides = ", ".join(
        f"{key}={payload[key]}" for key in ("tool", "model", "provider") if payload.get(key)
    )
    suffix = f" [{overrides}]" if overrides else ""
    return f"  {index + 1}. {payload.get('instructions', '')}{suffix}"


# ---------------------------------------------------------------------------
# Trigger message builders
# ---------------------------------------------------------------------------


def format_worker_online_message(
    worker_id: str,
    repos: list[str],
    tools: list[str],
    agent_count: int,
    provider: str | None,
    models: dict | None,
) -> str:
    repos_str = ",".join(repos) if repos else ""
    tools_str = ",".join(tools) if tools else ""
    tools_suffix = f" tools={tools_str}" if tools_str else ""
    provider_suffix = f" provider={provider}" if provider else ""
    model_suffix = ""
    if isinstance(models, dict) and models:
        counts = {
            tool_name: len(rows) for tool_name, rows in models.items() if isinstance(rows, list)
        }
        if counts:
            model_suffix = " models=" + ",".join(f"{k}:{v}" for k, v in sorted(counts.items()))
    return (
        f"[worker-online] worker_id={worker_id} repos={repos_str} agent_count={agent_count}"
        f"{tools_suffix}{provider_suffix}{model_suffix}"
    )


def format_worker_offline_message(worker_id: str, reason: str) -> str:
    return f"[worker-offline] worker_id={worker_id} reason={reason}"


def format_worker_offline_batch_message(worker_ids: list[str]) -> str:
    """Batch variant used when several workers drop off a socket at once.

    Collapsed into a single trigger so a simultaneous mass-disconnect doesn't
    stampede the foreman with N concurrent embedded-foreman runs.
    """
    return "\n".join(
        f"[worker-offline] worker_id={wid} reason=disconnect" for wid in sorted(worker_ids)
    )


def format_task_rejected_message(worker_id: str, task_id: str, reason: str) -> str:
    return (
        f"[task-rejected] Worker {worker_id} rejected task {task_id}: {reason}. "
        "The task was returned to pending/unassigned. Pick another idle worker or spawn one."
    )


def format_task_error_message(
    worker_id: str,
    task_id: str,
    *,
    queued_payloads: list[dict],
    stop_reason: str = "",
    last_text: str = "",
) -> str:
    if queued_payloads:
        queued_summary = "\n".join(
            format_queued_followup(i, p) for i, p in enumerate(queued_payloads)
        )
        return (
            f"[task-error] Worker {worker_id} reported task {task_id} as errored. "
            f"While the task was locked, {len(queued_payloads)} follow-up request(s) were queued:\n"
            f"{queued_summary}\n"
            "The queued follow-ups were NOT dispatched because the task errored. "
            "Decide: call send_followup to retry, or call finalize_task with "
            "outcome='failed' to mark it failed."
        )
    detail = f" Stop reason: {stop_reason}." if stop_reason else ""
    if last_text:
        detail += f' Last output: "{format_last_output(last_text)}"'
    return (
        f"[task-error] Worker {worker_id} reported task {task_id} as errored.{detail} "
        "Decide: call send_followup to retry the task, or call finalize_task with "
        "outcome='failed' to mark it failed."
    )


def format_task_complete_message(
    worker_id: str,
    task_id: str,
    desc: str,
    branch: str,
    pr_url: str,
    stop_reason: str | None,
    last_text: str,
) -> str:
    pr_line = f" PR: {pr_url}." if pr_url else ""
    last_text_snippet = f' Last output: "{format_last_output(last_text)}".' if last_text else ""
    if pr_url:
        # PR exists: lifecycle is driven by GitHub webhooks, not the foreman.
        # The task will be auto-finalized on merge or auto-failed on close without merge.
        if stop_reason == "max_turns":
            return (
                f"[task-complete/max-turns] Worker {worker_id} task {task_id}: "
                f'"{desc[:80]}" — branch: {branch}.{pr_line} '
                f"The runner hit its max-turns limit before finishing. Partial work committed.{last_text_snippet} "
                "IMPORTANT: DO NOT call finalize_task — the task will be automatically "
                "finalized when the PR is merged (or marked failed if the PR is closed without "
                "merging). Use send_followup to continue work on the same branch/worktree."
            )
        return (
            f"[task-complete] Worker {worker_id} finished task {task_id}: "
            f'"{desc[:80]}" — branch: {branch}.{pr_line} '
            "IMPORTANT: DO NOT call finalize_task now. The task will be automatically "
            "finalized when the PR is merged (or automatically marked failed if the PR "
            "is closed without merging). Only call send_followup if CI fails or reviewers "
            "request changes."
        )
    if stop_reason == "max_turns":
        return (
            f"[task-complete/max-turns] Worker {worker_id} task {task_id}: "
            f'"{desc[:80]}" — branch: {branch}. '
            f"The runner hit its max-turns limit and stopped before finishing. "
            f"Partial work has been committed and the branch pushed.{last_text_snippet} "
            "Call send_followup with a continuation prompt so the worker can resume on the "
            "same branch/worktree. Only call finalize_task if the partial work is sufficient "
            "or the task should be abandoned."
        )
    return (
        f"[task-complete] Worker {worker_id} finished task {task_id}: "
        f'"{desc[:80]}" — branch: {branch}. '
        "No PR was opened. Call send_followup for more work, or finalize_task to close "
        "this task (use outcome='failed' if the task did not succeed)."
    )


def format_followup_done_message(
    worker_id: str,
    task_id: str,
    *,
    stop_reason: str | None,
    last_text: str,
    queued_payloads: list[dict],
) -> str:
    if stop_reason == "max_turns":
        last_text_snippet = f' Last output: "{format_last_output(last_text)}".' if last_text else ""
        return (
            f"[followup-done/max-turns] Worker {worker_id} follow-up for task {task_id} "
            f"hit the runner's max-turns limit before finishing. Partial work committed.{last_text_snippet} "
            "Call send_followup with a continuation prompt to resume, or call finalize_task if "
            "the partial work is sufficient."
        )
    if queued_payloads:
        queued_summary = "\n".join(
            format_queued_followup(i, p) for i, p in enumerate(queued_payloads)
        )
        return (
            f"[followup-done] Worker {worker_id} completed a follow-up for task {task_id}. "
            f"While the task was locked, {len(queued_payloads)} follow-up request(s) were queued:\n"
            f"{queued_summary}\n"
            "Review the queued instructions and call send_followup with the relevant ones "
            "(or a combined version), or call finalize_task if the work is done."
        )
    return (
        f"[followup-done] Worker {worker_id} completed a follow-up for task {task_id}. "
        "Decide: call send_followup for more work, or call finalize_task to mark it done."
    )


def format_needs_input_message(
    worker_id: str, task_id: str, description: str, stop_reason: str, last_message: str
) -> str:
    return (
        f"Worker {worker_id} could not complete task {task_id} and needs your help.\n"
        f"Task: {description}\n"
        f"Stop reason: {stop_reason}" + (f"\nLast message: {last_message}" if last_message else "")
    )


def format_user_followup_message(task_id: str, state: str, branch: str, instructions: str) -> str:
    branch_ctx = f" on branch `{branch}`" if branch else ""
    return (
        f"[user-followup] User requested follow-up on task {task_id}{branch_ctx} "
        f'(currently {state}): "{instructions}". '
        "Call send_followup to dispatch this work — it will pick the "
        "original worker if idle, otherwise any idle worker pulls the "
        "branch from GitHub."
    )
