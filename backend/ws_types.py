"""Pydantic v2 discriminated-union models for the Pioneer Square WebSocket protocol.

Two unions are exported:
  - ``OutboundWSMessage``: all message types the *backend* sends to browsers/workers
  - ``InboundWSMessage``:  all message types *received* from workers or the foreman

Bidirectional types (inbound echo → outbound) appear once under a shared name.

Usage in handlers::

    from ws_types import AgentStateMsg, TaskAssignedMsg
    from events import broadcast_msg, send_ws_message

    await broadcast_msg(guild_id, AgentStateMsg(agentId=aid, state="offline"))
    await send_ws_message(ws, TaskAssignedMsg(workerId=wid, taskId=tid, ...))

Inbound validation in dispatch::

    from ws_types import parse_inbound_message
    from pydantic import ValidationError

    try:
        parsed = parse_inbound_message(data)
    except ValidationError as exc:
        logger.warning("Invalid inbound WS message: %s", exc)
        return
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class _WS(BaseModel):
    """Shared base for all WS message models."""

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# Outbound: backend → browser / worker
# ---------------------------------------------------------------------------


class PongMsg(_WS):
    type: Literal["pong"] = "pong"
    timestamp: str


class AgentJoinedMsg(_WS):
    type: Literal["agent-joined"] = "agent-joined"
    agentId: str | None = None
    agentName: str
    agentType: str
    workerId: str | None = None
    state: str = "idle"
    joinedAt: str
    workerName: str | None = None


class AgentStateMsg(_WS):
    """Bidirectional: received from worker, echoed to all clients."""

    type: Literal["agent-state"] = "agent-state"
    agentId: str | None = None
    state: str
    workerId: str | None = None
    activity: str | None = None
    taskId: str | None = None


class ChatMsg(_WS):
    """Chat message – bidirectional; outbound adds createdAt/userId/role fields."""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["chat"] = "chat"
    from_: str = Field("user", alias="from")
    to: str = "foreman"
    content: str = ""
    createdAt: str | None = None
    userId: str | None = None
    role: str | None = None
    toolName: str | None = None
    toolInput: dict[str, Any] | None = None
    toolId: str | None = None
    toolOutput: str | None = None
    isError: bool | None = None
    # Origin of the message: "web", "discord", "api". Omitted (None) means
    # "web" — the frontend only renders an origin label for non-web sources.
    source: str | None = None
    # Set on Foreman -> user messages that concern a specific task (issue
    # #1200: task_id is metadata on the run's conversation, not a separate
    # Foreman context). None when the run isn't about any particular task.
    # The frontend badges these so it's clear which task a line concerns.
    taskId: str | None = None
    # Foreman-owned conversation thread (#1167) this message belongs to, when
    # resolvable — lets the frontend route a live message into the right
    # thread pane in addition to the guild-wide comms pane. None for messages
    # sent before any thread exists yet, or not scoped to a conversation.
    threadId: str | None = None


class TerminalOutputMsg(_WS):
    """Terminal log line – bidirectional; outbound adds timestamp."""

    type: Literal["terminal-output"] = "terminal-output"
    agentId: str | None = None
    workerId: str | None = None
    taskId: str | None = None
    line: str = ""
    timestamp: str | None = None
    detail: dict[str, Any] | None = None
    level: str | None = None


class TaskAssignedMsg(_WS):
    type: Literal["task-assigned"] = "task-assigned"
    workerId: str
    taskId: str
    name: str = ""
    description: str = ""
    tool: str = "pi"
    taskType: str | None = None
    targetAgentId: str | None = None
    model: str | None = None
    modelTier: str | None = None
    provider: str | None = None
    phase: str | None = None
    parentTaskId: str | None = None
    issueNumber: int | None = None
    issueRepo: str | None = None
    prNumber: int | None = None
    prRepo: str | None = None
    branch: str | None = None
    prUrl: str | None = None
    headSha: str | None = None
    repos: list[str] | None = None
    # Conversation thread this task was created from (#1167). None for tasks
    # not tied to a Foreman-owned thread (issue pickups, webhook-triggered work).
    threadId: str | None = None


class TaskCreatedMsg(_WS):
    type: Literal["task-created"] = "task-created"
    taskId: str
    name: str
    description: str
    phase: str
    taskType: str | None = None
    state: str
    createdAt: str
    # Conversation thread this task was created from (#1167). None for tasks
    # not tied to a Foreman-owned thread (issue pickups, webhook-triggered work).
    threadId: str | None = None


class TaskUpdateMsg(_WS):
    """Task state patch – bidirectional; exact fields vary by source."""

    type: Literal["task-update"] = "task-update"
    taskId: str
    state: str | None = None
    workerId: str | None = None
    branch: str | None = None
    worktreePath: str | None = None
    prUrl: str | None = None
    deletedAt: str | None = None
    phase: str | None = None
    # Present on the worker's state="error" report — a plain task-update, not
    # a task-complete/task-followup-done, so these ride along here instead.
    agentId: str | None = None
    stopReason: str | None = None
    lastText: str | None = None
    sessionId: str | None = None


class TaskRejectedMsg(_WS):
    """Worker -> backend: assignment could not be accepted immediately."""

    type: Literal["task-rejected"] = "task-rejected"
    taskId: str
    workerId: str
    reason: str = "worker has no idle agent slot"


class TaskCompleteMsg(_WS):
    """Echoed outbound from inbound worker message."""

    type: Literal["task-complete"] = "task-complete"
    taskId: str | None = None
    workerId: str | None = None
    agentId: str | None = None
    description: str | None = None
    branch: str | None = None
    prUrl: str | None = None
    lastText: str | None = None
    stopReason: str = "success"
    sessionId: str | None = None
    # Worker-reported post-run state (e.g. "awaiting-review",
    # "awaiting-foreman-review") — determines the next task state.
    state: str | None = None


class TaskFollowupMsg(_WS):
    """Follow-up instructions dispatched to a worker."""

    type: Literal["task-followup"] = "task-followup"
    workerId: str
    taskId: str
    name: str = ""
    description: str = ""
    tool: str = "pi"
    model: str | None = None
    modelTier: str | None = None
    provider: str | None = None
    branch: str
    instructions: str
    issueNumber: int | None = None
    issueRepo: str | None = None
    # Prior agent session ID, only set when this follow-up is dispatched back
    # to the same worker that ran the task (see send_followup in foreman/tools.py).
    sessionId: str | None = None
    # True when the foreman explicitly asked send_followup to open a PR once
    # this follow-up pushes (#1095 — PR creation is no longer automatic).
    createPr: bool = False


class TaskFollowupDoneMsg(_WS):
    """Echoed outbound from inbound worker message."""

    type: Literal["task-followup-done"] = "task-followup-done"
    taskId: str | None = None
    workerId: str | None = None
    agentId: str | None = None
    stopReason: str = "success"
    lastText: str | None = None
    prUrl: str | None = None
    sessionId: str | None = None
    # Worker-reported post-run state (e.g. "awaiting-review",
    # "awaiting-foreman-review") — determines the next task state.
    state: str | None = None


class TaskFinalizeMsg(_WS):
    type: Literal["task-finalize"] = "task-finalize"
    workerId: str | None = None
    taskId: str


class TaskCancelMsg(_WS):
    type: Literal["task-cancel"] = "task-cancel"
    workerId: str | None = None
    taskId: str


class TaskRedirectMsg(_WS):
    type: Literal["task-redirect"] = "task-redirect"
    workerId: str | None = None
    taskId: str
    instructions: str


class NeedsInputMsg(_WS):
    """Bidirectional – echoed outbound."""

    type: Literal["needs-input"] = "needs-input"
    workerId: str = ""
    taskId: str | None = None
    description: str | None = None
    stopReason: str | None = None
    lastMessage: str | None = None


class WorkerStateMsg(_WS):
    """Worker.state changed — lets clients learn about a worker (e.g. "launching")
    before its first agent-joined event, since the sidebar previously had no way
    to see a spawned worker until it fully connected."""

    type: Literal["worker-state"] = "worker-state"
    workerId: str
    state: str
    name: str | None = None


class WorkerOutdatedMsg(_WS):
    """Backend -> worker: this worker is running an older version.

    Informational only — the worker logs it and keeps running its current work.
    Replaces the old version-mismatch worker-shutdown signal."""

    type: Literal["worker-outdated"] = "worker-outdated"
    workerId: str
    reason: str | None = None


class WorkerMessageMsg(_WS):
    type: Literal["worker-message"] = "worker-message"
    workerId: str
    message: str
    # Which of the worker's concurrently running agents the message is for. A
    # worker runs max_agents tasks at once, so without this the worker has to
    # guess and can inject the text into an unrelated task's agent. None = let
    # the worker deliver only if exactly one agent is running.
    taskId: str | None = None


class WorkerShutdownMsg(_WS):
    type: Literal["worker-shutdown"] = "worker-shutdown"
    workerId: str
    reason: str | None = None


class WorkerPingMsg(_WS):
    type: Literal["worker-ping"] = "worker-ping"
    workerId: str
    timestamp: str
    from_: str = Field("foreman", alias="from")


class ForemanApiRequestMsg(_WS):
    """Backend -> external foreman proxy: execute one LLM API request."""

    type: Literal["foreman-api-request"] = "foreman-api-request"
    requestId: str
    guildId: str
    model: str
    maxTokens: int = 1024
    system: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    toolChoice: dict[str, Any] | None = None


class ForemanRegisteredMsg(_WS):
    type: Literal["foreman-registered"] = "foreman-registered"
    guildId: str
    agentId: str | None = None


class ForemanEvictedMsg(_WS):
    type: Literal["foreman-evicted"] = "foreman-evicted"
    guildId: str
    reason: str


class ForemanDisconnectMsg(_WS):
    type: Literal["foreman-disconnect"] = "foreman-disconnect"
    guildId: str | None = None


class ForemanPollStatusMsg(_WS):
    type: Literal["foreman-poll-status"] = "foreman-poll-status"
    nextCheckIn: int | float


class GuildUpdatedMsg(_WS):
    type: Literal["guild-updated"] = "guild-updated"
    id: str
    name: str | None = None
    primary_repo: str | None = None
    description: str | None = None
    url: str | None = None
    version: str | None = None


class ThreadCreatedMsg(_WS):
    """A Foreman-owned conversation thread was created (#1167).

    Broadcast the first time the Foreman handles a message for a
    conversation with no active thread yet — thread creation is always a
    side effect of the Foreman handling a message, never something a
    downstream mirror (Discord, frontend) originates itself.
    """

    type: Literal["thread-created"] = "thread-created"
    threadId: str
    conversationId: int
    userId: str | None = None
    name: str | None = None
    status: str
    createdAt: str


class ThreadUpdatedMsg(_WS):
    """A thread's lifecycle state changed — patch-style, like ``TaskUpdateMsg``."""

    type: Literal["thread-updated"] = "thread-updated"
    threadId: str
    status: str | None = None
    discordThreadId: str | None = None
    deletedAt: str | None = None


class GithubEventMsg(_WS):
    type: Literal["github-event"] = "github-event"
    deliveryId: str | None = None
    event: str | None = None
    action: str | None = None
    repo: str | None = None
    prNumber: int | None = None
    prUrl: str | None = None
    taskId: str | None = None
    sender: str | None = None


class ClaudeUsageMsg(_WS):
    """LLM session usage stats. Extra fields from _summarize() are allowed."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    type: Literal["claude-usage"] = "claude-usage"
    taskId: str | None = None
    workerId: str | None = None
    sessionId: str | None = None
    # Tool runner that produced this usage (e.g. "claude", "pi", "codex").
    tool: str | None = None
    model: str | None = None
    repo: str | None = None
    reporter: str | None = None


# WebRTC signaling – forwarded verbatim; extra fields allowed
class OfferMsg(_WS):
    model_config = ConfigDict(populate_by_name=True, extra="allow")
    type: Literal["offer"] = "offer"


class AnswerMsg(_WS):
    model_config = ConfigDict(populate_by_name=True, extra="allow")
    type: Literal["answer"] = "answer"


class IceCandidateMsg(_WS):
    model_config = ConfigDict(populate_by_name=True, extra="allow")
    type: Literal["ice-candidate"] = "ice-candidate"


# ---------------------------------------------------------------------------
# Inbound-only types (received from workers / browser / external foreman)
# ---------------------------------------------------------------------------


class PingMsg(_WS):
    type: Literal["ping"] = "ping"


class JoinMsg(_WS):
    type: Literal["join"] = "join"
    agentId: str | None = None
    agentName: str = "Unknown"
    agentType: str = "worker"
    workerId: str | None = None
    external: bool | None = None


class WorkerRegisterMsg(_WS):
    type: Literal["worker-register"] = "worker-register"
    workerId: str | None = None
    repos: list[str] | None = None
    user: str | None = None
    hostname: str | None = None
    tools: list[str] | None = None
    models: dict[str, Any] | None = None
    provider: str | None = None
    tool: str | None = None


class WorkerDisconnectMsg(_WS):
    type: Literal["worker-disconnect"] = "worker-disconnect"
    workerId: str | None = None
    reason: str | None = None


class WorkerPongMsg(_WS):
    type: Literal["worker-pong"] = "worker-pong"
    workerId: str
    timestamp: str | None = None


class ForemanApiResponseMsg(_WS):
    """External foreman proxy -> backend: result for one LLM API request."""

    type: Literal["foreman-api-response"] = "foreman-api-response"
    requestId: str
    guildId: str | None = None
    ok: bool = True
    response: dict[str, Any] | None = None
    error: str | None = None
    apiRequestId: str | None = None
    provider: str | None = None
    model: str | None = None


# ---------------------------------------------------------------------------
# Discriminated unions
# ---------------------------------------------------------------------------

OutboundWSMessage = Annotated[
    PongMsg
    | AgentJoinedMsg
    | AgentStateMsg
    | ChatMsg
    | TerminalOutputMsg
    | TaskAssignedMsg
    | TaskCreatedMsg
    | TaskUpdateMsg
    | TaskRejectedMsg
    | TaskCompleteMsg
    | TaskFollowupMsg
    | TaskFollowupDoneMsg
    | TaskFinalizeMsg
    | TaskCancelMsg
    | TaskRedirectMsg
    | NeedsInputMsg
    | WorkerMessageMsg
    | WorkerShutdownMsg
    | WorkerOutdatedMsg
    | WorkerStateMsg
    | WorkerPingMsg
    | ForemanApiRequestMsg
    | ForemanRegisteredMsg
    | ForemanEvictedMsg
    | ForemanDisconnectMsg
    | ForemanPollStatusMsg
    | GuildUpdatedMsg
    | GithubEventMsg
    | ClaudeUsageMsg
    | ThreadCreatedMsg
    | ThreadUpdatedMsg
    | OfferMsg
    | AnswerMsg
    | IceCandidateMsg,
    Field(discriminator="type"),
]

InboundWSMessage = Annotated[
    PingMsg
    | JoinMsg
    | AgentStateMsg
    | ChatMsg
    | TerminalOutputMsg
    | WorkerRegisterMsg
    | WorkerDisconnectMsg
    | WorkerPongMsg
    | TaskUpdateMsg
    | TaskRejectedMsg
    | TaskCompleteMsg
    | TaskFollowupDoneMsg
    | NeedsInputMsg
    | ForemanDisconnectMsg
    | ForemanApiResponseMsg
    | OfferMsg
    | AnswerMsg
    | IceCandidateMsg,
    Field(discriminator="type"),
]

_inbound_adapter: TypeAdapter[InboundWSMessage] = TypeAdapter(InboundWSMessage)


def _discriminator_values(union: Any) -> frozenset[str]:
    """Extract the ``type`` literal of every variant in a discriminated union.

    Works on the ``Annotated[A | B | ..., Field(discriminator="type")]`` shape
    used by ``InboundWSMessage``/``OutboundWSMessage``: unwraps the
    ``Annotated`` layer, then reads each variant's default value for its
    ``type`` field (always a one-value ``Literal``).
    """
    (variants_type,) = get_args(union)[:1]
    return frozenset(variant.model_fields["type"].default for variant in get_args(variants_type))


# Set of type strings that are known inbound message types — derived from
# InboundWSMessage so it can never drift from the union it describes (a
# hand-maintained copy previously missed "task-rejected", letting it bypass
# validation entirely).
KNOWN_INBOUND_TYPES: frozenset[str] = _discriminator_values(InboundWSMessage)


def parse_inbound_message(data: dict[str, Any]) -> InboundWSMessage:
    """Parse and validate a raw inbound WS frame into a typed model.

    Raises ``pydantic.ValidationError`` when the message is malformed.
    """
    return _inbound_adapter.validate_python(data)


def protocol_spec() -> dict[str, dict[str, list[str]]]:
    """Machine-readable snapshot of the WS protocol's *closed* message shapes.

    For each direction, every message type's field names (wire keys — aliases
    resolved, ``"type"`` excluded). Models with ``extra="allow"``
    (``claude-usage``, the WebRTC signaling types) have an open, dynamic shape
    by design and are omitted — there's no fixed field set to compare against.

    Source of truth for the frontend/backend protocol parity check: see
    ``scripts/export_ws_protocol.py`` and
    ``frontend/src/generated/ws-protocol.spec.ts``.
    """

    def _spec(union: Any) -> dict[str, list[str]]:
        (variants_type,) = get_args(union)[:1]
        out: dict[str, list[str]] = {}
        for variant in get_args(variants_type):
            if variant.model_config.get("extra") == "allow":
                continue
            type_name = variant.model_fields["type"].default
            out[type_name] = sorted(
                (info.alias or name)
                for name, info in variant.model_fields.items()
                if name != "type"
            )
        return out

    return {"outbound": _spec(OutboundWSMessage), "inbound": _spec(InboundWSMessage)}
