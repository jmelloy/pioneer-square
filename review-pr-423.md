# Review: PR #423 — Migrate to SQLModel + rename guild_pk → guild_id

## Summary

PR #423 migrates all SQLAlchemy `DeclarativeBase`/`Column(...)` model definitions to
SQLModel `Field(...)` equivalents and renames the transitional `guild_pk` integer FK
column to `guild_id` across all nine child tables via Alembic migration
`20260520_000004`.

## Findings

### Issues

**1. `server_default` removed — fresh-install DB defaults differ from upgraded DBs**

Every `server_default="..."` in the old models is replaced with a Python-level
`default=...` in SQLModel. The migration only renames columns; it does not add
`DEFAULT` constraints. On an existing DB the old defaults survive from earlier
migrations. On a fresh `alembic upgrade head`, columns such as `Agent.state`,
`Task.state`, `Task.tool`, `Worker.repos`, `GuildMember.role`, etc. will have no
SQL-level DEFAULT. Raw SQL inserts that omit these columns will insert NULL rather
than the expected sentinel values.

Recommendation: add `sa_column_kwargs={"server_default": "..."}` in `Field(...)` for
critical columns, or add a follow-up migration reinstating DEFAULT constraints.

**2. `Task.phase` nullability widened without a migration**

The old `Column(Text, server_default="execute")` is replaced with
`phase: str | None = Field(default="execute")`. The Python type now allows None,
which was not the declared intent. Any caller that assumes `task.phase` is non-None
could encounter a regression when reading legacy NULL rows.

**3. Breaking REST API key change not documented**

The `GET /guilds/{id}/workers` response field changed from `guild_pk` to `guild_id`
(confirmed by the test assertion update in `test_worker_api.py`). Clients reading
`worker.guild_pk` will silently get `undefined`. This should be called out in the PR
description as a breaking API change.

### Minor

- `ctx.guild_pk` on `WSContext` still uses the old name while model fields are
  `guild_id`. Not a bug, but inconsistent. Follow-up rename suggested.
- Local variables `guild_pk` / `guild_pk_val` remain throughout the codebase.
  Functionally fine; cosmetically inconsistent with the new field name.
- `SQLModel.metadata` is process-global. If a future dependency also uses SQLModel,
  Alembic autogenerate would pick up its tables. Worth a comment in `alembic/env.py`.

## What's done well

- Complete rename coverage across all ORM sites, raw SQL queries, test helpers,
  foreman, ws_handlers, and routes.
- Migration is clean, atomic, and includes a correct downgrade path.
- `main.py` uses `.label("guild_slug")` to disambiguate `Worker.guild_id` (int FK)
  from `Guild.guild_id` (str slug) in multi-join queries — without this, log and
  broadcast calls would silently use an integer where a string is expected.
- `alembic/env.py` correctly switches to `SQLModel.metadata` and imports `models`
  for side effects with a clear comment.

## Verdict

Approve with recommendations. Core logic is correct and coverage is thorough.
The `server_default` issue is the most impactful for new installs.
