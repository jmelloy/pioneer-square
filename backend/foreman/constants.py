"""Shared constants for the foreman AI runner."""

# ~8 k tokens; cap per-result content before storing/sending. Tools that commonly
# produce large responses include get_task_status, list_github_issues,
# get_github_issue, and search_github_issues.
MAX_TOOL_RESULT_CHARS = 32_000
MAX_HISTORY_MESSAGES = 20  # sliding window cap on messages sent to Anthropic
MAX_FOREMAN_ROUNDS = 10  # safety cap on tool-call rounds per invocation
_HUMAN_TURN_WINDOW = 3  # how many non-tool-response user turns to load from DB
# ponytail: lowered 5→3 to cut per-run parent-context tokens (~40% less history
# on every round). Periodic checks are near-stateless so recall loss is minor;
# raise back toward 5 if the foreman starts losing conversational thread.
_24H_SECS = 86_400

# --- Conversation-scoped context (#1294) ---------------------------------
# A run that knows its Conversation sends the *whole* conversation, capped
# only by this explicit token budget (see message_utils.fit_token_budget)
# instead of silently falling back to the tiny _HUMAN_TURN_WINDOW /
# MAX_HISTORY_MESSAGES windows above.
FOREMAN_CONTEXT_TOKEN_BUDGET = 120_000
# Rough chars-per-token used to estimate a message's size without calling the
# provider's tokenizer. Deliberately pessimistic (real ratio is ~3.7 for
# English prose, higher for the JSON tool traffic that dominates history).
CHARS_PER_TOKEN = 4
# Hard ceiling on rows read per conversation-scoped history load, so one
# pathological conversation can't pull an unbounded result set into memory.
# ponytail: flat cap, not paging — the token budget throws most of these away
# anyway; add keyset paging only if a real conversation ever hits it.
MAX_CONVERSATION_TURNS = 2_000
# Conversation-linked context rows (tasks / GitHub events) rendered into the
# state preamble alongside the turn history.
MAX_CONVERSATION_TASKS = 50
MAX_CONVERSATION_EVENTS = 20
