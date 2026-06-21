"""Foreman tool schema definitions shared by embedded and standalone runners.

FOREMAN_TOOLS is the single source of truth for the Claude tool JSON schema.
Both the embedded foreman (backend/foreman/tools.py) and standalone foreman
(foreman/pioneer_foreman/tools.py) import from here.
"""

FOREMAN_TOOLS = [
    {
        "name": "create_task",
        "description": (
            "Create a named foreman task. Call this before assigning worker tasks — it gives the "
            "work a human-readable name visible in the sidebar and returns a task_id to reference "
            "in assign_task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short human-readable name (≤60 chars), e.g. 'Implement OAuth login'.",
                },
                "description": {
                    "type": "string",
                    "description": "Full description of the work to be done.",
                },
                "phase": {
                    "type": "string",
                    "enum": ["plan", "execute", "review"],
                    "description": "Starting phase. Default: execute.",
                },
            },
            "required": ["name", "description"],
        },
    },
    {
        "name": "assign_task",
        "description": (
            "Queue a coding task for a worker agent. The worker creates a git worktree, "
            "runs the chosen coding agent on the description, and pushes its work. "
            "For execute-phase tasks the worker opens a PR; for plan-phase tasks, the "
            "Foreman should post findings as a comment on the linked GitHub issue instead "
            "— do NOT open a PR for a document, spec, or outline. "
            "Pass task_id (from create_task) to assign that existing task to a worker instead "
            "of creating a duplicate — this is the preferred flow. "
            "For review-phase tasks (phase='review'), include explicit instructions telling the "
            "worker to post findings via 'gh pr review' and NOT to commit or open a new PR — "
            "the worker runtime injects standard review guardrails automatically. "
            "For shallow/fallback reviews without dispatching a worker, use review_pr_internal "
            "or review_pr instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "worker_id": {
                    "type": "string",
                    "description": "Worker agent ID (e.g. w-abc123). Must be idle.",
                },
                "description": {
                    "type": "string",
                    "description": "Detailed, self-contained task description the coding agent receives.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID returned by create_task. When provided, assigns that "
                    "existing task to the worker instead of creating a new row.",
                },
                "name": {
                    "type": "string",
                    "description": "Short task name shown in the sidebar (≤60 chars).",
                },
                "tool": {
                    "type": "string",
                    "enum": ["claude", "codex", "pi"],
                    "description": "Coding agent to use. Default: claude.",
                },
                "model": {
                    "type": "string",
                    "description": "Model override for the chosen tool (e.g. 'claude-opus-4-8', 'o4-mini', 'gpt-4o'). Omit to use the worker's configured default.",
                },
                "provider": {
                    "type": "string",
                    "description": "Provider override for pi tasks (e.g. 'anthropic', 'openai', 'google'). Ignored for claude and codex.",
                },
                "parent_task_id": {
                    "type": "string",
                    "description": (
                        "Foreman task ID of the parent work item this sub-task belongs to. "
                        "Set this when the review or sub-task is spawned in the context of an existing "
                        "piece of work (e.g. assigning a review task for a PR that was opened by a "
                        "parent execute task). Populates the DB hierarchy so the relationship is "
                        "visible in the sidebar. Ignored when task_id is provided."
                    ),
                },
                "phase": {
                    "type": "string",
                    "enum": ["plan", "execute", "review", "followup"],
                    "description": "Phase of work.",
                },
                "issue_number": {
                    "type": "integer",
                    "description": "GitHub issue to close (optional).",
                },
                "issue_repo": {
                    "type": "string",
                    "description": "owner/repo for the issue (optional).",
                },
                "repos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Explicit list of owner/repo strings to clone for this task. "
                        "When provided, the worker clones only these repos instead of "
                        "falling back to its full configured list. Omit when the full "
                        "list is appropriate."
                    ),
                },
            },
            "required": ["worker_id", "description"],
        },
    },
    {
        "name": "send_followup",
        "description": (
            "Continue work on an existing task's branch. The worker executes the "
            "follow-up instructions on the same branch (and same worktree if the "
            "original worker is still idle and within the 24h cleanup window) — "
            "ideal for CI fixes, lint, test fixes, doc additions, or reviewer "
            "comments. Worker selection: by default the original worker is "
            "preferred when idle (free worktree reuse); when it's busy or "
            "offline, any idle worker in the guild picks up the branch from "
            "GitHub. Pass preferred_worker_id to force a specific worker. "
            "Call this in response to task-complete, task-followup-done, "
            "user comments on a parked PR task, or CI failures."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID (t-xxxxxx)."},
                "instructions": {
                    "type": "string",
                    "description": "Follow-up instructions to execute on the existing branch.",
                },
                "preferred_worker_id": {
                    "type": "string",
                    "description": (
                        "Optional: prefer this worker if idle. Defaults to the "
                        "task's current worker_id."
                    ),
                },
            },
            "required": ["task_id", "instructions"],
        },
    },
    {
        "name": "finalize_task",
        "description": (
            "Close a task with no further follow-up needed. "
            "Call after reviewing a completed or errored task when no additional work is required. "
            "Use outcome='failed' when the task did not succeed (push errors, agent errors, "
            "abandoned work). Tasks are soft-deleted after their expiry window so the table "
            "doesn't accumulate cruft. Pick the window by task type:\n"
            "  - Ephemeral tasks (periodic-check, status-poll, automated health "
            "checks): expires_in_seconds = 1200 (20 minutes)\n"
            "  - Code tasks (execute / review / followup phases): omit the field "
            "to use the default 3 days, or pass expires_in_seconds = 259200\n"
            "  - Error / failed tasks: expires_in_seconds = 86400 (1 day)\n"
            "Pass deleted_at instead if you need an exact ISO-8601 timestamp.\n"
            "NOTE: For tasks that have an open PR, the GitHub webhook *may* deliver a "
            "'PR merged' event — but do not rely on it firing reliably. Always call "
            "get_pr_status to confirm the merged state before calling finalize_task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to finalize."},
                "outcome": {
                    "type": "string",
                    "enum": ["done", "failed"],
                    "description": (
                        "Final state to set on the task. "
                        "'done' (default) for successful completion; "
                        "'failed' for tasks that errored, hit push failures, or were abandoned."
                    ),
                },
                "expires_in_seconds": {
                    "type": "integer",
                    "description": (
                        "Seconds from now until the task is soft-deleted. "
                        "Defaults to 259200 (3 days) when omitted."
                    ),
                },
                "deleted_at": {
                    "type": "string",
                    "description": (
                        "ISO-8601 UTC timestamp at which the task is soft-deleted. "
                        "Takes precedence over expires_in_seconds when both are set."
                    ),
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "message_worker",
        "description": "Send a message to a worker's terminal — for mid-task context injection.",
        "input_schema": {
            "type": "object",
            "properties": {
                "worker_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["worker_id", "message"],
        },
    },
    {
        "name": "redirect_task",
        "description": (
            "Redirect a running task mid-execution with new instructions. "
            "Terminates the current Claude subprocess, then immediately resumes it in the same "
            "session — Claude keeps full context of what it was doing and acts on the new "
            "instructions instead. For tasks awaiting review, acts as a follow-up. "
            "Use this to course-correct without losing progress."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID (t-xxxxxx) to redirect."},
                "instructions": {
                    "type": "string",
                    "description": "New instructions. Claude will see its full prior history.",
                },
            },
            "required": ["task_id", "instructions"],
        },
    },
    {
        "name": "cancel_task",
        "description": (
            "Cancel a running or pending task. Use when a task is going in the wrong direction, "
            "is stuck, or is no longer needed. The worker terminates its Claude subprocess "
            "immediately and releases the agent slot. Cannot cancel tasks that are already done or failed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID (t-xxxxxx) to cancel."},
                "reason": {"type": "string", "description": "Optional reason for cancellation."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "shutdown_worker",
        "description": (
            "Send a shutdown signal to a worker agent, causing it to gracefully stop. "
            "Idle agents exit immediately; busy agents finish their current task and skip "
            "the follow-up window. The worker process disconnects and transitions to offline. "
            "Use when a worker is misbehaving, the operator is winding down, or a host needs "
            "to be freed up — prefer cancel_task for stopping a single bad task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "worker_id": {
                    "type": "string",
                    "description": "Worker agent ID (e.g. w-abc123).",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason for shutdown.",
                },
            },
            "required": ["worker_id"],
        },
    },
    {
        "name": "list_github_issues",
        "description": (
            "List GitHub issues for a repo. Use this to discover work that needs to be done."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo, e.g. 'acme/backend'"},
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Default: open",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max issues to return (default 20, max 50)",
                },
            },
            "required": ["repo"],
        },
    },
    {
        "name": "get_github_issue",
        "description": "Get full details of a single GitHub issue including its body and comments.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "issue_number": {"type": "integer"},
            },
            "required": ["repo", "issue_number"],
        },
    },
    {
        "name": "list_github_prs",
        "description": "List pull requests for a repo — useful for reviewing completed worker branches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Default: open",
                },
            },
            "required": ["repo"],
        },
    },
    {
        "name": "claim_github_issue",
        "description": (
            "Assign a GitHub issue to the authenticated user (claim it for this guild's operator). "
            "Call this when picking up an issue to work on, before assigning a worker task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "issue_number": {"type": "integer"},
            },
            "required": ["repo", "issue_number"],
        },
    },
    {
        "name": "get_task_status",
        "description": (
            "Get the current status of a task: state, phase, assigned worker and active agent state, "
            "and the last log lines. Use this to verify a task is progressing and to diagnose stalls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID (t-xxxxxx)."},
                "log_lines": {
                    "type": "integer",
                    "description": "Number of recent log lines to return (default 10, max 50).",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "create_github_issue",
        "description": (
            "Create a new GitHub issue to track work before assigning it to a worker. "
            "Search first with search_github_issues to avoid duplicates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "title": {"type": "string", "description": "Issue title."},
                "body": {"type": "string", "description": "Issue body in markdown."},
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Label names to apply (labels must already exist in the repo).",
                },
            },
            "required": ["repo", "title", "body"],
        },
    },
    {
        "name": "get_pr_status",
        "description": (
            "Fetch the live status of a single pull request: merged/closed/open state, "
            "submitted reviews, and the latest check-run conclusions for the head SHA. "
            "Use this to interpret a `[github-event]` message that doesn't carry enough "
            "context, or when deciding whether all required checks have completed before "
            "finalizing a task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "pr_number": {"type": "integer", "description": "Pull request number."},
            },
            "required": ["repo", "pr_number"],
        },
    },
    {
        "name": "search_github_issues",
        "description": (
            "Search GitHub issues and PRs by keyword within a repo. "
            "Call this before create_github_issue to check whether an issue already exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo to restrict search to."},
                "query": {"type": "string", "description": "Search keywords."},
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Filter by state. Default: open.",
                },
            },
            "required": ["repo", "query"],
        },
    },
    {
        "name": "review_pr",
        "description": (
            "Request a shallow automated code review via the EXTERNAL code-review-agent MCP server "
            "at agent.meyers.life (override with REVIEWER_AGENT_URL env var). "
            "The remote agent fetches the PR diff, runs a Claude-powered review, and posts "
            "the result as a GitHub PR review (APPROVE / REQUEST_CHANGES / COMMENT with "
            "inline comments). Findings are posted as review comments on the original PR, never "
            "as a new PR. "
            "Use this for a quick external review when no worker is available, or as a fallback. "
            "For a full review that checks out the branch and runs tests/lint, use "
            "create_task(phase='review') + assign_task instead. "
            "For a self-contained internal review without any external dependency, "
            "use review_pr_internal instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pr_url": {
                    "type": "string",
                    "description": (
                        "Full GitHub PR URL, e.g. https://github.com/owner/repo/pull/123"
                    ),
                },
            },
            "required": ["pr_url"],
        },
    },
    {
        "name": "review_pr_internal",
        "description": (
            "Perform a shallow internal code review of a GitHub pull request without calling "
            "any external service or dispatching a worker. "
            "Fetches the PR diff directly from the GitHub API, uses the Foreman AI to "
            "analyse it, then posts a GitHub PR review with a 3–5 bullet-point summary "
            "and up to 5 inline comments on specific lines. "
            "Supports action values APPROVE, REQUEST_CHANGES, or COMMENT (default COMMENT). "
            "Findings are posted as review comments on the original PR via the GitHub Reviews "
            "API, never as a new PR. "
            "Use this for a quick diff-only review when no worker is available, or when "
            "agent.meyers.life is unavailable. For a full review that checks out the branch "
            "and runs tests/lint, use create_task(phase='review') + assign_task instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pr_url": {
                    "type": "string",
                    "description": "Full GitHub PR URL, e.g. https://github.com/owner/repo/pull/123",
                },
                "action": {
                    "type": "string",
                    "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"],
                    "description": (
                        "Review verdict to submit to GitHub. "
                        "APPROVE — looks good; REQUEST_CHANGES — must fix before merge; "
                        "COMMENT — neutral feedback (default)."
                    ),
                },
            },
            "required": ["pr_url"],
        },
    },
    {
        "name": "dnsid",
        "description": (
            "Run a DNSid operation via the dnsid-py library. "
            "Three commands: "
            "'resolve' — look up an FQDN's _dnsid TXT record and JWKS (param: fqdn); "
            "'sign' — sign a JWT with the agent's configured Ed25519 identity (param: claims object); "
            "'verify' — verify a JWT against its DNSid record (params: jwt, expected_aud, optional expected_nonce). "
            "Returns a JSON result on success; raises on failure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["resolve", "sign", "verify"],
                    "description": "Subcommand to run.",
                },
                "fqdn": {
                    "type": "string",
                    "description": "resolve: FQDN to look up, e.g. 'example.com'.",
                },
                "claims": {
                    "type": "object",
                    "description": "sign: JSON object of JWT claims (iss, sub, exp, nonce, …).",
                },
                "jwt": {
                    "type": "string",
                    "description": "verify: compact JWT string to verify.",
                },
                "expected_aud": {
                    "type": "string",
                    "description": "verify: required — aud claim must contain this value.",
                },
                "expected_nonce": {
                    "type": "string",
                    "description": "verify: optional — nonce claim must equal this value.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "call_agent",
        "description": (
            "Call a skill on a remote A2A-compatible HTTP agent. "
            "Fetches the agent card from {agent_url}/.well-known/agent.json to discover "
            "available skills, then dispatches the requested skill with the given params. "
            "Returns the raw JSON result from the agent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_url": {
                    "type": "string",
                    "description": (
                        "Base URL of the target A2A agent, "
                        "e.g. https://agent.example.com or http://localhost:8080"
                    ),
                },
                "skill": {
                    "type": "string",
                    "description": (
                        "Skill id to invoke, e.g. 'verify_identity_record' or 'lookup_dnsid'. "
                        "Must match a skill id listed in the agent card."
                    ),
                },
                "params": {
                    "type": "object",
                    "description": "Parameters to pass to the skill as a JSON object.",
                },
            },
            "required": ["agent_url", "skill"],
        },
    },
    # spawn_worker intentionally disabled — see issues #551, #564, #566, #567
]

# Tools excluded from per-task child contexts. Child contexts manage a single,
# already-assigned task; creating or assigning new tasks remains the parent
# foreman's responsibility. See docs/foreman-per-task-context.md.
_CHILD_EXCLUDED_TOOLS = frozenset({"create_task", "assign_task"})

# Tool set handed to a per-task child context (FOREMAN_TOOLS minus the
# create/assign pair). Same JSON schemas, narrowed scope.
CHILD_FOREMAN_TOOLS = [t for t in FOREMAN_TOOLS if t["name"] not in _CHILD_EXCLUDED_TOOLS]
