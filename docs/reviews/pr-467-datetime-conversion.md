# PR #467 Review: Convert date fields from str to datetime

**PR:** https://github.com/jmelloy/pioneer-square/pull/467
**Author:** jmelloy
**Reviewer:** Claude (automated)
**Date:** 2026-05-26

## Summary

PR converts 22 datetime columns across 12 tables from `Text` (storing ISO-8601 strings) to proper `DateTime(timezone=True)` (TIMESTAMPTZ in PostgreSQL). Includes an Alembic migration, updates to all 15 application files that write timestamps, and test helper updates.

## Verdict: Approve with minor fixes

The core approach is correct and complete. Two items should be addressed before merge.

---

## Required fixes

### 1. Silent parse failure in `ws_handlers.py:handle_task_update`

The `finishedAt` parsing block swallows errors silently:

```python
except (ValueError, TypeError):
    pass  # finishedAt silently dropped
```

If a worker sends a malformed timestamp, the field is silently skipped — no log, no error. A task will appear to never finish in the DB. Add a warning log at minimum:

```python
except (ValueError, TypeError) as exc:
    logger.warning("Ignoring invalid finishedAt %r: %s", raw, exc)
```

### 2. Inconsistent HTTP response serialization

Most routes explicitly call `.isoformat()` before including datetimes in response dicts. `routes/foreman.py:671` does not:

```python
return {"task_id": task_id, "created_at": created_at}  # created_at is datetime
```

FastAPI's `jsonable_encoder` handles this transparently, but the inconsistency signals ambiguity about intent. Pick one pattern for HTTP responses.

---

## Positive findings

- **DRY migration**: The `_COLUMNS` loop approach avoids 22 near-identical `alter_column` calls.
- **Correct `postgresql_using` cast**: Appropriate for this PostgreSQL-only project (confirmed by PL/pgSQL in `alembic/env.py`).
- **Layer separation**: Raw `datetime` for DB ops; explicit `.isoformat()` for WebSocket JSON payloads; `jsonable_encoder` for HTTP. The PR description explains this clearly and the code follows it.
- **`live_tasks_filter` correctness**: Comparing a `DateTime` column against a `datetime` object via SQLAlchemy is correct and avoids the silent string-sort vs. timestamp-sort bugs the old approach risked with ISO strings.
- **Error-case return from `_resolve_finalize_deleted_at`**: Changed from `("", error)` to `(None, error)` — semantically cleaner; tests updated consistently.
- **Full downgrade support**: The migration reverses in `reversed(_COLUMNS)` order, which is correct for FK-dependent tables.

---

## Minor notes

- Several informative architectural comments were removed from `models.py` alongside boilerplate ones. Worth restoring:
  - `ForemanTurn.is_tool_response` comment (int-as-bool pattern is non-obvious)
  - `TaskEvent` docstring (original explained the lock-serialization mechanic and re-trigger flow)
  - `Task.deleted_at` soft-delete semantic description
- `test_finalize_endpoint_default_sets_three_day_window` round-trips through `isoformat()`/`fromisoformat()` when it could now compare datetimes directly.
- The `_COLUMNS` list tuples (`table, column, nullable`) would be more self-documenting as named tuples for a list this long.
