# Discord Bot Setup (Phase 2 — Per-PR/Issue Threads)

Phase 1 added flat-channel webhook notifications (`DISCORD_WEBHOOK_URL`).
Phase 2 upgrades to per-PR/issue Discord threads using a bot token.

## Required bot permissions

When creating the application in the [Discord Developer Portal](https://discord.com/developers/applications):

| Permission | Why |
|---|---|
| `SEND_MESSAGES` | Post messages into threads and the parent channel |
| `MANAGE_THREADS` | Archive threads on PR close/merge |

In the OAuth2 URL generator, select **Bot** scope and tick both permissions above.
Invite the bot to your server before setting the env vars.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Phase 2 only | Bot token from the Discord Developer Portal (starts with `MTxx...`). When absent, Phase 1 webhook behaviour is used. |
| `DISCORD_CHANNEL_ID` | Phase 2 only | ID of the text channel where threads are created. Right-click the channel → *Copy Channel ID* (requires Developer Mode). |
| `DISCORD_WEBHOOK_URL` | Phase 1 only | Webhook URL for flat-channel notifications. Used as fallback when `DISCORD_BOT_TOKEN` is not set. |

Both `DISCORD_BOT_TOKEN` **and** `DISCORD_CHANNEL_ID` must be set together — the bot needs to know which channel to create threads in.

## Behaviour summary

| Tokens set | Behaviour |
|---|---|
| `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` | Creates/reuses one thread per PR or issue; routes all events into that thread; archives on close/merge |
| `DISCORD_WEBHOOK_URL` only | Posts all events as flat embeds to the webhook channel (Phase 1) |
| Neither | Silent no-op — no Discord messages sent |
| `DISCORD_BOT_TOKEN` set but thread creation fails | Falls back to `DISCORD_WEBHOOK_URL` if set |

## Thread lifecycle

1. **PR opened / issue assigned** — a thread named `#NNN: <title>` is created in the configured channel. The first message includes PR/issue title, URL, assignee, and labels.
2. **Subsequent events** (CI pass/fail, task-complete) — posted as embed messages inside the thread. Thread IDs are persisted in the `discord_threads` table so restarts do not create duplicate threads.
3. **PR merged or closed** — a summary message is posted and the thread is archived.

## Example `.env`

```dotenv
# Phase 2 (threads)
DISCORD_BOT_TOKEN=MTxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DISCORD_CHANNEL_ID=1234567890123456789

# Phase 1 fallback (optional)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxx/yyy
```
