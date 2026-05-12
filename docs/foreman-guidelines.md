# Foreman Guidelines

Operational rules that shape how the Foreman AI creates and assigns tasks.

---

## Artifact repo selection (added 2026-05-12)

When a task produces a committed artifact — a design doc, spec, ADR, or any file
that must land in a repository — the foreman selects the target repo using this
priority order:

| Priority | Condition | Action |
|----------|-----------|--------|
| 1 | `issue_repo` is set **and** a worker covers it | Commit artifact to `issue_repo`; prefer that worker |
| 2 | `issue_repo` is set but **no worker covers it** | Fall back to guild default repo; worker adds a fallback note to the PR body |
| 3 | No `issue_repo` | Use guild default repo (unchanged behaviour) |

### Worker instruction requirement

Whenever a task involves committing an artifact, the foreman must include this line
in the task description it sends to the worker:

```
**Target repo for this artifact:** `{repo}` (derived from the linked issue / guild default)
```

This ensures the worker never has to guess which repo to commit to.

### Fallback note template

When falling back to the guild default because no worker covers `issue_repo`, the
worker should add this note to the PR description:

```
⚠️ This artifact belongs in `{issue_repo}` but no worker has access to that repo;
committed here as a fallback.
```

### Scope

This rule applies only to artifact-producing tasks. Pure code-change tasks (editing
files already in a worker's checkout) are unaffected.
