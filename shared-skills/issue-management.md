---
name: issue-management
description: Foreman issue-management playbook — triage, execution (claim → create_task → assign_task → monitor → followup), and PR review, with decision trees and the real foreman tool names.
---

# Issue Management (Foreman Patterns)

> Extracted from the foreman's documented behaviour: `backend/foreman/prompt.py` (system prompt)
> and `backend/foreman/tools_schema.py` (tool schemas, the single source of truth). Tool
> *arguments* below are abridged — check `tools_schema.py` for the full schema. If the prompt
> changes, update this doc.

## Tool cheat-sheet

| Stage | Tool | Purpose |
|-------|------|---------|
| Triage | `list_github_issues`, `get_github_issue`, `search_github_issues`, `create_github_issue`, `list_github_prs` | Find, read, dedupe, file issues. `get_github_issue` returns `sub_issues` (non-empty ⇒ epic) |
| Claim | `claim_github_issue {repo, issue_number}` | Assign the issue to the guild operator |
| Dispatch | `create_task {name, description, phase, issue_number, issue_repo, pr_number, pr_repo}` → `assign_task {worker_id, task_id, description, tier, parent_task_id, ...}` | Name the job, then hand it to a worker |
| Capacity | `spawn_worker`, `shutdown_worker` | Add / remove worker processes |
| Monitor | `get_task_status {task_id, log_lines}`, `get_pr_status` | State, active agent, recent logs; PR reviews + checks + merged |
| Steer | `message_worker`, `redirect_task`, `cancel_task` | Context, course-correct (resumes with full history), stop |
| Iterate | `send_followup {task_id, instructions, tier, ...}` | More work on the same branch |
| Close | `finalize_task {task_id, outcome: done\|failed}` | End the task |
| PRs | `create_pr`, `review_pr_internal {pr_url, action?}` | Open a PR without a worker; quick diff review |

Task lifecycle: `pending → working → awaiting-review → (follow-up loop) → done | failed`.

> ⚠️ **Safety-critical invariants**
> - `create_task` and `assign_task` are **one atomic pair** — call `assign_task` immediately with the returned `task_id`.
> - **Always** pass `issue_number`+`issue_repo` (or `pr_number`+`pr_repo`) on `create_task`. Unlinked tasks render as "Ungrouped" and lose Discord thread routing.
> - Only assign to workers with an **idle agent slot**. Workers keep no backlog.
> - Never `finalize_task` a PR task on the strength of a webhook alone — confirm with `get_pr_status`.
> - Never assign an **epic** to a worker.

---

## 1. Triage

### 1.1 Categorise: bug / feature / refactor

The foreman has no dedicated classifier tool; it reads the issue (`get_github_issue`) and decides.
Use these heuristics:

| Signal | Category | Typical handling |
|--------|----------|------------------|
| Error/regression, "expected vs actual", stack trace, repro steps | **bug** | `phase=execute`; ask for a failing test first |
| New capability, "add/support X", user-facing behaviour | **feature** | Consider `phase=plan` first if the spec is vague |
| "Clean up", "extract", "rename", no behaviour change | **refactor** | `phase=execute`; keep scope tight, demand unchanged tests |
| Has `sub_issues` | **epic** | Never assign directly — see 1.4 |
| Spec/outline requested, no code | **plan** | `phase=plan`; result posted as an issue comment, **not a PR** |

### 1.2 Assess priority

Pioneer Square has no formal priority field; use these inputs and say so when you report:

1. **Readiness** — a `devReady`-family label (`devReady`, `dev-ready`, `ready-for-dev`, `ready`) means "pick up now". Unlabelled issues are not auto-picked up.
2. **Severity** — prod outage / data loss / security > broken workflow > cosmetic.
3. **Blocking** — does it block other tasks or an epic's next sub-issue?
4. **Cost** — pick the `tier` (`cheap`, `standard`, `powerful`) to match difficulty; don't spend `powerful` on a typo.

### 1.3 Check for duplicates

```text
search_github_issues {repo: "owner/repo", query: "<key terms from the report>"}
list_github_prs      {repo: "owner/repo", state: "open"}     # is a PR already open for it?
```

Then check the live `<state>` task list for a non-terminal task already referencing this issue number.

### 1.4 Triage decision tree

```text
New / newly-labelled issue
│
├─ Already assigned to someone else? ──────────── yes → SKIP
├─ A non-terminal task already references it? ─── yes → SKIP (or send_followup on it)
├─ A duplicate issue/PR already exists? ───────── yes → comment linking it; don't start work
├─ Has sub_issues (epic)? ─────────────────────── yes → pick ONE workable sub-issue
│        (state=open, no assignees, no non-terminal task); leave the epic open and
│        unclaimed; if none workable, do nothing this cycle
├─ Not devReady and human didn't ask for it? ──── yes → leave it; ask if unclear
├─ Spec too vague to implement? ───────────────── yes → create_task(phase="plan")
└─ otherwise → go to Execution
```

