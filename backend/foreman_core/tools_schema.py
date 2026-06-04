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
            "runs the chosen coding agent on the description, then pushes the branch and "
            "opens a PR. "
            "Pass task_id (from create_task) to assign that existing task to a worker instead "
            "of creating a duplicate — this is the preferred flow. "
            "WARNING: do NOT use assign_task to perform a PR code review. Workers always "
            "end by committing and opening a new PR — they cannot post GitHub PR review "
            "comments. For PR reviews use review_pr_internal or review_pr instead."
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
                    "description": "Foreman task ID this worker task belongs to (optional, ignored if task_id provided).",
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
            "Mark a task complete with no further follow-up needed. "
            "Call after reviewing a completed task when no additional work is required. "
            "Tasks are soft-deleted after their expiry window so the table doesn't "
            "accumulate cruft. Pick the window by task type:\n"
            "  - Ephemeral tasks (periodic-check, status-poll, automated health "
            "checks): expires_in_seconds = 1200 (20 minutes)\n"
            "  - Code tasks (execute / review / followup phases): omit the field "
            "to use the default 3 days, or pass expires_in_seconds = 259200\n"
            "  - Error / failed tasks: expires_in_seconds = 86400 (1 day)\n"
            "Pass deleted_at instead if you need an exact ISO-8601 timestamp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to finalize."},
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
            "Request an automated code review via the EXTERNAL code-review-agent MCP server "
            "at agent.meyers.life (override with REVIEWER_AGENT_URL env var). "
            "The remote agent fetches the PR diff, runs a Claude-powered review, and posts "
            "the result as a GitHub PR review (APPROVE / REQUEST_CHANGES / COMMENT with "
            "inline comments). This is the CORRECT way to review a PR — findings are posted "
            "as review comments on the original PR, never as a new PR. "
            "Use this when you want a specialised external reviewer. "
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
            "Perform an internal agent-driven code review of a GitHub pull request "
            "without calling any external service. "
            "Fetches the PR diff directly from the GitHub API, uses the Foreman AI to "
            "analyse it, then posts a GitHub PR review with a 3–5 bullet-point summary "
            "and up to 5 inline comments on specific lines. "
            "Supports action values APPROVE, REQUEST_CHANGES, or COMMENT (default COMMENT). "
            "This is the CORRECT way to review a PR — findings are posted as review "
            "comments on the original PR via the GitHub Reviews API, never as a new PR. "
            "Use this instead of review_pr when you want a quick review with no external "
            "dependency, or when agent.meyers.life is unavailable."
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
            "Run a DNSid operation via the local dnsid-sdk CLI. "
            "Three commands: "
            "'resolve' — look up an FQDN's _dnsid TXT record and JWKS (param: fqdn); "
            "'sign' — sign a JWT with the agent's configured Ed25519 identity (param: claims object); "
            "'verify' — verify a JWT against its DNSid record (params: jwt, expected_aud, optional expected_nonce). "
            "Returns the CLI's JSON output on success; raises on failure."
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
