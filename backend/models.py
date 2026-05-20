from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import Index, or_, text
from sqlmodel import Field, SQLModel


def live_tasks_filter(now: str | None = None):
    """SQL clause matching tasks that have not been soft-deleted.

    A task is "live" when ``deleted_at`` is NULL or set to a future timestamp.
    *now* defaults to the current UTC time as an ISO-8601 string; pass an
    explicit value to make a query reproducible in tests.
    """
    if now is None:
        now = datetime.now(UTC).isoformat()
    return or_(Task.deleted_at.is_(None), Task.deleted_at > now)


class Guild(SQLModel, table=True):
    __tablename__ = "guilds"
    __table_args__ = (
        Index(
            "uq_guilds_guild_id_active",
            "guild_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    guild_id: str
    created_at: str
    name: Optional[str] = None
    github_user_id: Optional[str] = None
    primary_repo: Optional[str] = None
    # HMAC-SHA256 shared secret used to verify GitHub webhook deliveries for
    # this guild. NULL until an owner first requests one via the
    # webhook-secret endpoint.
    webhook_secret: Optional[str] = None
    # A2A AgentCard fields — used to populate /.well-known/agent.json
    description: Optional[str] = None
    url: Optional[str] = None
    version: Optional[str] = None
    # ISO-8601 UTC timestamp at which this guild is considered soft-deleted.
    # NULL = active; partial unique index enforces one active row per guild_id.
    deleted_at: Optional[str] = None


class Agent(SQLModel, table=True):
    __tablename__ = "agents"

    id: str = Field(primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    worker_id: Optional[str] = Field(default=None, foreign_key="workers.id")
    name: str
    type: str = Field(default="worker")
    state: str = Field(default="idle")
    activity: Optional[str] = None
    # Task this agent is currently executing (NULL when idle/offline). Set
    # from worker-emitted agent-state messages; lets the UI map a task row
    # to its agent unambiguously when a worker runs concurrent slots that
    # share a worker_id.
    current_task_id: Optional[str] = None
    joined_at: str
    # ISO-8601 UTC timestamp of the last message received from this agent over
    # the WebSocket. Refreshed by every inbound frame (incl. application-level
    # `ping`); the sweeper marks the agent offline when this gets stale.
    last_seen: Optional[str] = None


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: Optional[int] = Field(default=None, primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    from_agent: Optional[str] = None
    to_agent: Optional[str] = None
    content: str
    message_type: str
    created_at: str
    user_id: Optional[str] = None  # github_user_id of the sender; NULL for system/worker messages
    role: Optional[str] = None  # "tool_use" | "tool_result" | NULL for plain chat
    meta: Optional[str] = None  # JSON blob with extra WS fields (toolId, toolName, …)


class Worker(SQLModel, table=True):
    __tablename__ = "workers"

    id: str = Field(primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    repos: str = Field(default="[]")
    # Optional GitHub org; when set the worker accepts any task targeting <org>/*
    # and clones repos lazily. NULL for workers that use an explicit repos list only.
    org: Optional[str] = None
    state: str = Field(default="idle")
    created_at: str
    last_seen: Optional[str] = None
    # Identity of the human user this worker process runs on behalf of.
    # NULL for legacy/unattributed workers.
    user_id: Optional[str] = Field(default=None, foreign_key="users.id")
    # Bearer token issued at registration; required for fetching guild secrets
    # (Claude credentials, GitHub token) over REST. NULL on legacy rows that
    # predate the auth requirement — those workers must re-register to get a
    # token before they can fetch credentials.
    auth_token: Optional[str] = None
    # Human-readable label: ``hostname[:3]/worker_id`` (e.g. ``tok/w-g2otus``).
    # NULL on rows created before this column was added; the API falls back to
    # worker_id for those legacy rows.
    name: Optional[str] = None


class Task(SQLModel, table=True):
    __tablename__ = "tasks"

    id: str = Field(primary_key=True)
    worker_id: Optional[str] = Field(default=None, foreign_key="workers.id")  # NULL for foreman-owned tasks
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    description: str
    tool: str = Field(default="claude")
    issue_number: Optional[int] = None
    issue_repo: Optional[str] = None
    state: str = Field(default="pending")
    branch: Optional[str] = None
    worktree_path: Optional[str] = None
    pr_url: Optional[str] = None
    # Explicit PR coordinates extracted from pr_url at PR-creation time, so
    # github webhook events can be linked back to the task without fragile
    # URL substring matching. Both NULL until the worker reports a PR.
    pr_number: Optional[int] = None
    pr_repo: Optional[str] = None
    created_at: str
    finished_at: Optional[str] = None
    name: Optional[str] = None
    parent_task_id: Optional[str] = None
    phase: Optional[str] = Field(default="execute")
    # ISO-8601 UTC timestamp at which this task is considered soft-deleted.
    # NULL = live; once `now() > deleted_at`, list/get queries hide the row.
    deleted_at: Optional[str] = None
    # github_user_id of the human who initiated this task. Used to route
    # worker-driven foreman events (task-complete, etc.) back to the originator's
    # foreman thread in multi-user guilds. NULL on legacy/system tasks.
    user_id: Optional[str] = None


class GithubToken(SQLModel, table=True):
    __tablename__ = "github_tokens"

    github_user_id: str = Field(primary_key=True)
    github_username: Optional[str] = None
    access_token: str
    token_type: str = Field(default="bearer")
    scope: Optional[str] = None
    created_at: str
    updated_at: str


class UserSession(SQLModel, table=True):
    __tablename__ = "user_sessions"

    token: str = Field(primary_key=True)
    github_user_id: str = Field(foreign_key="github_tokens.github_user_id")
    created_at: str


class User(SQLModel, table=True):
    __tablename__ = "users"

    # Canonical user id == GitHub numeric id, kept as Text for FK compatibility
    # with the existing github_user_id columns (messages.user_id,
    # guilds.github_user_id, etc).
    id: str = Field(primary_key=True)
    github_id: str = Field(unique=True)
    github_login: Optional[str] = None
    email: Optional[str] = None
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    created_at: str
    updated_at: str


class GuildMember(SQLModel, table=True):
    __tablename__ = "guild_members"

    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id", primary_key=True)
    user_id: str = Field(foreign_key="users.id", primary_key=True)
    # owner | member | viewer
    role: str = Field(default="member")
    created_at: str


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: Optional[str] = Field(default=None, foreign_key="tasks.id")
    timestamp: str
    line: str
    worker_id: Optional[str] = None
    agent_id: Optional[str] = None
    data: Optional[str] = None  # JSON: full tool input/output for click-to-expand


class ClaudeCredentials(SQLModel, table=True):
    __tablename__ = "claude_credentials"

    id: Optional[int] = Field(default=None, primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id", unique=True)
    credentials_blob: str  # base64-encoded tar.gz of ~/.claude/
    updated_at: str


class GuildKey(SQLModel, table=True):
    __tablename__ = "guild_keys"

    id: Optional[int] = Field(default=None, primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id", unique=True)
    key_id: str  # "kid" in JWK
    public_key_pem: str
    private_key_pem: str
    created_at: str
    # When set, served verbatim at /.well-known/jwks.json instead of the
    # auto-generated key. Stored as JSON text ({"keys": [...]}).
    custom_jwks: Optional[str] = None
    # Private key in JWK format for backend signing; never served publicly.
    private_key_jwk: Optional[str] = None


class ForemanTurn(SQLModel, table=True):
    __tablename__ = "foreman_turns"

    id: Optional[int] = Field(default=None, primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    user_id: str
    role: str  # "user" | "assistant" | "system"
    content_json: str  # JSON-serialized content blocks
    # 1 if this "user" turn carries tool_results (not human input); 0 otherwise
    is_tool_response: int = Field(default=0)
    # For tool_result turns: id of the assistant turn whose tool_use blocks this answers
    parent_id: Optional[int] = Field(default=None, foreign_key="foreman_turns.id")
    created_at: str
    # Token usage from the API response (assistant turns only; NULL for user/system turns)
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class GithubEvent(SQLModel, table=True):
    __tablename__ = "github_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    # task_id is nullable because an event may arrive before we've linked the
    # PR to a task (e.g. webhook fires for a manually-opened PR).
    task_id: Optional[str] = Field(default=None, foreign_key="tasks.id")
    # X-GitHub-Delivery header value; UNIQUE so GitHub redelivery is a no-op.
    delivery_id: str = Field(unique=True)
    event_type: str
    action: Optional[str] = None
    repo: str  # owner/repo
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    sender_login: Optional[str] = None
    payload_json: str
    created_at: str


class Lock(SQLModel, table=True):
    """Standalone key-value lock table. See lock_service.LockService for usage."""

    __tablename__ = "locks"

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True)
    owner: Optional[str] = None
    acquired_at: datetime
    expires_at: Optional[datetime] = None


class TaskEvent(SQLModel, table=True):
    """Queued follow-up triggers that arrived while a task was locked.

    When send_followup is called on a task that already holds a follow-up lock,
    the call is serialised here instead of spawning a second worker. On lock
    release (task-followup-done) the foreman is re-triggered with the queued
    instructions so it can decide whether to dispatch them.
    """

    __tablename__ = "task_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(foreign_key="tasks.id")
    # "pending-followup" is the only event_type currently; reserved for future use.
    event_type: str
    payload_json: str  # JSON: instructions, preferred_worker_id
    created_at: str
