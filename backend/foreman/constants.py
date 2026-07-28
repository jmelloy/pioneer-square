"""Shared constants for the foreman AI runner."""

# ~8 k tokens; cap per-result content before storing/sending. Tools that commonly
# produce large responses include get_task_status, list_github_issues,
# get_github_issue, and search_github_issues.
MAX_TOOL_RESULT_CHARS = 32_000
MAX_HISTORY_MESSAGES = 20  # sliding window cap on messages sent to Anthropic
MAX_FOREMAN_ROUNDS = 10  # safety cap on tool-call rounds per invocation
_HUMAN_TURN_WINDOW = 5  # how many non-tool-response user turns to load from DB
_TERMINAL_STATES = frozenset({"done", "failed", "cancelled", "error"})
_24H_SECS = 86_400
