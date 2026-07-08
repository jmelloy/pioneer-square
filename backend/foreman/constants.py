"""Shared constants for the foreman AI runner."""

MAX_TOOL_RESULT_CHARS = 8_000  # ~2 k tokens; cap per-result content before storing/sending
MAX_HISTORY_MESSAGES = 20  # sliding window cap on messages sent to Anthropic
MAX_FOREMAN_ROUNDS = 10  # safety cap on tool-call rounds per invocation
_HUMAN_TURN_WINDOW = 5  # how many non-tool-response user turns to load from DB
_TERMINAL_STATES = frozenset({"done", "failed", "cancelled", "error"})
_24H_SECS = 86_400
# Default soft-delete window used when estimating completion time from deleted_at.
# Must match routes/tasks.py DEFAULT_FINALIZE_TTL (3 days).
_DEFAULT_TASK_TTL_SECS = 3 * 24 * 60 * 60
