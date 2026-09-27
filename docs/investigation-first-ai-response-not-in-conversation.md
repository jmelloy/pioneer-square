# Investigation: "the first AI response doesn't appear in the conversation"

## Report

> The first response from the AI to a conversation doesn't seem to appear in
> the conversation — later responses work, but the first one doesn't show up
> when you fetch/view the conversation.

## Summary

`Message`/`ForemanTurn` persistence itself is correct: every write path
(`foreman.journal.TurnJournal`, `routes/conversations.py`) stamps
`conversation_id` from an already-resolved value and commits it properly. A
minimal end-to-end reproduction (WS chat message → `_run_foreman_ai` →
`TurnJournal.text()` → `GET /conversations/{id}/messages`) round-trips
correctly for the simple, single-conversation case — see
`backend/tests/test_ws_chat_conversation_id.py::test_first_ai_response_appears_when_fetching_the_conversation`.

The real bug is a **conversation-resolution mismatch** between the human's
message and the Foreman's reply, in both of the two production entry points
that trigger a Foreman run from a plain chat message
(`ws_handlers.handle_chat` for the web UI, `discord.router._forward_to_foreman`
for Discord):

1. Each entry point resolves (or creates) a specific `Conversation` up front
   and stamps it onto the human's own `Message` row.
2. Neither entry point passed that resolved `conversation_id` on to
   `foreman.triggers.trigger_foreman`.
3. Because `trigger_foreman` therefore received `conversation_id=None`,
   `foreman.runner._run_foreman_ai` re-resolved a conversation itself, from
   scratch, via `foreman.conversation_service.resolve_conversation_id`'s
   generic `(guild_id, user_id)` fallback — which is defined as "the most
   recently updated `Conversation` for this user" (see
   `conversation_service.get_or_create_conversation`).
4. Since issue #1296, a `(guild_id, user_id)` pair is no longer 1:1 with a
   `Conversation` — a user can have several open conversations at once. The
   two resolutions (the caller's specific one vs. the generic "most recent"
   one) can therefore disagree, and when they do, **the Foreman's reply gets
   persisted under a different `Conversation` than the human's message**. The
   human's message is right where you'd expect it; the AI's reply silently
   lands somewhere else and never shows up when you open the conversation you
   were actually looking at.

