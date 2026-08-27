"""The runner seam: a closed run outcome shared by every tool adapter.

Each tool adapter (claude_runner, codex_runner, pi_runner) used to hand back a
bare ``(success, stop_reason, last_text, session_id)`` tuple with no shared
vocabulary — `stop_reason` was three different string sets depending on which
runner produced it, and claude's was a verbatim pass-through of whatever the
Claude Code CLI happened to emit that day. Callers (worker.py, the tests)
encoded a contract no runner actually implemented.

``RunResult`` closes that: every runner maps its own native reason strings
into the fixed ``StopReason`` vocabulary below before handing the result back,
so a caller branching on ``result.stop_reason`` is branching on a union this
module owns, not on Claude's (or Codex's, or Pi's) implementation details.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Awaitable, Callable
from typing import Protocol

EmitFn = Callable[..., Awaitable[None]]  # emit(line: str, detail: dict | None = None)
UsageFn = Callable[[dict], Awaitable[None]]  # on_usage(record: dict)
OnProcFn = Callable[[object], None]  # on_proc(process_handle) — worker's live-handle callback


class StopReason(enum.StrEnum):
    """The closed set of reasons a run (or the task it belonged to) stopped.

    SUCCESS, MAX_TURNS, ERROR, INTERRUPTED, and NO_EVENTS are the reasons a
    runner itself can report from ``Runner.run()``. NEEDS_INPUT is reserved
    for a runner that detects it needs human input mid-run (no runner emits
    it today). PUSH_FAILED and NO_CHANGES are never produced by a runner —
    they are downgrades the worker applies afterwards, once it knows whether
    the agent's "success" actually produced a pushable commit (see
    ``RunResult.with_stop_reason``); they live in the same closed union so
    that downgrade is a typed transition instead of overwriting a bare string.
    """

    SUCCESS = "success"
    MAX_TURNS = "max_turns"
    ERROR = "error_during_execution"
    INTERRUPTED = "interrupted"
    NO_EVENTS = "no_events"
    NEEDS_INPUT = "needs_input"
    PUSH_FAILED = "push_failed"
    NO_CHANGES = "no_changes"


@dataclasses.dataclass(frozen=True)
class RunRequest:
    """Everything a runner needs to execute one turn of a task.

    Fields a given runner doesn't use (e.g. ``provider`` for claude/codex) are
    simply ignored. Runner-owned configuration that doesn't vary per call —
    binary path, extra CLI args, max-turns budget, API keys — lives on the
    Runner instance itself (set up once by the registry from Config), not
    here.
    """

    description: str
    cwd: str
    emit: EmitFn
    env: dict[str, str] | None = None
    on_usage: UsageFn | None = None
    on_proc: OnProcFn | None = None
    resume_session_id: str | None = None
    model: str | None = None
    provider: str | None = None


@dataclasses.dataclass(frozen=True)
class RunResult:
    """The closed outcome of one runner invocation.

    ``final_message`` is the run's last assistant-authored text — spliced
    into the Foreman's prompt by the backend, so it is data, not diagnostic
    output. ``raw_stop_reason`` retains the tool's own native reason string
    (e.g. Claude Code's literal result subtype) purely for logging; nothing
    should branch on it — that vocabulary is exactly what ``stop_reason``
    exists to close over.
    """

    success: bool
    stop_reason: StopReason
    final_message: str = ""
    session_id: str | None = None
    raw_stop_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stop_reason, StopReason):
            raise TypeError(
                f"RunResult.stop_reason must be a StopReason, got {self.stop_reason!r} "
                f"(runners must map their native vocabulary into StopReason before returning)"
            )

    def with_stop_reason(self, reason: StopReason) -> RunResult:
        """Return a copy downgraded to *reason*, forcing ``success=False``.

        Used by the worker after a runner reports success but the outcome
        turns out not to be reviewable (push failed, no commits) — a typed
        transition instead of mutating the success/stop_reason locals in
        place.
        """
        return dataclasses.replace(self, success=False, stop_reason=reason)


class ProcessHandle(Protocol):
    """Live subprocess handle a runner hands the worker via ``RunRequest.on_proc``.

    Every runner's process wrapper (claude's ``ClaudeProcess``, codex's
    ``CodexProcess``, pi's ``PiProcess``) duck-types this so worker.py's
    cancel/redirect/message-injection paths work regardless of which tool is
    actually running — the seam these paths hold onto is this Protocol, not
    any one tool's concrete class.
    """

    session_id: str | None

    async def send_message(self, text: str) -> bool: ...

    async def terminate(self) -> None: ...


class Runner(Protocol):
    """The seam every tool adapter (claude/codex/pi/...) implements.

    An instance owns everything specific to its tool — binary path, CLI
    args, credential probing, provider aliasing — so adding a new tool means
    writing a new module and registering it, not editing the dispatcher.
    """

    async def run(self, req: RunRequest) -> RunResult: ...

    async def probe_credentials(self, env: dict[str, str]) -> bool:
        """Return True if this tool has usable credentials in *env*."""
        ...

    async def list_models(self, env: dict[str, str]) -> list[dict]:
        """Return this tool's live model catalog for *env*, or [] if unknown/unsupported."""
        ...
