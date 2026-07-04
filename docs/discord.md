# Discord Integration

Pioneer Square can mirror activity into a Discord server: event notifications, per-PR/issue
discussion threads, a live Foreman chat feed, and `/ps` slash commands that let you drive
the factory floor from Discord. Everything is optional and additive — with no Discord env
vars set, the integration is a silent no-op.

The integration shipped in three phases, all backwards-compatible with the phase before it:

| Phase | What it adds | Needs |
|---|---|---|
| 1 | Flat-channel webhook notifications | `DISCORD_WEBHOOK_URL` |
| 2 | Per-PR/issue threads, thread archiving | `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` |
| 3 | Slash commands, Foreman chat mirror, @-mentions | `DISCORD_APPLICATION_ID` + `DISCORD_PUBLIC_KEY` (+ Phase 2 vars) |

You can stop at any phase — e.g. run Phase 1 only for simple notifications, or Phase 1 + 2
without ever setting up slash commands.

## Setup guide

### 1. Create the Discord application and bot

In the [Discord Developer Portal](https://discord.com/developers/applications):

1. **New Application** → give it a name (e.g. "Pioneer Square").
2. Under **Bot**, click **Add Bot**, then copy the **Token** — this is `DISCORD_BOT_TOKEN`.
   Keep it out of version control.
3. Under **General Information**, copy the **Application ID** (`DISCORD_APPLICATION_ID`) and
   **Public Key** (`DISCORD_PUBLIC_KEY`).
4. If you plan to use slash commands (Phase 3), set **Interactions Endpoint URL** on the same
   page to `https://<your-backend-host>/discord/interactions`. Discord will send a PING to
   this URL and expects a signed PONG back — it won't save the URL until the backend is
   reachable and `DISCORD_PUBLIC_KEY` is configured, so deploy the backend first.

   > **Ordering matters:** `DISCORD_PUBLIC_KEY` (see [step 3](#3-environment-variables)) must
   > already be set on the *running* backend before you try to save the Interactions Endpoint
   > URL here — Discord's verification PING is signature-checked against that key, and the
   > save fails if the backend doesn't have it yet. Set the env var, (re)deploy, confirm the
   > backend is reachable, then come back and save the URL.

### 2. Bot permissions and invite

| Permission | Needed for |
|---|---|
| `SEND_MESSAGES` | Posting notifications and thread starter messages |
| `CREATE_PUBLIC_THREADS` | Creating per-PR/issue threads (Phase 2) |
| `MANAGE_THREADS` | Archiving threads on PR merge/close (Phase 2) |

Build an invite URL with the **OAuth2 URL Generator** (OAuth2 → URL Generator):
- Scopes: `bot` (and `applications.commands` if you want slash commands — Phase 3)
- Bot permissions: the three above

Open the generated URL and invite the bot to your server before setting any env vars — the
bot must already be a member of the guild that owns `DISCORD_CHANNEL_ID`.

### 3. Environment variables

| Variable | Phase | Required | Description |
|---|---|---|---|
| `DISCORD_WEBHOOK_URL` | 1 | No | Incoming webhook URL for flat-channel embeds. Create one via a channel's *Integrations → Webhooks*. Also used as the fallback target when the bot token is unset or thread routing fails. |
| `DISCORD_BOT_TOKEN` | 2/3 | No | Bot token from the Developer Portal. Enables thread routing, slash commands, and the Foreman chat mirror. Reused across all three phases. |
| `DISCORD_CHANNEL_ID` | 2/3 | With `DISCORD_BOT_TOKEN` | ID of the text channel new threads are created in. Enable Developer Mode in Discord settings, then right-click the channel → *Copy Channel ID*. |
| `DISCORD_APPLICATION_ID` | 3 | For slash commands | Application ID, used to build the followup-message URL when editing a deferred slash command reply. |
| `DISCORD_PUBLIC_KEY` | 3 | For slash commands | Ed25519 public key (hex) used to verify `POST /discord/interactions` requests. Without it, the endpoint refuses all requests with 401. |
| `DISCORD_ALLOWED_ROLE_IDS` | 3 | No | Comma-separated Discord role IDs allowed to run `/ps` commands. Empty/unset = everyone allowed. DM interactions are always denied when this is set (no role info is available outside a guild). |
| `DISCORD_PIONEER_GUILD_SLUG` | 3 | For slash commands | The Pioneer Square guild (workspace) slug that `/ps` commands operate against. Not a Discord ID — see [Terminology note](#terminology-note-guild-vs-guild) below. |
| `DISCORD_DEV_GUILD_ID` | 3 (registration only) | No | Discord server ID. When set, `scripts/register_discord_commands.py` registers commands to that one server (near-instant) instead of globally (up to 1 hour to propagate). |

`DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` must be set **together** — the bot needs both
the credential and a destination channel to create threads.

#### Terminology note: guild vs. guild

Discord calls a server a "guild." Pioneer Square independently calls a workspace a "guild"
(the 6-character ID used in URLs and `pioneer-worker.toml`). `DISCORD_PIONEER_GUILD_SLUG` is
a **Pioneer Square** guild slug, not a Discord server ID — it tells the `/ps` command handler
which Pioneer Square workspace to query and mutate. The Discord server your bot lives in is
identified only implicitly, via `DISCORD_CHANNEL_ID`.

### 4. Register slash commands (Phase 3 only)

Slash commands aren't automatically registered — run this once after deploying, and again
whenever `scripts/register_discord_commands.py` changes:

```bash
DISCORD_APPLICATION_ID=<id> DISCORD_BOT_TOKEN=<token> python scripts/register_discord_commands.py
```

For faster iteration during development, scope registration to one server instead of waiting
up to an hour for a global rollout:

```bash
DISCORD_APPLICATION_ID=<id> DISCORD_BOT_TOKEN=<token> \
    DISCORD_DEV_GUILD_ID=<guild_id> python scripts/register_discord_commands.py
```

### docker-compose

`docker-compose.yml` already forwards all Discord variables to the `backend` service; set
them in your `.env` (copy from `.env.example`):

```dotenv
# Phase 1: flat-channel webhook notifications.
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxx/yyy

# Phase 2: bot-based per-PR/issue thread notifications. Both required together.
DISCORD_BOT_TOKEN=MTxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DISCORD_CHANNEL_ID=1234567890123456789

# Phase 3: slash commands & bidirectional control.
DISCORD_APPLICATION_ID=1234567890123456789
DISCORD_PUBLIC_KEY=<hex public key>
DISCORD_ALLOWED_ROLE_IDS=1111111111111111,2222222222222222
DISCORD_PIONEER_GUILD_SLUG=abc123
```

```bash
docker compose up --build backend
```

## Feature reference

### Phase 1 — flat webhook notifications

Set only `DISCORD_WEBHOOK_URL` to post colour-coded embeds to a single channel. Delivery is
fire-and-forget: notifications run as detached background tasks and never block the caller;
HTTP failures are logged at WARNING and swallowed, never raised.

Colour palette (sidebar colour of the embed):

| Event | Colour |
|---|---|
| `task-complete`, `ci-pass` | green |
| `task-failed`, `task-cancelled`, `ci-fail` | red |
| `pr-opened` | blue |
| `pr-merged` | purple |
| `pr-closed` | grey |
| `worker-online` | teal |
| `worker-offline` | orange |
| anything else | blurple (default) |

`task-failed` and `task-cancelled` have colours reserved but are not currently emitted by any
call site — only the events listed below actually fire notifications today:

- `worker-online` / `worker-offline` — always flat (never thread-routed), fired on WebSocket
  worker connect/disconnect.
- `pr-opened`, `pr-merged`, `pr-closed` — fired from the GitHub webhook handler.
- `ci-pass`, `ci-fail` — fired from GitHub `check_run`/`check_suite` webhook events.
- `task-complete` — fired when a worker reports a task finished.

### Phase 2 — per-PR/issue threads

Add `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` to route PR/issue-related events into one
Discord thread per `(issue_repo, issue_number)` pair instead of a flat channel.

**Thread lifecycle:**

1. **Lazy creation** — the first thread-aware event for a given PR/issue posts a starter
   message in `DISCORD_CHANNEL_ID`, then creates a public thread off that message named
   `#<number>: <title>` (truncated to 100 chars).
2. **Persistence** — the mapping is written to the `discord_threads` table
   (`issue_repo`, `issue_number`, `thread_id`), unique on `(issue_repo, issue_number)`, so a
   backend restart reuses the existing thread instead of creating a duplicate.
3. **Reuse** — every subsequent event for the same PR/issue (CI results, task-complete) is
   posted as an embed inside the existing thread.
4. **Archive** — on `pr-merged` or `pr-closed`, a closing summary is posted and the thread is
   archived (`PATCH` with `archived: true`).

**Fallback behaviour** — thread routing is attempted only when `DISCORD_BOT_TOKEN` is set
*and* the event carries `issue_repo`/`issue_number`. It falls back to the Phase 1 flat webhook
when:
- `DISCORD_BOT_TOKEN` is unset, or
- thread creation fails (missing channel, Discord API error, DB error).

If neither `DISCORD_BOT_TOKEN` nor `DISCORD_WEBHOOK_URL` is set, notifications are a silent
no-op — nothing is sent and nothing is logged as an error.

### Phase 3 — slash commands & bidirectional control

Adds `POST /discord/interactions`, a signed webhook endpoint Discord calls for every
interaction (health-check pings and slash-command invocations).

- **Signature verification** — every request must carry valid `X-Signature-Ed25519` and
  `X-Signature-Timestamp` headers, verified against `DISCORD_PUBLIC_KEY` over the raw request
  body. Missing/invalid signatures get a `401`. Requests are rejected outright if
  `DISCORD_PUBLIC_KEY` isn't configured.
- **PING** — Discord's endpoint health-check; answered immediately with `PONG`.
- **Authorization** — checked against `DISCORD_ALLOWED_ROLE_IDS` before anything else runs.
  No roles configured = everyone allowed. DMs are always denied once a role restriction is
  configured, since Discord doesn't include member/role data on DM interactions.
- **Deferred replies** — Discord requires a response within 3 seconds. The endpoint
  immediately acknowledges with a deferred *ephemeral* (only visible to the invoking user)
  response, then runs the actual command in a background task and edits the reply in place
  once it finishes.

See [Slash command reference](#slash-command-reference) below for the full command list.

### Foreman chat mirror

When `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` are set, every plain-text line the Foreman AI
sends to a user (not tool-call traces) is mirrored into Discord via
`notify_foreman_chat(guild_id, content)`.

- One thread is created per `(guild_id, session_key)` pair, where `session_key` defaults to
  the current UTC date (`YYYY-MM-DD`) — so a whole day's Foreman conversation for a given
  Pioneer Square guild lands in one thread, named `Foreman session <date> (<guild_id>)`.
- The mapping persists in the `discord_foreman_threads` table, unique on
  `(guild_id, session_key)`.
- Silent no-op if the bot token/channel aren't configured, or if the content is blank.

## Slash command reference

All commands are subcommands of `/ps` and are scoped to the guild in
`DISCORD_PIONEER_GUILD_SLUG`. Every reply is ephemeral. Requires a role in
`DISCORD_ALLOWED_ROLE_IDS` (or no restriction configured).

| Command | Description | Notes |
|---|---|---|
| `/ps status` | Worker counts (total/online/idle/offline) and active task counts by state | Read-only |
| `/ps workers` | Lists every worker with state, repos, and live agent count | Read-only |
| `/ps pickup <issue-url>` | Creates a `pending` task (`phase=execute`) for a GitHub issue URL and queues it for an idle worker | URL must match `https://github.com/<owner>/<repo>/issues/<number>` |
| `/ps review <pr-url>` | Creates a `pending` review task (`phase=review`) for a GitHub PR URL | URL must match `https://github.com/<owner>/<repo>/pull/<number>` |
| `/ps cancel <task-id>` | Cancels a running task and soft-deletes it after a 3-day TTL | No-op reply if the task is already `done`/`failed`/`cancelled` |

Commands are defined in `scripts/register_discord_commands.py` and must be re-registered
(see [Setup guide](#4-register-slash-commands-phase-3-only)) whenever that file changes.

## User identity linking

The `discord_users` table maps a lowercased GitHub login to a Discord user ID, so
notifications can @-mention the right person instead of printing a plain `@login` string.

| Column | Description |
|---|---|
| `github_login` | Primary key, stored lowercase |
| `discord_user_id` | Discord snowflake ID |
| `created_at` / `updated_at` | Timestamps |

There is no automated discovery and no in-Discord `/link-account` command — a user links
their own account via the REST API:

| Endpoint | Auth | Behaviour |
|---|---|---|
| `GET /api/discord-users` | Any authenticated user | List all mappings |
| `GET /api/discord-users/{github_login}` | Any authenticated user | Get one mapping, `404` if none |
| `PUT /api/discord-users/{github_login}` | Must be authenticated **as** `github_login` | Upsert `{"discord_user_id": "..."}`; `403` if you try to set someone else's mapping |
| `DELETE /api/discord-users/{github_login}` | Must be authenticated **as** `github_login` | Remove your own mapping |

To find your own Discord user ID: enable **Developer Mode** in Discord (User Settings →
Advanced), then right-click your username anywhere → **Copy User ID**.

**@-mention behaviour** — `mention_or_login(github_login)` is used wherever a GitHub actor is
rendered in a notification (currently: PR-opened assignees). It:
- Returns a real `<@discord_user_id>` mention when a mapping exists.
- Falls back to a plain `@github_login` string when no mapping exists.
- Returns `""` for an empty/missing login.
- Never raises — a DB error during lookup degrades to the plain-text fallback.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No Discord messages at all, no errors | Neither `DISCORD_WEBHOOK_URL` nor `DISCORD_BOT_TOKEN` is set — this is a silent no-op by design. Check `docker compose config` to confirm the vars actually reach the `backend` container. |
| Notifications land in the flat channel instead of a thread | `DISCORD_BOT_TOKEN` is set but `DISCORD_CHANNEL_ID` is missing, or thread creation failed and it fell back to `DISCORD_WEBHOOK_URL`. Check backend logs for `discord: bot API request failed` / `discord: thread DB save failed` warnings. |
| Bot API calls fail with 403 | Bot is missing `SEND_MESSAGES`, `CREATE_PUBLIC_THREADS`, or `MANAGE_THREADS` — re-invite it with the correct OAuth2 scopes/permissions, or grant channel-level permission overwrites. |
| Slash commands don't appear in Discord | Commands were never registered, or registered globally and haven't propagated yet (up to 1 hour). Run `scripts/register_discord_commands.py`; use `DISCORD_DEV_GUILD_ID` for instant guild-scoped registration while iterating. |
| `POST /discord/interactions` returns `401 Invalid signature` | `DISCORD_PUBLIC_KEY` is unset or doesn't match the application, or a reverse proxy is rewriting/re-encoding the raw request body (the signature is computed over the exact bytes Discord sent). |
| "You are not authorized to use Pioneer Square commands" | `DISCORD_ALLOWED_ROLE_IDS` is set and the invoking user has none of the listed roles. Note DMs are always denied once this restriction is configured. |
| `/ps` commands reply "Pioneer guild `` not found" | `DISCORD_PIONEER_GUILD_SLUG` is unset or doesn't match an existing Pioneer Square guild slug. |
| @-mentions show as plain `@login` instead of a real mention | No `discord_users` row for that GitHub login yet — the user needs to `PUT /api/discord-users/{their_login}` with their Discord user ID. |
| Duplicate threads for the same PR/issue | Shouldn't happen — `discord_threads` has a unique index on `(issue_repo, issue_number)`. If seen, confirm the `add_discord_threads` Alembic migration has actually been applied. |
