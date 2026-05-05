# SQLModel Migration Plan — Backend & Foreman

**Date:** 2026-05-04  
**Scope:** `backend/` and `backend/foreman/` only  
**Goal:** Replace the separate SQLAlchemy `DeclarativeBase` table definitions and inline Pydantic `BaseModel` request schemas with a single set of `SQLModel` classes that serve as both the ORM mapping and the Pydantic serialisation layer.

---

## 1. Current State

### ORM layer — `backend/models.py`

Twelve classes inherit from a `DeclarativeBase`-derived `Base`. Every column is expressed with `Column(Text, …)` / `Column(Integer, …)` and explicit `ForeignKey` objects. The module also exports `live_tasks_filter()`, which builds an `or_()` expression against `Task.deleted_at`.

| Class | Table | PK type |
|---|---|---|
| `Guild` | `guilds` | `Text` |
| `Agent` | `agents` | `Text` |
| `Worker` | `workers` | `Text` |
| `Task` | `tasks` | `Text` |
| `Message` | `messages` | `Integer` autoincrement |
| `TaskLog` | `task_logs` | `Integer` autoincrement |
| `GithubToken` | `github_tokens` | `Text` |
| `UserSession` | `user_sessions` | `Text` |
| `User` | `users` | `Text` |
| `GuildMember` | `guild_members` | composite `Text`×`Text` |
| `ClaudeCredentials` | `claude_credentials` | `Integer` autoincrement |
| `ForemanTurn` | `foreman_turns` | `Integer` autoincrement |

### Request-schema layer — inline in routes and `agent_runner.py`

Fourteen `pydantic.BaseModel` subclasses are defined inline in the files that use them. None of them carry `response_model=` annotations on the endpoints; responses are serialised via the `row_to_dict()` helper or ad-hoc dicts.

| Class | File |
|---|---|
| `GuildCreate`, `GuildUpdate`, `MemberCreate`, `MemberUpdate` | `routes/guilds.py` |
| `FollowupCreate`, `FinalizeBody`, `RedirectCreate` | `routes/tasks.py` |
| `WorkerCreate`, `SpawnWorkerRequest`, `TaskCreate`, `WorkerMessage` | `routes/workers.py` |
| `CodeExchangeRequest`, `ClaudeCredentialsRequest` | `routes/auth.py` |
| `RunAgentRequest` | `agent_runner.py` |

### Foreman package — `backend/foreman/`

- `runner.py` and `tools.py` import ORM classes directly from `models` (no duplicate schemas).
- `state.py` is a stub kept for import compatibility.
- No Pydantic models inside `foreman/`.

---

## 2. What Changes and Why

The migration has two independent parts that happen to share the same tooling choice (`sqlmodel`):

**Part A — ORM layer:** swap `DeclarativeBase`+`Column()` for `SQLModel` table classes. Every model immediately becomes a Pydantic model too, enabling `model.model_dump()` and typed construction in tests without writing a separate schema class.

**Part B — Request schemas:** swap `pydantic.BaseModel` for `sqlmodel.SQLModel` (no `table=True`). Behaviorally identical; the benefit is a uniform import (`from sqlmodel import SQLModel, Field`) across the whole codebase.

---

## 3. File-by-file Changes

### `backend/models.py` — **full rewrite**

Replace the `Base = DeclarativeBase()` machinery with `SQLModel` table classes. The `live_tasks_filter()` function is unchanged; `Task.deleted_at` is still an `InstrumentedAttribute` and `or_()` works identically.

Remove:
```python
from sqlalchemy import Column, ForeignKey, Integer, Text, or_
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass
```

Add:
```python
from sqlalchemy import Column, ForeignKey, Index, Integer, Text, UniqueConstraint, or_
from sqlmodel import Field, SQLModel
```

`sqlalchemy` imports are still needed for `sa_column=Column(…)` on fields that require `server_default` or non-trivial constraints (see section 4).

### `backend/alembic/env.py` — **one-line change**

```python
# Before
from models import Base
target_metadata = Base.metadata

# After
from sqlmodel import SQLModel
target_metadata = SQLModel.metadata
```

SQLModel registers all `table=True` classes into `SQLModel.metadata`; Alembic must point at that registry.

### `backend/routes/guilds.py`, `tasks.py`, `workers.py`, `auth.py` and `backend/agent_runner.py` — **mechanical swap**

