import os

from sqlalchemy import Column, ForeignKey, Integer, Text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_PATH = os.environ.get("DB_PATH", "pioneer_square.db")
DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal: sessionmaker = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


class Guild(Base):
    __tablename__ = "guilds"

    id = Column(Text, primary_key=True)
    created_at = Column(Text, nullable=False)
    name = Column(Text)
    github_user_id = Column(Text)

    def as_dict(self):
        return {c: getattr(self, c) for c in self.__table__.columns.keys()}


class Worker(Base):
    __tablename__ = "workers"

    id = Column(Text, primary_key=True)
    guild_id = Column(Text, ForeignKey("guilds.id"), nullable=False)
    repos = Column(Text, nullable=False, default="[]", server_default="'[]'")
    state = Column(Text, nullable=False, default="idle", server_default="'idle'")
    created_at = Column(Text, nullable=False)

    def as_dict(self):
        return {c: getattr(self, c) for c in self.__table__.columns.keys()}


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Text, primary_key=True)
    guild_id = Column(Text, ForeignKey("guilds.id"), nullable=False)
    worker_id = Column(Text, ForeignKey("workers.id"))
    name = Column(Text, nullable=False)
    type = Column(Text, nullable=False, default="worker", server_default="'worker'")
    state = Column(Text, nullable=False, default="idle", server_default="'idle'")
    joined_at = Column(Text, nullable=False)

    def as_dict(self):
        return {c: getattr(self, c) for c in self.__table__.columns.keys()}


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(Text, ForeignKey("guilds.id"), nullable=False)
    from_agent = Column(Text)
    to_agent = Column(Text)
    content = Column(Text, nullable=False)
    message_type = Column(Text, nullable=False)
    created_at = Column(Text, nullable=False)

    def as_dict(self):
        return {c: getattr(self, c) for c in self.__table__.columns.keys()}


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Text, primary_key=True)
    worker_id = Column(Text, ForeignKey("workers.id"), nullable=False)
    guild_id = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    tool = Column(Text, nullable=False, default="claude", server_default="'claude'")
    issue_number = Column(Integer)
    issue_repo = Column(Text)
    state = Column(Text, nullable=False, default="pending", server_default="'pending'")
    branch = Column(Text)
    worktree_path = Column(Text)
    pr_url = Column(Text)
    created_at = Column(Text, nullable=False)
    finished_at = Column(Text)
    name = Column(Text)
    parent_task_id = Column(Text)
    phase = Column(Text, default="execute", server_default="'execute'")

    def as_dict(self):
        return {c: getattr(self, c) for c in self.__table__.columns.keys()}


class GithubToken(Base):
    __tablename__ = "github_tokens"

    github_user_id = Column(Text, primary_key=True)
    github_username = Column(Text)
    access_token = Column(Text, nullable=False)
    token_type = Column(Text, nullable=False, default="bearer", server_default="'bearer'")
    scope = Column(Text)
    created_at = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)

    def as_dict(self):
        return {c: getattr(self, c) for c in self.__table__.columns.keys()}


class UserSession(Base):
    __tablename__ = "user_sessions"

    token = Column(Text, primary_key=True)
    github_user_id = Column(
        Text, ForeignKey("github_tokens.github_user_id"), nullable=False
    )
    created_at = Column(Text, nullable=False)

    def as_dict(self):
        return {c: getattr(self, c) for c in self.__table__.columns.keys()}


class TaskLog(Base):
    __tablename__ = "task_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Text, ForeignKey("tasks.id"), nullable=False)
    timestamp = Column(Text, nullable=False)
    line = Column(Text, nullable=False)

    def as_dict(self):
        return {c: getattr(self, c) for c in self.__table__.columns.keys()}


async def get_db() -> AsyncSession:
    """Return a new AsyncSession. Caller is responsible for closing it."""
    return AsyncSessionLocal()
