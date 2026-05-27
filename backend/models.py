from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Index, or_, text
from sqlmodel import Field, SQLModel


def live_tasks_filter(now: datetime | None = None):
    """SQL clause matching tasks that have not been soft-deleted.

    A task is "live" when ``deleted_at`` is NULL or set to a future timestamp.
    *now* defaults to the current UTC time; pass an explicit value to make a
    query reproducible in tests.
    """
    if now is None:
        now = datetime.now(UTC)
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

    id: int | None = Field(default=None, primary_key=True)
    guild_id: str
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    name: str | None = None
    github_user_id: str | None = None
    primary_repo: str | None = None
    # HMAC-SHA256 shared secret used to verify GitHub webhook deliveries for
    # this guild. NULL until an owner first requests one via the
    # webhook-secret endpoint.
    webhook_secret: str | None = None
    # A2A AgentCard fields — used to populate /.well-known/agent.json
    description: str | None = None
    url: str | None = None
    version: str | None = None
    # UTC instant at which this guild is considered soft-deleted.
    # NULL = active; partial unique index enforces one active row per guild_id.
    deleted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class Agent(SQLModel, table=True):
    __tablename__ = "agents"

    id: str = Field(primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    worker_id: str | None = Field(default=None, foreign_key="workers.id")
    name: str
    type: str = Field(default="worker", sa_column_kwargs={"server_default": "'worker'"})
    state: str = Field(default="idle", sa_column_kwargs={"server_default": "'idle'"})
    activity: str | None = None
    # Task this agent is currently executing (NULL when idle/offline). Set
    # from worker-emitted agent-state messages; lets the UI map a task row
    # to its agent unambiguously when a worker runs concurrent slots that
    # share a worker_id.
    current_task_id: str | None = None
    joined_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    # UTC instant of the last message received from this agent over the
    # WebSocket. Refreshed by every inbound frame (incl. application-level
    # `ping`); the sweeper marks the agent offline when this gets stale.
    last_seen: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: int | None = Field(default=None, primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    from_agent: str | None = None
    to_agent: str | None = None
    content: str
    message_type: str
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    user_id: str | None = None  # github_user_id of the sender; NULL for system/worker messages
    role: str | None = None  # "tool_use" | "tool_result" | NULL for plain chat
    meta: str | None = None  # JSON blob with extra WS fields (toolId, toolName, …)


class Worker(SQLModel, table=True):
    __tablename__ = "workers"

    id: str = Field(primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    repos: str = Field(default="[]", sa_column_kwargs={"server_default": "'[]'"})
    # Optional GitHub org; when set the worker accepts any task targeting <org>/*
    # and clones repos lazily. NULL for workers that use an explicit repos list only.
    org: str | None = None
    state: str = Field(default="idle", sa_column_kwargs={"server_default": "'idle'"})
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    last_seen: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # Identity of the human user this worker process runs on behalf of.
    # NULL for legacy/unattributed workers.
    user_id: str | None = Field(default=None, foreign_key="users.id")
    # Bearer token issued at registration; required for fetching guild secrets
    # (Claude credentials, GitHub token) over REST. NULL on legacy rows that
    # predate the auth requirement — those workers must re-register to get a
    # token before they can fetch credentials.
    auth_token: str | None = None
    # Human-readable label: ``hostname[:3]/worker_id`` (e.g. ``tok/w-g2otus``).
    # NULL on rows created before this column was added; the API falls back to
    # worker_id for those legacy rows.
    name: str | None = None


class Task(SQLModel, table=True):
    __tablename__ = "tasks"

    id: str = Field(primary_key=True)
    worker_id: str | None = Field(
        default=None, foreign_key="workers.id"
    )  # NULL for foreman-owned tasks
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    description: str
    tool: str = Field(default="claude", sa_column_kwargs={"server_default": "'claude'"})
    issue_number: int | None = None
    issue_repo: str | None = None
    state: str = Field(default="pending", sa_column_kwargs={"server_default": "'pending'"})
    branch: str | None = None
    worktree_path: str | None = None
    pr_url: str | None = None
    # Explicit PR coordinates extracted from pr_url at PR-creation time, so
    # github webhook events can be linked back to the task without fragile
    # URL substring matching. Both NULL until the worker reports a PR.
    pr_number: int | None = None
    pr_repo: str | None = None
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    name: str | None = None
    parent_task_id: str | None = None
    phase: str | None = Field(default="execute", sa_column_kwargs={"server_default": "'execute'"})
    # UTC instant at which this task is considered soft-deleted.
    # NULL = live; once `now() > deleted_at`, list/get queries hide the row.
    deleted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # github_user_id of the human who initiated this task. Used to route
    # worker-driven foreman events (task-complete, etc.) back to the originator's
    # foreman thread in multi-user guilds. NULL on legacy/system tasks.
    user_id: str | None = None


class GithubToken(SQLModel, table=True):
    __tablename__ = "github_tokens"

    github_user_id: str = Field(primary_key=True)
    github_username: str | None = None
    access_token: str
    token_type: str = Field(default="bearer", sa_column_kwargs={"server_default": "'bearer'"})
    scope: str | None = None
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class UserSession(SQLModel, table=True):
    __tablename__ = "user_sessions"

    token: str = Field(primary_key=True)
    github_user_id: str = Field(foreign_key="github_tokens.github_user_id")
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class User(SQLModel, table=True):
    __tablename__ = "users"

    # Canonical user id == GitHub numeric id, kept as Text for FK compatibility
    # with the existing github_user_id columns (messages.user_id,
    # guilds.github_user_id, etc).
    id: str = Field(primary_key=True)
    github_id: str = Field(unique=True)
    github_login: str | None = None
    email: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class GuildMember(SQLModel, table=True):
    __tablename__ = "guild_members"

    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id", primary_key=True)
    user_id: str = Field(foreign_key="users.id", primary_key=True)
    role: str = Field(
        default="member", sa_column_kwargs={"server_default": "'member'"}
    )  # owner | member | viewer
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    id: int | None = Field(default=None, primary_key=True)
    task_id: str | None = Field(default=None, foreign_key="tasks.id")
    timestamp: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    line: str
    worker_id: str | None = None
    agent_id: str | None = None
    data: str | None = None  # JSON: full tool input/output for click-to-expand


class ClaudeCredentials(SQLModel, table=True):
    __tablename__ = "claude_credentials"

    id: int | None = Field(default=None, primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id", unique=True)
    credentials_blob: str  # base64-encoded tar.gz of ~/.claude/
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class GuildKey(SQLModel, table=True):
    __tablename__ = "guild_keys"

    id: int | None = Field(default=None, primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id", unique=True)
    key_id: str  # "kid" in JWK
    public_key_pem: str
    private_key_pem: str
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    # When set, served verbatim at /.well-known/jwks.json instead of the
    # auto-generated key. Stored as JSON text ({"keys": [...]}).
    custom_jwks: str | None = None
    # Private key in JWK format for backend signing; never served publicly.
    private_key_jwk: str | None = None


class ForemanTurn(SQLModel, table=True):
    __tablename__ = "foreman_turns"

    id: int | None = Field(default=None, primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    user_id: str
    role: str  # "user" | "assistant" | "system"
    content_json: str  # JSON-serialized content blocks
    # 1 if this "user" turn carries tool_results (not human input); 0 otherwise
    is_tool_response: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    # For tool_result turns: id of the assistant turn whose tool_use blocks this answers
    parent_id: int | None = Field(default=None, foreign_key="foreman_turns.id")
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    # Token usage from the API response (assistant turns only; NULL for user/system turns)
    input_tokens: int | None = None
    output_tokens: int | None = None


class GithubEvent(SQLModel, table=True):
    __tablename__ = "github_events"

    id: int | None = Field(default=None, primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    # task_id is nullable because an event may arrive before we've linked the
    # PR to a task (e.g. webhook fires for a manually-opened PR).
    task_id: str | None = Field(default=None, foreign_key="tasks.id")
    # X-GitHub-Delivery header value; UNIQUE so GitHub redelivery is a no-op.
    delivery_id: str = Field(unique=True)
    event_type: str
    action: str | None = None
    repo: str  # owner/repo
    pr_number: int | None = None
    pr_url: str | None = None
    sender_login: str | None = None
    payload_json: str
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class Lock(SQLModel, table=True):
    """Standalone key-value lock table. See lock_service.LockService for usage."""

    __tablename__ = "locks"

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True)
    owner: str | None = None
    acquired_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class TaskEvent(SQLModel, table=True):
    """Queued follow-up triggers that arrived while a task was locked.

    When send_followup is called on a task that already holds a follow-up lock,
    the call is serialised here instead of spawning a second worker. On lock
    release (task-followup-done) the foreman is re-triggered with the queued
    instructions so it can decide whether to dispatch them.
    """

    __tablename__ = "task_events"

    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(foreign_key="tasks.id")
    # "pending-followup" is the only event_type currently; reserved for future use.
    event_type: str
    payload_json: str  # JSON: instructions, preferred_worker_id
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class PushToken(SQLModel, table=True):
    """APNs (or, in the future, FCM) device token registered by the iOS app.

    The token is the primary key because Apple's tokens are globally unique
    per app install — when the same token comes back from a different user
    it means the device was re-provisioned and we overwrite ``user_id``.
    """

    __tablename__ = "push_tokens"

    token: str = Field(primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    platform: str = Field(
        default="ios", sa_column_kwargs={"server_default": "'ios'"}
    )  # ios | (future: android)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    last_seen_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
