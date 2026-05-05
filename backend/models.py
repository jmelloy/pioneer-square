from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Column, ForeignKey, Index, Integer, Text, or_
from sqlmodel import Field, SQLModel


def live_tasks_filter(now: str | None = None):
    if now is None:
        now = datetime.now(UTC).isoformat()
    return or_(Task.deleted_at.is_(None), Task.deleted_at > now)


class Guild(SQLModel, table=True):
    __tablename__ = "guilds"

    id: str = Field(primary_key=True)
    created_at: str
    name: str | None = None
    github_user_id: str | None = None
    primary_repo: str | None = None


class Agent(SQLModel, table=True):
    __tablename__ = "agents"

    id: str = Field(primary_key=True)
    guild_id: str = Field(foreign_key="guilds.id")
    worker_id: str | None = Field(default=None, foreign_key="workers.id")
    name: str
    type: str = Field(sa_column=Column(Text, server_default="worker", nullable=False))
    state: str = Field(sa_column=Column(Text, server_default="idle", nullable=False))
    activity: str | None = None
    joined_at: str
    last_seen: str | None = None


class Worker(SQLModel, table=True):
    __tablename__ = "workers"

    id: str = Field(primary_key=True)
    guild_id: str = Field(foreign_key="guilds.id")
    repos: str = Field(sa_column=Column(Text, server_default="[]", nullable=False))
    state: str = Field(sa_column=Column(Text, server_default="idle", nullable=False))
    created_at: str
    last_seen: str | None = None
    user_id: str | None = Field(default=None, foreign_key="users.id")
    auth_token: str | None = None


class Task(SQLModel, table=True):
    __tablename__ = "tasks"

    id: str = Field(primary_key=True)
    worker_id: str = Field(foreign_key="workers.id")
    guild_id: str
    description: str
    tool: str = Field(sa_column=Column(Text, server_default="claude", nullable=False))
    issue_number: int | None = None
    issue_repo: str | None = None
    state: str = Field(sa_column=Column(Text, server_default="pending", nullable=False))
    branch: str | None = None
    worktree_path: str | None = None
    pr_url: str | None = None
    created_at: str
    finished_at: str | None = None
    name: str | None = None
    parent_task_id: str | None = None
    phase: str | None = Field(
        default=None, sa_column=Column(Text, server_default="execute", nullable=True)
    )
    deleted_at: str | None = None
    user_id: str | None = None


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: int | None = Field(default=None, primary_key=True)
    guild_id: str = Field(foreign_key="guilds.id")
    from_agent: str | None = None
    to_agent: str | None = None
    content: str
    message_type: str
    created_at: str
    user_id: str | None = None


class GithubToken(SQLModel, table=True):
    __tablename__ = "github_tokens"

    github_user_id: str = Field(primary_key=True)
    github_username: str | None = None
    access_token: str
    token_type: str = Field(sa_column=Column(Text, server_default="bearer", nullable=False))
    scope: str | None = None
    created_at: str
    updated_at: str


class UserSession(SQLModel, table=True):
    __tablename__ = "user_sessions"

    token: str = Field(primary_key=True)
    github_user_id: str = Field(foreign_key="github_tokens.github_user_id")
    created_at: str


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: str = Field(primary_key=True)
    github_id: str = Field(sa_column=Column(Text, nullable=False, unique=True))
    github_login: str | None = None
    email: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    created_at: str
    updated_at: str


class GuildMember(SQLModel, table=True):
    __tablename__ = "guild_members"

    guild_id: str = Field(primary_key=True, foreign_key="guilds.id")
    user_id: str = Field(primary_key=True, foreign_key="users.id")
    role: str = Field(sa_column=Column(Text, server_default="member", nullable=False))
    created_at: str


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    id: int | None = Field(default=None, primary_key=True)
    task_id: str | None = Field(default=None, foreign_key="tasks.id")
    timestamp: str
    line: str
    worker_id: str | None = None
    agent_id: str | None = None
    data: str | None = None


class ClaudeCredentials(SQLModel, table=True):
    __tablename__ = "claude_credentials"

    id: int | None = Field(default=None, primary_key=True)
    guild_id: str = Field(
        sa_column=Column(Text, ForeignKey("guilds.id"), nullable=False, unique=True)
    )
    credentials_blob: str
    updated_at: str


class ForemanTurn(SQLModel, table=True):
    __tablename__ = "foreman_turns"
    __table_args__ = (Index("ix_foreman_turns_guild_user", "guild_id", "user_id"),)

    id: int | None = Field(default=None, primary_key=True)
    guild_id: str
    user_id: str
    role: str
    content_json: str
    is_tool_response: int = Field(sa_column=Column(Integer, server_default="0", nullable=False))
    parent_id: int | None = Field(default=None, foreign_key="foreman_turns.id")
    created_at: str