> Work that **continues an existing PR/branch** (CI fix, lint, test fix, review comments) is not a
> new issue. Skip issue creation entirely and use `send_followup` on the existing task.

### 1.5 Filing a new issue (only when none exists)

For new features, refactors, or multi-file changes: `search_github_issues` first, then
`create_github_issue`. Append the triggering message as a quote at the bottom of the body,
truncated to 500 chars:

```markdown
<issue body, unchanged>

---
> **Triggered by:** @username
> <original human message, ≤500 chars>
```

---

## 2. Execution: claim → create_task → assign_task → monitor → followup

### 2.1 Happy path

```text
1. get_github_issue        {repo, issue_number}            # read it; check sub_issues
2. claim_github_issue      {repo, issue_number}            # skip if already assigned
3. create_task             {name: "Fix login redirect", description: "...",
                            phase: "execute",
                            issue_number: 123, issue_repo: "owner/repo"}   → task_id
4. assign_task             {worker_id: "w-abc", task_id: <task_id>, description: "...",
                            tier: "standard",
                            issue_number: 123, issue_repo: "owner/repo"}
5. get_task_status         {task_id, log_lines: 10}        # verify it started and is logging
6. (worker sends task-complete → task parks in `awaiting-review`, worker goes idle)
7. get_pr_status / review  → send_followup {task_id, instructions, tier}  OR  finalize_task
```

Key details:

- Pass issue linkage to **both** `create_task` and `assign_task` so the worker's PR references
  the issue and the task groups under it.
- For sub-tasks or reviews spawned from existing work, pass `parent_task_id=<foreman_task_id>` to `assign_task`.
- Pick a worker whose `repos` cover the task. If none is online, `spawn_worker` (async; ~2 min to
  come online; auto-shuts down when idle). **Never spawn a duplicate** for repos an online or
  starting worker already covers.
- Use `phase="plan"` when you want a spec first; post the result as an **issue comment** — only
  open a PR when there is actual code.

### 2.2 Monitoring and steering

```text
get_task_status → state / phase / agent state / last log lines
```

| Observation | Action |
|-------------|--------|
| Fresh logs, `working` | Wait |
| Needs extra context | `message_worker {task_id, ...}` (forwarded to the active agent) |
| Going the wrong way, but salvageable | `redirect_task {task_id, instructions}` — SIGTERM + resume with full history |
| Wrong/obsolete task | `cancel_task {task_id, reason}` (not valid on done/failed tasks) |
| Worker wedged or unneeded | `shutdown_worker` (idle exits now; busy finishes current task) |
| `[worker-offline] reason=disconnect` | `get_task_status` on affected tasks; reassign via `send_followup`; escalate if no workers left |
| `[worker-offline] reason=shutdown` | Clean exit; only check tasks still in progress |
| `[worker-online]` | Assign any unassigned pending tasks the new worker's repos cover |

When you `redirect_task` or `send_followup`, also post a short comment on the linked issue —
the issue thread is the running progress log.

### 2.3 After `task-complete`: followup vs finalize

`awaiting-review` is the **normal home for an open PR**, not limbo. Default: leave PR-bearing
tasks open for human review.

```text
Task in awaiting-review
│
├─ PR merged? (confirm with get_pr_status, don't trust webhook) ─ yes → finalize_task(outcome="done")
├─ PR closed unmerged?
│     ├─ fixable → send_followup with the fix
│     └─ abandoning → finalize_task(outcome="failed")
├─ CI failed (check_run / check_suite conclusion=failure)? → send_followup with concrete fix (read summary line)
├─ Reviewer requested changes / commented?
│     ├─ contradicts the linked issue's intent → send_followup telling worker to DECLINE, citing issue #N
│     ├─ minor nit (style/naming) → worker may accept at its discretion
│     └─ legitimate → send_followup with the requested change
├─ Approved / CI green, not merged → no action; wait
├─ Ephemeral/automation task, or work abandoned → finalize_task
└─ Worker error / push failure with no recovery → finalize_task(outcome="failed")
```

`send_followup` worker selection is automatic: original worker if idle (cached worktree), else any
idle worker that pulls the branch from GitHub. Force one with `preferred_worker_id`. It requires
`tier` on every call and may switch `tool`/`provider` (e.g. escalate a stuck `claude` task to `codex`).

> ⚠️ Reviewer feedback is **not** automatically authoritative. Re-read the linked issue first.
> Instruct the worker to check each comment against the issue and decline contradictions
> assertively ("Keeping X as required by issue #NNN"), not apologetically.

### 2.4 Webhook / event routing quick table

