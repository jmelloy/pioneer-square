"""Foreman tool schema definitions for the backend Foreman runner.

FOREMAN_TOOLS is the single source of truth for the Claude tool JSON schema.
The backend executes all Foreman tools; a standalone proxy only receives this
schema as part of an LLM API request so local/OpenAI-compatible providers can
produce tool calls in the same shape.
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
                "issue_number": {
                    "type": "integer",
                    "description": (
                        "GitHub issue this task belongs to. ALWAYS set this (with "
                        "issue_repo) when the work relates to an issue — it groups the "
                        "task under the issue in the sidebar and routes its Discord "
                        "notifications into the issue's thread."
                    ),
                },
                "issue_repo": {
                    "type": "string",
                    "description": "owner/repo for issue_number.",
                },
                "pr_number": {
                    "type": "integer",
                    "description": (
                        "GitHub PR this task targets (e.g. review tasks). Set with pr_repo "
                        "whenever a PR is known."
                    ),
                },
                "pr_repo": {
                    "type": "string",
                    "description": "owner/repo for pr_number.",
                },
            },
            "required": ["name", "description"],
        },
    },
    {
        "name": "assign_task",
        "description": (
            "Assign a coding task to an idle worker slot — the worker host process (w-xxx) spawns an agent subprocess to execute it. "
            "The worker creates a git worktree, runs the chosen coding agent on the description, "
            "and pushes its work, then parks in awaiting-foreman-review — it does NOT open a "
            "PR automatically. Once you've reviewed the pushed branch and decided it's ready, "
            "call send_followup(create_pr=true) to open the PR. For plan-phase tasks, post "
            "findings as a comment on the linked GitHub issue instead — do NOT open a PR for "
            "a document, spec, or outline. "
            "Pass task_id (from create_task) to assign that existing task to a worker instead "
            "of creating a duplicate — this is the preferred flow. "
            "For review-phase tasks (phase='review'), include explicit instructions telling the "
            "worker to post findings via 'gh pr review' and NOT to commit or open a new PR — "
            "the worker runtime injects standard review guardrails automatically. "
            "For shallow/fallback reviews without dispatching a worker, use review_pr_internal "
            "instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "worker_id": {
                    "type": "string",
                    "description": "Worker host-process ID (e.g. w-abc123). Must have an idle agent slot; workers do not queue backlog tasks.",
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
                "tier": {
                    "type": "string",
                    "enum": ["cheap", "standard", "powerful"],
                    "description": (
                        "REQUIRED. Model capability tier to dispatch this task at — the "
                        "foreman always picks the capability class; the backend resolves it "
                        "to the best available model in the worker's provider catalog. "
                        "Choose by the difficulty of the work itself, NOT by which tool "
                        "runs it (tier is agent-agnostic): 'cheap' for trivial/mechanical "
                        "work (issue triage, simple reviews, lint/doc fixes); 'standard' "
                        "for normal plan/execute work (the safe default when unsure); "
                        "'powerful' for gnarly or high-stakes work. The resolved tier is "
                        "recorded on the task."
                    ),
                },
                "provider": {
                    "type": "string",
                    "description": "Optional provider override for pi tasks only. Omit unless the user explicitly asked for a provider; Pi will otherwise use its normal local default model/provider selection.",
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
                "pr_number": {
                    "type": "integer",
                    "description": (
                        "The pull request number this task acts on. REQUIRED for "
                        "phase='review' — the worker checks out this PR's branch via "
                        "`gh pr view <pr_number>`. This is the PR number, NOT the "
                        "issue number."
                    ),
                },
                "pr_repo": {
                    "type": "string",
                    "description": "owner/repo of the PR (for phase='review'; defaults to issue_repo).",
                },
                "branch": {
                    "type": "string",
                    "description": (
                        "The PR's head ref (e.g. 'feature-branch'). Optional metadata for "
                        "phase='review' tasks — the worker re-resolves the actual head branch "
                        "from pr_number/pr_repo at checkout time, but recording it here lets "
                        "the task row and logs show which branch was under review."
                    ),
                },
                "pr_url": {
                    "type": "string",
                    "description": "Full URL of the pull request (e.g. from the webhook payload).",
                },
                "head_sha": {
                    "type": "string",
                    "description": "The PR head commit SHA at the time this task was created (informational, for logging).",
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
            "required": ["worker_id", "description", "tier"],
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
            "user comments on a parked PR task, or CI failures. "
            "When triggered by a reviewer comment, PR review, or PR issue comment, "
            "write instructions telling the worker to check the comment against the "
            "linked GitHub issue (the source of truth for intent) and decline — "
            "assertively, citing the issue number, not apologetically — anything that "
            "contradicts it; minor nits (style, naming) can still be accepted. "
            "Pass tier every call (required); optionally pass tool/provider to "
            "switch coding agent for the follow-up (e.g. escalate a stuck claude "
            "task to codex), or omit them to keep the task's current tool/provider."
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
                "tool": {
                    "type": "string",
                    "enum": ["claude", "codex", "pi"],
                    "description": (
                        "Optional: coding agent to use for this follow-up. Must be "
                        "supported by the dispatched worker. Defaults to the task's "
                        "current tool."
                    ),
                },
                "tier": {
                    "type": "string",
                    "enum": ["cheap", "standard", "powerful"],
                    "description": (
                        "REQUIRED. Model capability tier for this follow-up — 'cheap', "
                        "'standard', or 'powerful'. Pass the task's existing tier to keep "
                        "the same capability class, or change it to escalate a stuck task "
                        "(e.g. bump a repeatedly-failing follow-up to 'powerful') or "
                        "de-escalate a trivial one. Choose by the difficulty of the "
                        "follow-up itself, not by which tool runs it (tier is "
                        "agent-agnostic). The backend re-resolves this to a concrete model "
                        "from the worker's provider catalog on every follow-up."
                    ),
                },
                "provider": {
                    "type": "string",
                    "description": (
                        "Optional provider override for pi follow-ups only. Omit unless the user explicitly asked for a provider; Pi will otherwise use its normal local default model/provider selection. Ignored for claude and codex."
                    ),
                },
                "create_pr": {
                    "type": "boolean",
                    "description": (
                        "Optional, default false. PR creation is no longer automatic — a "
                        "worker only pushes its branch and parks the task in "
                        "awaiting-foreman-review. Pass true when this follow-up should open "
                        "a GitHub PR for the branch once it pushes (e.g. once you've decided "
                        "the work is ready for human/CI review). Once a PR exists, the task "
                        "moves to awaiting-review and its lifecycle is driven by the GitHub "
                        "webhook (merge/close) instead of the foreman."
                    ),
                },
            },
            "required": ["task_id", "instructions", "tier"],
        },
    },
    {
        "name": "finalize_task",
        "description": (
            "Close a task with no further follow-up needed. "
            "Call after reviewing a completed or errored task when no additional work is required. "
            "Use outcome='failed' when the task did not succeed (push errors, agent errors, "
            "abandoned work). Soft-delete is automatic: successful tasks tied to a still-open "
            "issue stay visible until the issue closes; everything else disappears from the "
            "board a few hours after finalizing. You do not choose the window.\n"
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
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "message_worker",
        "description": (
            "Send a message to a worker's terminal — reaches the agent subprocess running "
            "mid-task; has no effect if the worker is idle. Always pass task_id when you mean "
            "a specific task: a worker runs several tasks at once, and without it the message "
            "is only delivered when exactly one agent is running."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "worker_id": {"type": "string"},
                "message": {"type": "string"},
                "task_id": {
                    "type": "string",
                    "description": (
                        "Task (t-xxxxxx) whose agent should receive the message. Required in "
                        "practice whenever the worker may be running more than one task."
                    ),
                },
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
            "Send a shutdown signal to a worker host process (w-xxx), causing it to gracefully stop. "
            "The worker exits immediately if idle; if busy, it finishes the current agent task and skips "
            "the follow-up window. The worker process disconnects and transitions to offline. "
            "Use when a worker is misbehaving, the operator is winding down, or a host needs "
            "to be freed up — prefer cancel_task for stopping a single bad task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "worker_id": {
                    "type": "string",
                    "description": "Worker host-process ID (e.g. w-abc123).",
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
        "description": "Get full details of a single GitHub issue including its body, comments, and native sub-issues (child issues linked via GitHub's parenting feature — an epic's sub_issues array is non-empty).",
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
            "and the last log lines. Use this to verify a task is progressing and to diagnose stalls. "
            "Each log entry's `line` is a short summary; a `data` field with the full untruncated "
            "tool output is included when it differs from the summary."
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
        "name": "create_pr",
        "description": (
            "Open a GitHub pull request for a branch that has already been pushed — e.g. after "
            "a worker's send_followup with a create_pr flag, or after manual review confirms a "
            "branch is ready. This does not dispatch a worker or touch any worktree; it only "
            "calls the GitHub API to open the PR. The branch must already exist on the remote "
            "with commits ahead of base, or this fails — use get_pr_status or list_github_prs "
            "first if unsure whether a PR already exists for the branch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "branch": {
                    "type": "string",
                    "description": "Name of the already-pushed branch containing the changes (head).",
                },
                "title": {"type": "string", "description": "Pull request title."},
                "body": {"type": "string", "description": "Pull request description in markdown."},
                "base": {
                    "type": "string",
                    "description": "Target branch to merge into. Default: main.",
                },
            },
            "required": ["repo", "branch", "title", "body"],
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
        "name": "review_pr_internal",
        "description": (
            "Perform a shallow internal code review of a GitHub pull request without calling "
            "any external service or dispatching a worker. "
            "Fetches the PR diff directly from the GitHub API, uses the Foreman AI to "
            "analyse it, then posts a GitHub PR review with a 3–5 bullet-point summary "
            "and up to 5 inline comments on specific lines. "
            "Supports action values APPROVE, REQUEST_CHANGES, or COMMENT. If action is omitted, "
            "the tool analyses the diff itself and picks a verdict biased toward APPROVE — see "
            "the action parameter for the exact policy. "
            "Findings are posted as review comments on the original PR via the GitHub Reviews "
            "API, never as a new PR. "
            "Use this for a quick diff-only review when no worker is available. For a full "
            "review that checks out the branch and runs tests/lint, use "
            "create_task(phase='review') + assign_task instead."
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
                        "Review verdict to submit to GitHub. Omit to let the tool decide from its "
                        "own analysis of the diff — biased toward APPROVE. Policy: "
                        "APPROVE — the code is functionally correct and any issues are minor nits "
                        "(style, naming, formatting); note the nits as inline comments but approve. "
                        "COMMENT — moderate concerns (performance, clarity) that don't block "
                        "merging. REQUEST_CHANGES — reserved for genuine bugs, security issues, or "
                        "logic errors that must be fixed before merge; never for style preferences. "
                        "Only pass this explicitly to override the tool's own judgement."
                    ),
                },
            },
            "required": ["pr_url"],
        },
    },
    {
        "name": "dnsid",
        "description": (
            "Verify a DNSid principal or a DNSid-signed JWT through the configured "
            "dnsid-py IdentityManager. Signing is intentionally not exposed to the model."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["resolve", "verify"],
                    "description": "Subcommand to run.",
                },
                "fqdn": {
                    "type": "string",
                    "description": "resolve: FQDN to look up, e.g. 'example.com'.",
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
    {
        "name": "analyze_epic",
        "description": (
            "Level 1: lightweight epic status summary. Fetches sub-issues and linked PRs, "
            "returns completion metrics and identified gaps/inconsistencies at a glance. "
            "Fast and suitable for periodic checks. Does NOT perform deep code analysis. "
            "Posts a status summary comment on the epic and adds a 'pm-reported' label "
            "to prevent re-running. When significant gaps are detected, suggests "
            "triggering a Level 2 worker task (deep_epic_analysis) for code-level review. "
            "Use this when an epic with 'devReady' label has all sub-issues completed, "
            "or when asked for a quick status check on an epic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "owner/repo (e.g. 'jmelloy/pioneer-square').",
                },
                "issue_number": {
                    "type": "integer",
                    "description": "The epic issue number to analyze.",
                },
                "file_gap_issues": {
                    "type": "boolean",
                    "description": (
                        "Deprecated. This parameter no longer creates issues. "
                        "Kept for backward compatibility."
                    ),
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "If true, run even if 'pm-reported' label is already present "
                        "or a report was posted recently. Default: false."
                    ),
                },
                "trigger_deep_analysis": {
                    "type": "boolean",
                    "description": (
                        "If true and significant gaps are found, automatically create "
                        "a worker task for Level 2 deep code analysis. Default: false. "
                        "When false, returns a recommendation flag instead."
                    ),
                },
            },
            "required": ["repo", "issue_number"],
        },
    },
    {
        "name": "message_discord_bot",
        "description": (
            "Send a message to a bot user over Discord. Looks up the bot's user row "
            "(auto-provisioned the first time it posted — see discord/bot_users.py) for its "
            "preferred channel and Discord identity, then delivers via the bot API. "
            "Fails if bot_user_id does not identify a Discord bot (is_bot=True, "
            "bot_provider='discord')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bot_user_id": {
                    "type": "string",
                    "description": "Pioneer Square user id of the target bot (e.g. 'discordbot:123').",
                },
                "message": {
                    "type": "string",
                    "description": "Message content to send.",
                },
                "delivery_method": {
                    "type": "string",
                    "enum": ["channel", "mention"],
                    "description": (
                        "'channel' (default) posts to the bot's preferred discord_channel_id. "
                        "'mention' posts an @mention of the bot into that same channel — use "
                        "when a plain channel post might not get the bot's attention."
                    ),
                },
            },
            "required": ["bot_user_id", "message"],
        },
    },
    {
        "name": "spawn_worker",
        "description": (
            "Start a new worker process to run tasks. Use when no worker is online for the "
            "repos a task needs, or when every capable worker is busy and work is queueing up. "
            "This call is asynchronous: it returns as soon as the worker is pre-registered and "
            "its container/ECS task has been started, but the worker itself takes about 2 "
            "minutes to come online and start picking up assigned tasks. Do not spawn "
            "a duplicate if a worker for the same repos is already online or currently starting. "
            "Spawned workers are automatically shut down after a period of inactivity, so there "
            "is no need to clean up idle workers yourself (shutdown_worker still works for an "
            "immediate stop). Parameters you omit fall back to the guild's spawn defaults, which "
            "are the right baseline for a worker serving the whole guild's queue; a guild with no "
            "defaults requires explicit repos. Pass parameters explicitly whenever this spawn "
            "should differ from that baseline."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Repos the worker should serve, as 'owner/repo'. Optional: defaults to "
                        "the guild's spawn defaults. Set it when the task you are standing this "
                        "worker up for needs a repo the defaults do not cover."
                    ),
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional tool runners to enable on the worker "
                        "(e.g. ['claude', 'codex']). Defaults to the guild's spawn defaults, "
                        "else claude only."
                    ),
                },
                "agent_count": {
                    "type": "integer",
                    "description": (
                        "Optional number of concurrent agent slots. Defaults to the guild's "
                        "spawn default, else 1."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Optional human-readable worker name.",
                },
            },
            "required": [],
        },
    },
]