This is exactly the failure mode PR #1280 (retrying the reverted #1272)
called out and partially guarded against: *"the new row would be silently
rolled back while callers still stamped rows with its ... never-persisted
id."* That specific commit race was fixed by making
`resolve_conversation_id` commit internally. But the *general* problem — two
independent call sites resolving "the" conversation for the same event and
not being guaranteed to agree — was never fully closed off, because two
call sites (`ws_handlers.handle_chat`, `discord.router._forward_to_foreman`)
kept resolving their own `conversation_id` without forwarding it, instead of
using the `conversation_id` passthrough mechanism `trigger_foreman` already
exposes (and which `routes/conversations.py::post_conversation_message` — the
REST reply endpoint added in #1297 — *does* use correctly).

## Where this is concretely reproducible today

`ws_handlers.handle_chat`'s own resolution and `run_foreman_ai`'s fallback
resolution both reduce to the *same* "most recently updated conversation"
query, so in practice they rarely disagree for the plain web-chat path (no
test previously covered this at all — see the new
`backend/tests/test_ws_chat_conversation_id.py`).

`discord.router._forward_to_foreman` is where the disagreement is easy to
force deterministically, because `_persist_inbound_message` can resolve a
conversation via a *more precise* signal than "most recently updated" —
`Conversation.discord_thread_id` (issue #1278: the source of truth for which
Discord thread belongs to which conversation). Concretely:

1. A user has an older `Conversation` (A) bound to a Discord thread.
2. That thread's `Thread` row is gone — soft-deleted or never created; this
   is `foreman.thread_service.reactivate_conversation_thread`'s own
   documented fallback case.
3. The same user has a newer, unrelated `Conversation` (B) — normal since
   #1296 made every top-level message start a fresh conversation.
4. The user replies inside conversation A's Discord thread.
   `_persist_inbound_message` correctly finds A via
   `Conversation.discord_thread_id` and stamps the human's `Message` with
   `conversation_id=A`.
5. `_forward_to_foreman` called `trigger_foreman` with no `conversation_id`,
   so `_run_foreman_ai` re-derived "the" conversation for
   `(guild_id, user_id)` and picked B (the more recently updated one) instead
   of A.
6. The Foreman's reply is persisted under B. Anyone looking at conversation A
   — the one the human actually replied in — never sees a response.

See `backend/tests/test_discord_router.py::test_forward_to_foreman_pins_the_conversation_it_just_persisted_to`
for a reproduction (now asserting the fixed behavior) and
`backend/tests/test_conversation_scoped_trigger.py` for the pre-existing
tests that document why `routes/conversations.py` already threads
`conversation_id` through explicitly (it needed the same fix, for the same
reason, back in #1297).

## Secondary finding

`ws_handlers.handle_chat`'s outbound `chat` WS broadcast for the human's own
message never included `conversationId`, unlike
`foreman.journal.TurnJournal`'s replies and `routes/conversations.py`'s REST
endpoint (both fixed by #1298's "stamp conversationId on outbound chat
messages"). The frontend's `ConversationDetailPanel.vue` filters its live
overlay (`guildStore.messages`) by `conversationId`, so a Conversations panel
that's already open when the human sends a message would never show that
message live — only after a full reload/refetch. This doesn't affect DB
persistence (the `Message` row itself was always stamped correctly), but it's
the same class of oversight and is fixed alongside the `conversation_id`
plumbing fix below.

## Fix

- `discord/router.py`: `_persist_inbound_message` now returns the
  `conversation_id` it resolved (or created). `_forward_to_foreman` forwards
  that value to `trigger_foreman(conversation_id=...)` instead of leaving it
  `None`.
- `ws_handlers.py`: `handle_chat` now passes the `conversation_id` it already
  resolved via `ensure_conversation_thread` through to
  `trigger_foreman(conversation_id=...)`, and includes `conversationId` on
  the outbound WS broadcast of the human's own message.

Both changes make the two ad-hoc-chat entry points match the pattern already
used (and required) by `routes/conversations.py::post_conversation_message`:
resolve the conversation once, then pin every downstream write to that exact
id instead of letting a second, less precise resolution run later and
potentially disagree.

## Tests added

- `backend/tests/test_ws_chat_conversation_id.py` (new file — `handle_chat`
  had no direct test coverage before this investigation):
  - `test_chat_broadcast_carries_conversation_id`
  - `test_first_ai_response_appears_when_fetching_the_conversation` (full
    end-to-end repro harness: real WS handler, real DB, real `TurnJournal`
    persistence, only the LLM network call stubbed)
  - `test_trigger_foreman_receives_the_conversation_id_handle_chat_already_resolved`
- `backend/tests/test_discord_router.py`:
  - `test_forward_to_foreman_pins_the_conversation_it_just_persisted_to`
    (deterministic reproduction of the Discord-side conversation mismatch,
    now asserting the fix)

## Notes / things not changed

- `foreman.conversation_service.resolve_conversation_id`'s `(guild_id,
  user_id)` "most recently updated" fallback is left as-is — it's still the
  correct behavior for genuinely conversation-agnostic callers (e.g. the
  debug context endpoint), and is exactly the fallback `trigger_foreman`'s
  own thread-ensure guard already relies on when no `conversation_id`/`task_id`
  is available at all.
- This investigation did not find any bug in `foreman.history.ConversationHistory`
  (the LLM-context loader) or in the `Message`/`ForemanTurn` write paths
  themselves — those are correct once they're handed the right
  `conversation_id`.
