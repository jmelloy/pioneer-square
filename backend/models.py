from sqlalchemy import Column, ForeignKey, Integer, Text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Guild(Base):
    __tablename__ = "guilds"

    id = Column(Text, primary_key=True)
    created_at = Column(Text, nullable=False)
    name = Column(Text)
    github_user_id = Column(Text)
    primary_repo = Column(Text, nullable=True)


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Text, primary_key=True)
    guild_id = Column(Text, ForeignKey("guilds.id"), nullable=False)
    worker_id = Column(Text, ForeignKey("workers.id"))
    name = Column(Text, nullable=False)
    type = Column(Text, nullable=False, server_default="worker")
    state = Column(Text, nullable=False, server_default="idle")
    activity = Column(Text, nullable=True)
    joined_at = Column(Text, nullable=False)


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(Text, ForeignKey("guilds.id"), nullable=False)
    from_agent = Column(Text)
    to_agent = Column(Text)
    content = Column(Text, nullable=False)
    message_type = Column(Text, nullable=False)
    created_at = Column(Text, nullable=False)
    user_id = Column(
        Text, nullable=True
    )  # github_user_id of the sender; NULL for system/worker messages


class Worker(Base):
    __tablename__ = "workers"

    id = Column(Text, primary_key=True)
    guild_id = Column(Text, ForeignKey("guilds.id"), nullable=False)
    repos = Column(Text, nullable=False, server_default="[]")
    state = Column(Text, nullable=False, server_default="idle")
    created_at = Column(Text, nullable=False)


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Text, primary_key=True)
    worker_id = Column(Text, ForeignKey("workers.id"), nullable=False)
    guild_id = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    tool = Column(Text, nullable=False, server_default="claude")
    issue_number = Column(Integer)
    issue_repo = Column(Text)
    state = Column(Text, nullable=False, server_default="pending")
    branch = Column(Text)
    worktree_path = Column(Text)
    pr_url = Column(Text)
    created_at = Column(Text, nullable=False)
    finished_at = Column(Text)
    name = Column(Text)
    parent_task_id = Column(Text)
    phase = Column(Text, server_default="execute")


class GithubToken(Base):
    __tablename__ = "github_tokens"

    github_user_id = Column(Text, primary_key=True)
    github_username = Column(Text)
    access_token = Column(Text, nullable=False)
    token_type = Column(Text, nullable=False, server_default="bearer")
    scope = Column(Text)
    created_at = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)


class UserSession(Base):
    __tablename__ = "user_sessions"

    token = Column(Text, primary_key=True)
    github_user_id = Column(Text, ForeignKey("github_tokens.github_user_id"), nullable=False)
    created_at = Column(Text, nullable=False)


class TaskLog(Base):
    __tablename__ = "task_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Text, ForeignKey("tasks.id"), nullable=True)
    timestamp = Column(Text, nullable=False)
    line = Column(Text, nullable=False)
    worker_id = Column(Text)
    agent_id = Column(Text)
    data = Column(Text)  # JSON: full tool input/output for click-to-expand


class ClaudeCredentials(Base):
    __tablename__ = "claude_credentials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(Text, ForeignKey("guilds.id"), nullable=False, unique=True)
    credentials_blob = Column(Text, nullable=False)  # base64-encoded tar.gz of ~/.claude/
    updated_at = Column(Text, nullable=False)


class ForemanTurn(Base):
    __tablename__ = "foreman_turns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(Text, nullable=False)
    user_id = Column(Text, nullable=False)
    role = Column(Text, nullable=False)  # "user" | "assistant" | "system"
    content_json = Column(Text, nullable=False)  # JSON-serialized content blocks
    # 1 if this "user" turn carries tool_results (not human input); 0 otherwise
    is_tool_response = Column(Integer, nullable=False, server_default="0")
    # For tool_result turns: id of the assistant turn whose tool_use blocks this answers
    parent_id = Column(Integer, ForeignKey("foreman_turns.id"), nullable=True)
    created_at = Column(Text, nullable=False)
