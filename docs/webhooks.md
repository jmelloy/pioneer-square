# GitHub Webhook Integration

## Overview

Pioneer Square receives GitHub webhook events at `POST /webhooks/github/{guild_id}`. Each delivery is validated with HMAC-SHA256 against the guild's stored webhook secret.

## Event flow

```
GitHub → POST /webhooks/github/{guild_id}
    ↓
Signature verification + deduplication (X-GitHub-Delivery)
    ↓
_build_foreman_summary()  ──→  run_foreman_ai(summary)   [internal, not shown in chat]
_build_chat_line()        ──→  DB persist + WS broadcast  [filtered from chat UI]
```

Two parallel paths are created for each actionable event:

1. **Foreman summary** (`_build_foreman_summary`) — a structured message fed directly to the Foreman AI via `run_foreman_ai()`. This contains event-specific guidance (e.g. "PR merged — call `finalize_task`") and is part of the AI's internal reasoning context.

2. **Chat line** (`_build_chat_line`) — a human-readable `[github-event]` line stored in the `messages` table and broadcast as `type: "chat"` with `from: "github"`.

## Filtering from the chat UI

Raw `[github-event]` lines are **not displayed** in the foreman chat panel. The frontend store (`guild.ts`) drops any incoming or historically loaded chat message where `from === "github"` before adding it to the visible `messages` array.

This means:
- The Foreman AI still receives all webhook events through its internal summary channel and acts on them normally.
- Foreman *responses* triggered by a webhook event (e.g. a `finalize_task` or `send_followup` decision) appear in chat as normal foreman messages.
- No raw `[github-event]` lines are ever shown to the human operator.

## Actionable events

Only events that pass `_should_dispatch_to_foreman()` wake the Foreman AI:

| Event | Condition |
|-------|-----------|
| `pull_request` | Matching task found; skip bot senders unless CI-related |
| `pull_request_review` | Review submitted with `changes_requested` or `approved` |
| `check_run` / `check_suite` | `completed` conclusion (failure, success, etc.) |
| `status` | Terminal state (failure, error, success) |

Pending, neutral, or skipped check states are silently ignored.
