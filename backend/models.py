from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Index, Text, or_, text
from sqlmodel import Field, SQLModel, col


def live_tasks_filter(now: datetime | None = None):
    """SQL clause matching tasks that have not been soft-deleted.

    A task is "live" when ``deleted_at`` is NULL or set to a future timestamp.
    *now* defaults to the current UTC time; pass an explicit value to make a
    query reproducible in tests.
    """
    if now is None:
        now = datetime.now(UTC)
    return or_(col(Task.deleted_at).is_(None), col(Task.deleted_at) > now)


class Guild(SQLModel, table=True):
    __tablename__ = "guilds"  # type: ignore[assignment]
    __table_args__ = (
        Index(
            "uq_guilds_slug_active",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    slug: str
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
    # Per-guild foreman AI configuration. NULL = use process defaults.
    foreman_config: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    # UTC instant at which this guild is considered soft-deleted.
    # NULL = active; partial unique index enforces one active row per slug.
    deleted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class Agent(SQLModel, table=True):
    __tablename__ = "agents"  # type: ignore[assignment]
    __table_args__ = (Index("ix_agents_guild_id_state", "guild_id", "state"),)

    id: str = Field(primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    worker_id: str | None = Field(default=None, foreign_key="workers.id", index=True)
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
    # UTC instant of the last agent-scoped message received over the WebSocket.
    # Worker-scoped liveness frames update Worker.last_seen instead.
    last_seen: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class Message(SQLModel, table=True):
    __tablename__ = "messages"  # type: ignore[assignment]
    __table_args__ = (Index("ix_messages_guild_id_created_at", "guild_id", "created_at"),)

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
    # Task this message is associated with. NULL for general chat and system messages
    # that are not scoped to a specific task (e.g. worker-online, periodic-check).
    task_id: str | None = Field(default=None, foreign_key="tasks.id", index=True)


class Worker(SQLModel, table=True):
    __tablename__ = "workers"  # type: ignore[assignment]
    __table_args__ = (
        Index("ix_workers_guild_id_created_at", "guild_id", "created_at"),
        Index("ix_workers_state_last_seen", "state", "last_seen"),
    )

    id: str = Field(primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    repos: str = Field(default="[]", sa_column_kwargs={"server_default": "'[]'"})
    # JSON-serialised list of tool runner names available on this worker (e.g. ["claude", "codex"]).
    tools: str = Field(default="[]", sa_column_kwargs={"server_default": "'[]'"})
    # Optional GitHub org; when set the worker accepts any task targeting <org>/*
    # and clones repos lazily. NULL for workers that use an explicit repos list only.
    org: str | None = None
    state: str = Field(default="idle", sa_column_kwargs={"server_default": "'idle'"})
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    # UTC instant of the last worker-scoped or agent-scoped frame received from
    # this worker process. The backend probes stale workers before marking them
    # and their agents offline.
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
    # Docker container id for this worker process (full 64-char hex or short 12-char).
    # NULL for workers started outside of Docker (compose, manual CLI).
    # Used by the lifecycle module to force-kill stale containers on version change.
    container_id: str | None = None
    # Backend version string recorded at spawn time (PIONEER_VERSION env var or git SHA).
    # On startup, workers whose spawned_version differs from the current version are
    # considered stale and drained/killed before fresh workers are started.
    spawned_version: str | None = None
    # UTC instant the container was successfully started (set after containers.run()).
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # UTC instant a graceful-shutdown signal was sent to this worker (set during drain).
    # NULL until the lifecycle module initiates a drain for this worker.
    drain_requested_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # Set to True when the foreman explicitly shuts down this worker via shutdown_worker.
    # Disabled workers are skipped during startup re-spawn so they are not brought back
    # automatically on the next backend restart.
    disabled: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    # AI provider this worker communicates with (e.g. 'anthropic', 'bedrock', 'openai').
    # NULL on legacy rows that predate this column; set during worker-register.
    provider: str | None = None
    # Primary tool runner this worker is configured for (e.g. 'claude', 'pi', 'codex').
    # NULL on legacy rows; set during worker-register.
    tool: str | None = None


class Task(SQLModel, table=True):
    __tablename__ = "tasks"  # type: ignore[assignment]
    __table_args__ = (
        Index("ix_tasks_guild_id_deleted_at_created_at", "guild_id", "deleted_at", "created_at"),
        Index("ix_tasks_state", "state"),
    )

    id: str = Field(primary_key=True)
    worker_id: str | None = Field(
        default=None, foreign_key="workers.id", index=True
    )  # NULL for foreman-owned tasks
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id")
    description: str
    tool: str = Field(default="claude", sa_column_kwargs={"server_default": "'claude'"})
    model: str | None = None
    provider: str | None = None
    issue_number: int | None = None
    issue_repo: str | None = None
    state: str = Field(default="pending", sa_column_kwargs={"server_default": "'pending'"})
    branch: str | None = None
    worktree_path: str | None = None
    pr_url: str | None = None
    # Explicit PR coordinates extracted from pr_url at PR-creation time, so
    # github webhook events can be linked back to the task without fragile
    # URL substring matching at receive time. Both NULL until the worker reports a PR.
    pr_number: int | None = None
    pr_repo: str | None = None
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
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
    __tablename__ = "github_tokens"  # type: ignore[assignment]

    github_user_id: str = Field(primary_key=True)
    github_username: str | None = None
    access_token: str
    token_type: str = Field(default="bearer", sa_column_kwargs={"server_default": "'bearer'"})
    scope: str | None = None
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class UserSession(SQLModel, table=True):
    __tablename__ = "user_sessions"  # type: ignore[assignment]

    token: str = Field(primary_key=True)
    github_user_id: str = Field(foreign_key="github_tokens.github_user_id")
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class User(SQLModel, table=True):
    __tablename__ = "users"  # type: ignore[assignment]

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
    __tablename__ = "guild_members"  # type: ignore[assignment]
    __table_args__ = (Index("ix_guild_members_guild_id_role", "guild_id", "role"),)

    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id", primary_key=True)
    user_id: str = Field(foreign_key="users.id", primary_key=True)
    role: str = Field(
        default="member", sa_column_kwargs={"server_default": "'member'"}
    )  # owner | member | viewer
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class GuildInvite(SQLModel, table=True):
    __tablename__ = "guild_invites"  # type: ignore[assignment]
    __table_args__ = (Index("ix_guild_invites_guild_id_status", "guild_id", "status"),)

    id: int | None = Field(default=None, primary_key=True)
    guild_id: int = Field(foreign_key="guilds.id")
    # Exactly one of github_login / github_id is non-null, depending on what the inviter typed.
    github_login: str | None = None  # GitHub username (stored lowercase)
    github_id: str | None = None  # GitHub numeric user ID
    role: str = Field(
        default="member", sa_column_kwargs={"server_default": "'member'"}
    )  # owner | member | viewer
    invited_by: str = Field(foreign_key="users.id")
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    # pending | accepted | cancelled
    status: str = Field(default="pending", sa_column_kwargs={"server_default": "'pending'"})


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    task_id: str | None = Field(default=None, foreign_key="tasks.id", index=True)
    timestamp: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    line: str
    worker_id: str | None = None
    agent_id: str | None = None
    data: str | None = None  # JSON: full tool input/output for click-to-expand
    # Semantic line type (worker/auth/claude/thinking/…) so the frontend can
    # style logs without parsing text prefixes. NULL = default agent output.
    level: str | None = None


class ClaudeCredentials(SQLModel, table=True):
    __tablename__ = "claude_credentials"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    # guild_id is the integer FK to guilds.id (renamed from guild_pk).
    guild_id: int = Field(foreign_key="guilds.id", unique=True)
    credentials_blob: str  # base64-encoded tar.gz of ~/.claude/
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class GuildKey(SQLModel, table=True):
    __tablename__ = "guild_keys"  # type: ignore[assignment]

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


class ApiRequestLog(SQLModel, table=True):
    """Full record of one Anthropic messages.create() call made by the foreman.

    Created before the call (with model/system/messages/extra) and updated
    after with request_id, response body, token usage, stop_reason, and
    completed_at. Linked from foreman_turns.request_id (FK to this table's id).
    """

    __tablename__ = "api_request_log"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    # Anthropic x-request-id response header; set after the call completes.
    request_id: str | None = Field(default=None, unique=True)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    model: str
    # Full list of system content blocks sent (matches the list passed to the API).
    system: list | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    # Full outbound messages array.
    messages: list | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    # Full response body from Anthropic (model_dump() of the SDK Message object).
    response: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    stop_reason: str | None = None
    # Task this API call was made on behalf of (nullable; foreman may handle multiple tasks).
    task_id: str | None = Field(default=None, foreign_key="tasks.id")
    # Extra API parameters: max_tokens, tools list, tool_choice, etc.
    extra: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))


class ForemanTurn(SQLModel, table=True):
    __tablename__ = "foreman_turns"  # type: ignore[assignment]

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
    # Token usage — deprecated; use api_request_log for usage going forward.
    # Kept nullable so existing rows are not affected.
    input_tokens: int | None = None
    output_tokens: int | None = None
    # JSON array of Anthropic API call metadata captured for each messages.create() call.
    # Each entry: {request_id?, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, ts}
    # Set on assistant turns; NULL otherwise.
    api_calls_json: str | None = None
    # FK to api_request_log.id for the API call that produced this assistant turn.
    # NULL on user/system turns and on turns predating this column.
    request_id: int | None = Field(default=None, foreign_key="api_request_log.id")
    # Task this turn was produced for (mirrors api_request_log.task_id for convenience).
    task_id: str | None = Field(default=None, foreign_key="tasks.id")


class GithubEvent(SQLModel, table=True):
    __tablename__ = "github_events"  # type: ignore[assignment]

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

    __tablename__ = "locks"  # type: ignore[assignment]

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

    __tablename__ = "task_events"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(foreign_key="tasks.id")
    # "pending-followup" is the only event_type currently; reserved for future use.
    event_type: str
    payload_json: str  # JSON: instructions, preferred_worker_id
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class LlmUsage(SQLModel, table=True):
    """Per-API-call usage captured from a worker's headless tool run.

    The worker parses tool output (e.g. ``claude --output-format stream-json``)
    and reports one row per assistant message (``kind="api_call"``) carrying
    that message's ``message.usage``, plus one ``kind="result"`` row per run
    holding the ``result`` event's self-reported aggregate ``usage`` and
    ``total_cost_usd``. Storing both lets the UI compare the per-call sum
    against the tool's self-reported total (the two disagree in practice).
    """

    __tablename__ = "llm_usage"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    # guild_id is the integer FK to guilds.id.
    guild_id: int = Field(foreign_key="guilds.id", index=True)
    # Worker task this usage belongs to (NULL only for ad-hoc reports).
    task_id: str | None = Field(default=None, foreign_key="tasks.id")
    worker_id: str | None = Field(default=None, foreign_key="workers.id")
    # Tool runner that produced this usage (e.g. "claude", "pi", "codex").
    tool: str = Field(default="claude", sa_column_kwargs={"server_default": "'claude'"})
    # Session id for the run this row came from (e.g. Claude session_id from system:init).
    session_id: str | None = None
    # "api_call" (one assistant message) | "result" (the run's result event).
    kind: str
    # 0-based index of the API call within the task run; NULL for result rows.
    call_index: int | None = None
    # Model id (e.g. "claude-opus-4-8").
    model: str | None = None
    input_tokens: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    output_tokens: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    cache_read_input_tokens: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    cache_creation_input_tokens: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    # Result rows only: self-reported cost/turns/outcome (NULL on api_call rows).
    cost_usd: float | None = None
    num_turns: int | None = None
    stop_reason: str | None = None
    # workspace repo "owner/name" if known.
    repo: str | None = None
    # Free-text label for who reported (worker name / user). NULL if unknown.
    reporter: str | None = None
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class UserSpawnSettings(SQLModel, table=True):
    """Per-user, per-guild spawn-worker preferences (repos, tools, env vars).

    Stored server-side so env var values survive browser sessions without
    being exposed in localStorage.
    """

    __tablename__ = "user_spawn_settings"  # type: ignore[assignment]

    guild_id: int = Field(foreign_key="guilds.id", primary_key=True)
    user_id: str = Field(foreign_key="users.id", primary_key=True)
    # JSON blob: {repos: [...], tools: [...], envVars: [{key, value}, ...]}
    # SECURITY: this column may contain sensitive values (API tokens, credentials,
    # secret env vars). Operators must restrict DB-level read access to this table.
    # TODO: add application-layer encryption for envVars before storing.
    # TODO: tracked in issue #537
    settings_json: str = Field(default="{}", sa_column=Column(Text, server_default="{}"))
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


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


class ModelCatalog(SQLModel, table=True):
    """Persisted snapshot of models.dev provider/model entries.

    Upserted from the models.dev API on startup and periodically refreshed.
    Primary key is (provider, model_id) so upserts are idempotent.
    """

    __tablename__ = "model_catalog"  # type: ignore[assignment]

    provider: str = Field(primary_key=True)
    model_id: str = Field(primary_key=True)
    display_name: str
    capabilities: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    raw: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    fetched_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