In each file:
```python
# Before
from pydantic import BaseModel
class Foo(BaseModel):
    …

# After
from sqlmodel import SQLModel
class Foo(SQLModel):
    …
```

`SQLModel` (without `table=True`) is a pure Pydantic model; behaviour is identical. No field definitions change.

### `backend/utils.py` — **no change required**

`row_to_dict()` currently does:
```python
{c.name: getattr(obj, c.name) for c in obj.__table__.columns}
```
SQLModel `table=True` classes are fully-fledged SQLAlchemy ORM classes and have `__table__`. This call continues to work. Optionally, it could be simplified to `obj.model_dump()` since SQLModel instances are also Pydantic models, but that is a separate clean-up and not required for correctness.

### `backend/foreman/` — **no change**

`runner.py` and `tools.py` import from `models` using the same public names (`Task`, `Worker`, `Agent`, `ForemanTurn`, …). Because the class names and field names are preserved, these files are unaffected.

### `backend/requirements.txt` — **add one line**

```
sqlmodel
```

Keep the existing `sqlalchemy[asyncio]` line — the `[asyncio]` extra installs `greenlet`, which the async engine needs. SQLModel's own SQLAlchemy dependency does not pull in `[asyncio]`.

---

## 4. Proposed SQLModel Class Definitions

### Conventions

- Columns that have a `server_default` in the current schema **must** use `sa_column=Column(…, server_default="…")` so Alembic autogenerate sees identical metadata and does not flag spurious drift.
- When `sa_column=` is used, all column properties (nullable, unique, server_default) live inside the `Column(…)` argument; other `Field()` kwargs are ignored.
- Integer autoincrement PKs follow the SQLModel idiom: `id: int | None = Field(default=None, primary_key=True)`. The value is `None` before insertion and populated by the DB.
- Nullable text columns are `str | None = Field(default=None)` (or just `= None`).

---

```python
# backend/models.py  (proposed)

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
    # unique=True via sa_column because combining foreign_key + unique in Field() is unreliable
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
    data: str | None = None  # JSON: tool input/output for click-to-expand


class ClaudeCredentials(SQLModel, table=True):
    __tablename__ = "claude_credentials"

    id: int | None = Field(default=None, primary_key=True)
    # unique=True must live inside sa_column because it combines with a FK
    guild_id: str = Field(
        sa_column=Column(Text, ForeignKey("guilds.id"), nullable=False, unique=True)
    )
    credentials_blob: str
    updated_at: str


class ForemanTurn(SQLModel, table=True):
    __tablename__ = "foreman_turns"
    # Declare the compound index that the migration creates but the current ORM
    # does not model. Adding it here eliminates a pre-existing autogenerate gap.
    __table_args__ = (Index("ix_foreman_turns_guild_user", "guild_id", "user_id"),)

    id: int | None = Field(default=None, primary_key=True)
    guild_id: str
    user_id: str
    role: str  # "user" | "assistant" | "system"
    content_json: str  # JSON-serialised content blocks
    is_tool_response: int = Field(
        sa_column=Column(Integer, server_default="0", nullable=False)
    )
    parent_id: int | None = Field(default=None, foreign_key="foreman_turns.id")
    created_at: str
```

---

## 5. Alembic Implications

### Metadata pointer

`alembic/env.py` must import `SQLModel.metadata` instead of `Base.metadata` (see section 3). This is the only required change.

### Schema drift

The DB schema was created and evolved entirely by the 15 existing Alembic migrations. This migration changes only the ORM/Python layer; no columns are added, removed, or altered. However, Alembic autogenerate compares ORM metadata against the live DB, so you must verify parity after the code change:

```bash
cd backend
alembic check          # exits non-zero if it detects any drift
# or, to inspect the generated SQL:
alembic revision --autogenerate -m "post-sqlmodel-check" --dry-run
```

Expected findings and mitigations:

| Potential drift | Cause | Fix |
|---|---|---|
| `server_default` removed | Field mapped without `sa_column` | Use `sa_column=Column(…, server_default="…")` as shown above |
| `ix_foreman_turns_guild_user` dropped | Index not modelled in current ORM | Add `__table_args__` to `ForemanTurn` as shown above |
| `nullable` mismatch on autoincrement PKs | SQLModel `int \| None` pattern | Benign for SQLite; Alembic SQLite batch-mode ignores it |