| Event | Foreman action |
|-------|----------------|
| `issues/labeled` (devReady), `issues/opened|reopened` with devReady | Pickup flow (triage tree, then execute) |
| `pull_request/review_requested` | Create a review task (§3) unless one already exists for the PR |
| `pull_request/closed` merged=true | `get_pr_status` → `finalize_task` |
| `check_run` failure | `send_followup` |
| `[discord-thread-reply] task_id=…` task `working` | `redirect_task` with the reply text |
| `[discord-thread-reply] task_id=…` task parked/done | `send_followup` with the reply text |
| `[periodic-check]` shows `merged=True` | `finalize_task` now |

Never open a new task or PR in reaction to a `[discord-thread-reply]` unless the human explicitly asks for new, unrelated work.

---

## 3. Review

Every review gets a sidebar task: **`create_task` first, `finalize_task` after**, whichever path you use.

### 3.1 Choose the path

```text
PR needs review
│
├─ Worker available and PR non-trivial? → Full worker-driven review (primary path)
│     create_task(name="Review PR #N: <title>", phase="review", pr_number=N, pr_repo="o/r")
│     assign_task(worker_id, task_id, parent_task_id=<foreman_task_id>,
│                 pr_number=N, pr_repo="o/r", description=<instructions below>)
│     ... worker posts `gh pr review` ...  →  finalize_task(task_id)
│
└─ No worker / quick diff-only? → Shallow review
      create_task(... phase="review" ...)
      review_pr_internal {pr_url, action?}   # omit action to let the tool choose (biased to APPROVE)
      finalize_task(task_id)                 # success OR failure
```

`pr_number`/`pr_repo` are **required** on `assign_task` for reviews (pass the PR number, not the issue number). A review task already existing for the PR ⇒ skip creation.

Worker review description must say:

```text
Check out the PR branch: gh pr checkout <PR_NUMBER> --repo <OWNER/REPO>
Read all changed files and nearby existing code; compare the PR to the linked issue and epic.
Look for existing helpers that already solve this and for unnecessary complexity.
Post findings: gh pr review <PR_NUMBER> --repo <OWNER/REPO> [--approve | --request-changes | --comment] --body "..."
Do NOT commit any files. Do NOT open a new PR.
```

> ⚠️ **Review output goes to GitHub PR reviews, never to a new PR.** Reviewers must not commit or push.

### 3.2 Approve vs request changes vs comment

The review is an **architecture / product-fit** review, not a build check — CI owns compile/test.
Ask: *should this code exist at all?*

```text
Read PR title/body + linked issue/epic. Does the diff serve that scope?
│
├─ Unnecessary for the issue, materially over-engineered, duplicates existing behaviour,
│  introduces a conflicting pattern, or solves the wrong problem
│      → REQUEST_CHANGES   (cite the mismatch / duplicate helper / simpler existing path)
│
├─ Fits, but non-blocking concerns: scope, naming, maintainability, a simpler path worth considering
│      → COMMENT
│
└─ Necessary, fits issue + epic, uses existing patterns, no avoidable complexity
   (minor nits allowed)
       → APPROVE
```

| Situation | Verdict |
|-----------|---------|
| Style/naming/format nits only | APPROVE (or COMMENT) — **never** block on style |
| Failing tests/compile error only | Not a review matter; CI handles it → `send_followup` if on your own task |
| Duplicates a helper that already exists | REQUEST_CHANGES, name the helper |
| Slightly larger than needed, works fine | COMMENT |
| Wrong problem solved | REQUEST_CHANGES |

Note the two paths are tuned slightly differently: `review_pr_internal`'s own `action` doc reserves
REQUEST_CHANGES for genuine bugs, security issues, or logic errors, and biases to APPROVE; the
worker path follows the architecture policy above. Pass `action` explicitly to
`review_pr_internal` only to override its judgement.

Tone: direct and concise. "This code should not exist" is acceptable when true. Include the
review policy in every worker review description so the worker's verdict follows it.

### 3.3 Review checklist

1. Read PR title/body and linked issue/epic.
2. Search for an existing helper, route, store, model, prompt or worker path before accepting a new one.
3. Prefer deletion, reuse, or a smaller change.
4. Ignore style-only complaints and routine test failures unless they reveal needless design.

---

## Appendix: end-to-end decision summary

```text
Signal arrives (issue label / chat / CI / review / worker event)
│
├─ New ready issue ................ Triage tree → claim → create_task+assign_task → monitor
├─ Existing PR needs more work .... send_followup (never a new issue/PR)
├─ Review requested ............... create_task(phase=review) → review (worker or internal) → finalize
├─ Running task off-track ......... redirect_task → (cancel_task if hopeless)
├─ Worker gone .................... get_task_status → send_followup to another idle worker / spawn_worker
├─ PR merged (confirmed) .......... finalize_task(done)
└─ Genuinely stuck ................ Escalate to the human
```
