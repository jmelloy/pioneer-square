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
    webhook_secret: str | None = None
    description: str | None = None
    url: str | None = None
    version: str | None = None
    deleted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class Agent(SQLModel, table=True):
    __tablename__ = "agents"

    id: str = Field(primary_key=True)
    guild_id: int = Field(foreign_key="guilds.id")
    worker_id: str | None = Field(default=None, foreign_key="workers.id")
    name: str
    type: str = Field(default="worker", sa_column_kwargs={"server_default": "'worker'"})
    state: str = Field(default="idle", sa_column_kwargs={"server_default": "'idle'"})
    activity: str | None = None
    current_task_id: str | None = None
    joined_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    last_seen: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: int | None = Field(default=None, primary_key=True)
    guild_id: int = Field(foreign_key="guilds.id")
    from_agent: str | None = None
    to_agent: str | None = None
    content: str
    message_type: str
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    user_id: str | None = None
    role: str | None = None
    meta: str | None = None


class Worker(SQLModel, table=True):
    __tablename__ = "workers"

    id: str = Field(primary_key=True)
    guild_id: int = Field(foreign_key="guilds.id")
    repos: str = Field(default="[]", sa_column_kwargs={"server_default": "'[]'"})
    org: str | None = None
    state: str = Field(default="idle", sa_column_kwargs={"server_default": "'idle'"})
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    last_seen: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    user_id: str | None = Field(default=None, foreign_key="users.id")
    auth_token: str | None = None
    name: str | None = None


class Task(SQLModel, table=True):
    __tablename__ = "tasks"

    id: str = Field(primary_key=True)
    worker_id: str | None = Field(default=None, foreign_key="workers.id")
    guild_id: int = Field(foreign_key="guilds.id")
    description: str
    tool: str = Field(default="claude", sa_column_kwargs={"server_default": "'claude'"})
    issue_number: int | None = None
    issue_repo: str | None = None
    state: str = Field(default="pending", sa_column_kwargs={"server_default": "'pending'"})
    branch: str | None = None
    worktree_path: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    pr_repo: str | None = None
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    name: str | None = None
    parent_task_id: str | None = None
    phase: str | None = Field(default="execute", sa_column_kwargs={"server_default": "'execute'"})
    deleted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
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

    guild_id: int = Field(foreign_key="guilds.id", primary_key=True)
    user_id: str = Field(foreign_key="users.id", primary_key=True)
    role: str = Field(default="member", sa_column_kwargs={"server_default": "'member'"})
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    id: int | None = Field(default=None, primary_key=True)
    task_id: str | None = Field(default=None, foreign_key="tasks.id")
    timestamp: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    line: str
    worker_id: str | None = None
    agent_id: str | None = None
    data: str | None = None


class ClaudeCredentials(SQLModel, table=True):
    __tablename__ = "claude_credentials"

    id: int | None = Field(default=None, primary_key=True)
    guild_id: int = Field(foreign_key="guilds.id", unique=True)
    credentials_blob: str
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class GuildKey(SQLModel, table=True):
    __tablename__ = "guild_keys"

    id: int | None = Field(default=None, primary_key=True)
    guild_id: int = Field(foreign_key="guilds.id", unique=True)
    key_id: str
    public_key_pem: str
    private_key_pem: str
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    custom_jwks: str | None = None
    private_key_jwk: str | None = None


class ForemanTurn(SQLModel, table=True):
    __tablename__ = "foreman_turns"

    id: int | None = Field(default=None, primary_key=True)
    guild_id: int = Field(foreign_key="guilds.id")
    user_id: str
    role: str
    content_json: str
    is_tool_response: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    parent_id: int | None = Field(default=None, foreign_key="foreman_turns.id")
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    input_tokens: int | None = None
    output_tokens: int | None = None


class GithubEvent(SQLModel, table=True):
    __tablename__ = "github_events"

    id: int | None = Field(default=None, primary_key=True)
    guild_id: int = Field(foreign_key="guilds.id")
    task_id: str | None = Field(default=None, foreign_key="tasks.id")
    delivery_id: str = Field(unique=True)
    event_type: str
    action: str | None = None
    repo: str
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
    """Queued follow-up triggers that arrived while a task was locked."""

    __tablename__ = "task_events"

    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(foreign_key="tasks.id")
    event_type: str
    payload_json: str
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
