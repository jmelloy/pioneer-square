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

from typing import Annotated, Any, Literal

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
    tool: str = "claude"
    model: str | None = None
    provider: str | None = None
    phase: str | None = None
    parentTaskId: str | None = None
    issueNumber: int | None = None
    issueRepo: str | None = None
    repos: list[str] | None = None


class TaskCreatedMsg(_WS):
    type: Literal["task-created"] = "task-created"
    taskId: str
    name: str
    description: str
    phase: str
    state: str
    createdAt: str


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


class TaskCompleteMsg(_WS):
    """Echoed outbound from inbound worker message."""

    type: Literal["task-complete"] = "task-complete"
    taskId: str | None = None
    workerId: str | None = None
    description: str | None = None
    branch: str | None = None
    prUrl: str | None = None
    lastText: str | None = None
    stopReason: str = "success"


class TaskFollowupMsg(_WS):
    """Follow-up instructions dispatched to a worker."""

    type: Literal["task-followup"] = "task-followup"
    workerId: str
    taskId: str
    name: str = ""
    description: str = ""
    tool: str = "claude"
    model: str | None = None
    provider: str | None = None
    branch: str
    instructions: str
    issueNumber: int | None = None
    issueRepo: str | None = None


class TaskFollowupDoneMsg(_WS):
    """Echoed outbound from inbound worker message."""

    type: Literal["task-followup-done"] = "task-followup-done"
    taskId: str | None = None
    workerId: str | None = None
    stopReason: str = "success"
    lastText: str | None = None
    prUrl: str | None = None


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


class ClaudeAuthRequiredMsg(_WS):
    """Bidirectional – echoed outbound."""

    type: Literal["claude-auth-required"] = "claude-auth-required"
    workerId: str = ""
    url: str = ""


class WorkerAuthResponseMsg(_WS):
    """Bidirectional – echoed outbound."""

    type: Literal["worker-auth-response"] = "worker-auth-response"
    workerId: str = ""
    code: str = ""


class WorkerMessageMsg(_WS):
    type: Literal["worker-message"] = "worker-message"
    workerId: str
    message: str


class WorkerShutdownMsg(_WS):
    type: Literal["worker-shutdown"] = "worker-shutdown"
    workerId: str
    reason: str | None = None


class WorkerPingMsg(_WS):
    type: Literal["worker-ping"] = "worker-ping"
    workerId: str
    timestamp: str
    from_: str = Field("foreman", alias="from")


class ForemanTriggerMsg(_WS):
    type: Literal["foreman-trigger"] = "foreman-trigger"
    event: str
    guildId: str
    humanMessage: str
    userId: str | None = None
    taskId: str | None = None


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


class WorkerDisconnectMsg(_WS):
    type: Literal["worker-disconnect"] = "worker-disconnect"
    workerId: str | None = None


class WorkerPongMsg(_WS):
    type: Literal["worker-pong"] = "worker-pong"
    workerId: str
    timestamp: str | None = None


class ForemanBroadcastMsg(_WS):
    type: Literal["foreman-broadcast"] = "foreman-broadcast"
    payload: dict[str, Any]


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
    | TaskCompleteMsg
    | TaskFollowupMsg
    | TaskFollowupDoneMsg
    | TaskFinalizeMsg
    | TaskCancelMsg
    | TaskRedirectMsg
    | NeedsInputMsg
    | ClaudeAuthRequiredMsg
    | WorkerAuthResponseMsg
    | WorkerMessageMsg
    | WorkerShutdownMsg
    | WorkerPingMsg
    | ForemanTriggerMsg
    | ForemanRegisteredMsg
    | ForemanEvictedMsg
    | ForemanDisconnectMsg
    | ForemanPollStatusMsg
    | GuildUpdatedMsg
    | GithubEventMsg
    | ClaudeUsageMsg
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
    | TaskCompleteMsg
    | TaskFollowupDoneMsg
    | NeedsInputMsg
    | ClaudeAuthRequiredMsg
    | ForemanDisconnectMsg
    | ForemanBroadcastMsg
    | WorkerAuthResponseMsg
    | OfferMsg
    | AnswerMsg
    | IceCandidateMsg,
    Field(discriminator="type"),
]

_inbound_adapter: TypeAdapter[InboundWSMessage] = TypeAdapter(InboundWSMessage)

# Set of type strings that are known inbound message types
KNOWN_INBOUND_TYPES: frozenset[str] = frozenset(
    {
        "ping",
        "join",
        "agent-state",
        "chat",
        "terminal-output",
        "worker-register",
        "worker-disconnect",
        "worker-pong",
        "task-update",
        "task-complete",
        "task-followup-done",
        "needs-input",
        "claude-auth-required",
        "foreman-disconnect",
        "foreman-broadcast",
        "worker-auth-response",
        "offer",
        "answer",
        "ice-candidate",
    }
)


def parse_inbound_message(data: dict[str, Any]) -> InboundWSMessage:
    """Parse and validate a raw inbound WS frame into a typed model.

    Raises ``pydantic.ValidationError`` when the message is malformed.
    """
    return _inbound_adapter.validate_python(data)