If any unexpected drift remains after applying the plan, write a blank fixup migration (body: `pass`) so Alembic's version table advances without touching the schema:

```python
def upgrade() -> None:
    pass

def downgrade() -> None:
    pass
```

### No new data migration needed

This is a pure ORM-layer swap. No `ALTER TABLE`, no data backfill, no schema revision is required.

---

## 6. Dependency Change

**File:** `backend/requirements.txt`

Add:
```
sqlmodel
```

Keep:
```
sqlalchemy[asyncio]   # greenlet for async engine; SQLModel's dep doesn't include this extra
aiosqlite             # async SQLite driver; unchanged
```

SQLModel currently supports SQLAlchemy 2.x. The existing unpinned `sqlalchemy[asyncio]` line resolves to the same major version, so no conflict is expected. If a conflict arises, pin both:
```
sqlmodel>=0.0.21
sqlalchemy[asyncio]>=2.0.14,<3
```

---

## 7. Risks and Edge Cases

### 7.1 `sa_column` exclusivity

When `sa_column=Column(…)` is passed to `Field()`, SQLModel ignores all other `Field()` keyword arguments (`default`, `nullable`, etc.) for that column — everything must be expressed inside the `Column(…)`. Mixing both leads to silent misconfiguration. The definitions in section 4 avoid this by putting all constraints inside `Column(…)` whenever `sa_column` is used.

### 7.2 `ClaudeCredentials.guild_id` unique + FK

SQLModel's `Field(foreign_key="…", unique=True)` does not reliably emit a `UNIQUE` constraint alongside the FK in all SQLModel versions. The `sa_column=Column(Text, ForeignKey("guilds.id"), nullable=False, unique=True)` form used above is the safe path; it imports `ForeignKey` from `sqlalchemy` directly.

### 7.3 SQLModel metadata isolation

SQLModel registers classes into its own metadata object (`SQLModel.metadata`), separate from any `DeclarativeBase.metadata`. If any third-party library or test fixture still references the old `Base.metadata`, it will see an empty registry after the migration. Search for `Base.metadata` beyond `alembic/env.py` before cutting over.

### 7.4 `FinalizeBody` exported by tests

`tests/test_soft_delete_tasks.py` imports `FinalizeBody` and `_resolve_finalize_deleted_at` directly from `routes.tasks`. Changing the base class from `pydantic.BaseModel` to `sqlmodel.SQLModel` is backward-compatible at the Pydantic API level (`.model_validate()`, `.model_dump()`, field access, etc.). No test changes required.

### 7.5 SQLModel version stability

SQLModel has historically made breaking changes in minor releases. Pin the version in `requirements.txt` once a compatible release is confirmed (`sqlmodel>=X.Y,<X.Z`), and add it to any lock file / Docker image.

### 7.6 `row_to_dict` and `__table__`

`row_to_dict` accesses `obj.__table__.columns`. SQLModel `table=True` classes are registered with SQLAlchemy's mapper so `__table__` exists. This is safe. No change needed, but the function could be simplified post-migration to `obj.model_dump()` as a clean-up.

### 7.7 Pre-existing autogenerate gap for `ix_foreman_turns_guild_user`

The compound index `ix_foreman_turns_guild_user` is created by the `d2e3f4a5b6c7` migration but is not modelled in the current `ForemanTurn` class. Alembic in the current codebase would try to drop it on autogenerate. This plan closes that gap by adding `__table_args__` to `ForemanTurn`. This is a net improvement, but it means the first `alembic check` after migration may still flag this index until `__table_args__` is in place.

---

## 8. Migration Sequence

1. Add `sqlmodel` to `backend/requirements.txt` and install (`pip install -r requirements.txt`).
2. Rewrite `backend/models.py` per section 4.
3. Update `backend/alembic/env.py`: swap `Base.metadata` → `SQLModel.metadata`.
4. In each route file and `agent_runner.py`, replace `from pydantic import BaseModel` / `class Foo(BaseModel)` with `from sqlmodel import SQLModel` / `class Foo(SQLModel)`.
5. Run `alembic check` (or dry-run autogenerate). Resolve any detected drift as described in section 5.
6. Run the full test suite (`python -m pytest` from `backend/`). All 119 tests should pass without modification.
7. Smoke-test the running server: worker registration, task assignment, foreman tool calls, WebSocket fan-out.
