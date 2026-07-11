# Discord Integration

Pioneer Square can mirror activity into a Discord server: event notifications, per-PR/issue
discussion threads, a live Foreman chat feed, and `/ps` slash commands that let you drive the
factory floor from Discord — all optional and additive, driven by a single bot, layering up as
you add credentials.

**Silent no-op by design:** with no Discord env vars set — or whenever a specific feature's
required vars are missing — the relevant code path does nothing and logs nothing as an error.
HTTP and DB failures are logged at WARNING and swallowed, never raised. This applies
throughout the doc below and isn't repeated per-section.

| Layer | What it adds | Needs |
|---|---|---|
| Notifications | Flat-channel embeds, per-PR/issue threads, thread archiving | `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` |
| Slash commands | `/ps` commands, Foreman chat mirror, @-mentions | `DISCORD_APPLICATION_ID` + `DISCORD_PUBLIC_KEY` (+ the bot vars) |

You can stop after the notification layer without ever setting up slash commands.

## Setup guide

### 1. Create the Discord application and bot

In the [Discord Developer Portal](https://discord.com/developers/applications):

1. **New Application** → give it a name (e.g. "Pioneer Square").
2. Under **Bot**, click **Add Bot**, then copy the **Token** — this is `DISCORD_BOT_TOKEN`.
   Keep it out of version control.
3. Under **General Information**, copy the **Application ID** (`DISCORD_APPLICATION_ID`) and
   **Public Key** (`DISCORD_PUBLIC_KEY`).
4. For slash commands, set **Interactions Endpoint URL** to
   `https://<your-domain>/discord/interactions` (the same public origin the SPA and API use).
   Discord verifies it with a signed PING/PONG, so `DISCORD_PUBLIC_KEY`
   ([step 3](#3-environment-variables)) must already be live on the backend first — set the env
   var, deploy, confirm reachability, *then* save the URL.

### 2. Bot permissions and invite

| Permission | Needed for |
|---|---|
| `SEND_MESSAGES` | Posting notifications and thread starter messages |
| `CREATE_PUBLIC_THREADS` | Creating per-PR/issue threads |
| `MANAGE_THREADS` | Archiving threads on PR merge/close |

Build an invite URL with the **OAuth2 URL Generator** (scopes: `bot`, plus
`applications.commands` for slash commands; bot permissions: the three above), then invite the
bot to your server before setting any env vars — it must already be a member of the guild that
owns `DISCORD_CHANNEL_ID`.

### 3. Environment variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | For any notifications | Bot token from the Developer Portal; backs embeds, thread routing, slash commands, and the Foreman chat mirror. |
| `DISCORD_CHANNEL_ID` | With `DISCORD_BOT_TOKEN` | Text channel where flat embeds post and threads are created (Developer Mode → right-click channel → *Copy Channel ID*). |
| `DISCORD_APPLICATION_ID` | For slash commands | Used to build the followup-message URL when editing a deferred slash-command reply. |
| `DISCORD_PUBLIC_KEY` | For slash commands | Ed25519 public key (hex) verifying `POST /discord/interactions` requests; missing it → all requests refused with 401. |
| `DISCORD_ALLOWED_ROLE_IDS` | No | Comma-separated role IDs allowed to run `/ps` commands. Empty/unset = everyone. DMs are always denied once this is set (no role info outside a guild). |
| `DISCORD_PIONEER_GUILD_SLUG` | For slash commands | The Pioneer Square guild (workspace) slug `/ps` commands operate against — not a Discord ID (see terminology note below). |
| `DISCORD_OPERATOR_ROLE_NAME` | No | Role name (besides Manage Channels) allowed to run `/join-channel`/`/leave-channel`. Default `Pioneer Square Operator`. |
| `DISCORD_DEV_GUILD_ID` | No (registration only) | Scopes `scripts/register_discord_commands.py` registration to one server (near-instant) instead of globally (up to 1 hour). |
| `DISCORD_STREAM_TASKS` | No | When truthy (`1`/`true`/`yes`/`on`), mirror each working task's live terminal output into a dedicated per-task Discord thread as silent, low-priority messages. Currently off by default (high-volume). Requires `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` — the feed always routes into a thread, never a flat channel post. |
| `DISCORD_PR_DEBOUNCE_SECONDS` | No | Seconds to buffer `check_run`/`check_suite` completions for the same PR before flushing them as one combined, silent Discord message. Currently defaults to `15`. |

`DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` must be set **together** — the bot needs both
the credential and a destination channel to create threads.

**Terminology note:** Discord calls a server a "guild." Pioneer Square independently calls a
workspace a "guild" (the 6-character ID used in URLs and `pioneer-worker.toml`).
`DISCORD_PIONEER_GUILD_SLUG` is the latter, not a Discord server ID.

### 4. Register slash commands (slash commands only)

Slash commands aren't automatically registered — run this once after deploying, and again
whenever `scripts/register_discord_commands.py` changes:

```bash
DISCORD_APPLICATION_ID=<id> DISCORD_BOT_TOKEN=<token> python scripts/register_discord_commands.py
```

Add `DISCORD_DEV_GUILD_ID=<guild_id>` to the same command to scope registration to one server
instead — near-instant, versus up to an hour to propagate globally — handy while iterating.

**docker-compose:** `docker-compose.yml` already forwards all Discord variables above to the
`backend` service — set them in your `.env` (copy from `.env.example`), then
`docker compose up --build backend`.

## Feature reference

### Flat-channel notifications

With `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` set, events that don't map to a per-PR/issue
thread post colour-coded embeds to the configured channel via the bot, fire-and-forget (never
blocks the caller).

Colour palette (sidebar colour of the embed) as of this writing:

| Event(s) | Colour |
|---|---|
| `task-complete`, `ci-pass` | green |
| `task-failed`, `task-cancelled`, `ci-fail` | red |
| `pr-opened` | blue |
| `pr-merged` | purple |
| `pr-closed` | grey |
| `worker-online` | teal |
| `worker-offline` | orange |
| anything else | blurple (default) |

`task-failed` and `task-cancelled` have colours reserved but currently have no call site that
emits them — only `pr-opened`/`pr-merged`/`pr-closed` (GitHub webhook handler),
`ci-pass`/`ci-fail` (check_run/check_suite events), and `task-complete` (worker report) fire
notifications today.

**CI check debounce/combine** — `check_run`/`check_suite` completions for the same PR are
currently buffered and flushed as one combined, silent message after
`DISCORD_PR_DEBOUNCE_SECONDS` (default 15s) of quiet, instead of one message per check; non-CI
events post immediately. Currently a **fixed-start** window (arms on the first buffered check,
not reset by later ones) — set it above the longest expected gap between checks, or a late one
gets its own follow-up message instead of joining the summary.

### Per-PR/issue threads

Add `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` to route PR/issue-related events into one
Discord thread per `(issue_repo, issue_number)` pair instead of a flat channel. Lifecycle:

1. **Lazy creation** — the first thread-aware event for a given PR/issue posts a starter
   message in `DISCORD_CHANNEL_ID`, then creates a public thread off that message named
   `#<number>: <title>` (truncated to 100 chars).
2. **Persistence** — the mapping is stored in `discord_thread_bindings`, keyed
   `subject_type="issue"`, `subject_key="<issue_repo>#<issue_number>"`, unique on
   `(subject_type, subject_key)`. This table is shared with the other thread-aware features
   below via one lookup/create path, so a backend restart always reuses the existing thread.
3. **Reuse** — every subsequent event for the same PR/issue (CI results, task-complete) is
   posted as an embed inside the existing thread.
4. **Archive** — on `pr-merged` or `pr-closed`, a closing summary is posted and the thread is
   archived (`PATCH` with `archived: true`).

Thread routing only kicks in when the event carries `issue_repo`/`issue_number`; when it
doesn't, or thread creation fails (missing channel, Discord API error, DB error), the event
falls back to a flat embed in the configured channel.

### Slash commands & bidirectional control

Adds `POST /discord/interactions`, a signed webhook endpoint Discord calls for every
interaction (health-check pings and slash-command invocations).

- **Signature verification** — every request must carry valid `X-Signature-Ed25519` /
  `X-Signature-Timestamp` headers, verified against `DISCORD_PUBLIC_KEY`; missing/invalid
  signatures (or a missing key) get a `401`. Discord's health-check `PING` gets an immediate
  `PONG`.
- **Authorization** — checked against `DISCORD_ALLOWED_ROLE_IDS` first; no roles configured
  means everyone is allowed. DMs are always denied once set (Discord omits member/role data on
  DM interactions).
- **Deferred replies** — Discord requires a response within 3 seconds, so the endpoint
  acknowledges immediately with a deferred *ephemeral* reply, then runs the command in the
  background and edits the reply in place once done.

See [Slash command reference](#slash-command-reference) for `/ps` and channel-routing commands;
a couple more (account linking, worker management) are covered under
[User identity linking](#user-identity-linking).

### Foreman chat mirror

When `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` are set, every plain-text line the Foreman AI
sends to a user (not tool-call traces) is mirrored into Discord. If the line is scoped to a task
whose PR/issue already has a Discord thread, it posts there; otherwise it posts to the guild's
main channel — currently there's no separate daily/dated fallback thread. No-op if blank.

### Live task-stream mirroring

Set `DISCORD_STREAM_TASKS` (plus `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID`) to mirror a
worker task's **live terminal output** into Discord while it runs. Currently off by default,
since the volume is high.

- **Per-task thread** — created lazily off a starter message, currently named
  `⚙ <task-id>: <description>`, persisted in `discord_thread_bindings`
  (`subject_type="task_stream"`, keyed by task ID). Kept deliberately separate from the
  PR/issue thread: this is the verbose working feed, while that one holds the tidy summary.
- **What's mirrored** — only actual agent/Claude output (`info`/`thinking`-level frames); worker
  lifecycle, auth, and runner framing lines are filtered out.
- **Low priority, batched** — messages currently carry `SUPPRESS_NOTIFICATIONS` and disabled
  mention parsing, and lines are flushed every few seconds (or sooner once buffered) rather than
  one POST per line, to stay under Discord's rate limits.

## Slash command reference

The `/ps` subcommands are scoped to the guild in `DISCORD_PIONEER_GUILD_SLUG`, require a role in
`DISCORD_ALLOWED_ROLE_IDS` (or no restriction), and always reply ephemerally. Commands are
defined in `scripts/register_discord_commands.py` and must be re-registered (step 4 above)
whenever that file changes.

| Command | Description | Notes |
|---|---|---|
| `/ps status` | Worker counts (total/online/idle/offline) and active task counts by state | Read-only |
| `/ps workers` | Lists every worker with state, repos, and live agent count | Read-only |
| `/ps pickup <issue-url>` | Creates a `pending` task (`phase=execute`) for a GitHub issue URL and queues it for an idle worker | URL must match `https://github.com/<owner>/<repo>/issues/<number>` |
| `/ps review <pr-url>` | Creates a `pending` review task (`phase=review`) for a GitHub PR URL | URL must match `https://github.com/<owner>/<repo>/pull/<number>` |
| `/ps cancel <task-id>` | Cancels a running task and soft-deletes it after a 3-day TTL | No-op reply if the task is already `done`/`failed`/`cancelled` |
| `/join-channel <channel> [guild]` | Wires a Discord channel to a Pioneer Square guild so its events post there | Requires **Manage Channels** or the `DISCORD_OPERATOR_ROLE_NAME` role. `guild` is optional if exactly one Pioneer Square guild is configured. Re-running on an already-wired channel updates the binding. |
| `/leave-channel [channel]` | Removes a channel's Pioneer Square guild binding (defaults to the current channel) | Same permission check as `/join-channel`. Replies "No binding found" if the channel isn't wired. |

### Per-channel guild routing (`/join-channel` / `/leave-channel`)

By default, all notifications go to the single channel in `DISCORD_CHANNEL_ID`. The
`discord_channel_guilds` table lets you fan a specific Pioneer Square guild's events out to
additional channels — e.g. one channel per team. `/join-channel channel:#my-team-updates
guild:my-guild-slug` upserts a `(discord_guild_id, discord_channel_id) → ps_guild_id` row;
`notify_event(...)`/`notify_foreman_chat(...)` look up the event's guild here first, falling
back to `DISCORD_CHANNEL_ID`. `/leave-channel` deletes the row, restoring that fallback.

## User identity linking

The `discord_users` table maps a lowercased GitHub login to a Discord user ID, so
notifications can @-mention the right person instead of printing a plain `@login` string.

| Column | Description |
|---|---|
| `github_login` | Primary key, stored lowercase |
| `discord_user_id` | Discord snowflake ID |
| `created_at` / `updated_at` | Timestamps |

No automated discovery, and no in-Discord command populates this mapping directly — link via
the REST API below. (A separate `/connect-account` command links a Discord identity to a
Pioneer Square *account* for command authorization like `/worker-spawn`, via a different table,
`discord_account_links` — unrelated to @-mentions.)

| Endpoint | Auth | Behaviour |
|---|---|---|
| `GET /api/discord-users` | Any authenticated user | List all mappings |
| `GET /api/discord-users/{github_login}` | Any authenticated user | Get one mapping, `404` if none |
| `PUT /api/discord-users/{github_login}` | Must be authenticated **as** `github_login` | Upsert `{"discord_user_id": "..."}`; `403` if you try to set someone else's mapping |
| `DELETE /api/discord-users/{github_login}` | Must be authenticated **as** `github_login` | Remove your own mapping |

To find your own Discord user ID: enable **Developer Mode** (User Settings → Advanced), then
right-click your username → **Copy User ID**.

**@-mention behaviour** — `mention_or_login(github_login)` is used wherever a GitHub actor is
rendered in a notification (currently: PR-opened assignees). It returns a real
`<@discord_user_id>` mention when a mapping exists, otherwise falls back to a plain
`@github_login` string (or `""` for an empty login) — never raises, so a DB error during
lookup just degrades to the plain-text fallback.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No Discord messages at all, no errors | `DISCORD_BOT_TOKEN` is not set — this is a silent no-op by design. Check `docker compose config` to confirm the vars actually reach the `backend` container. |
| Notifications land in the flat channel instead of a thread | The event carries no `issue_repo`/`issue_number`, or thread creation failed and it fell back to a flat channel embed. Check backend logs for `discord: bot API request failed` / `discord: thread DB save failed` warnings. |
| Bot API calls fail with 403 | Bot is missing `SEND_MESSAGES`, `CREATE_PUBLIC_THREADS`, or `MANAGE_THREADS` — re-invite it with the correct OAuth2 scopes/permissions, or grant channel-level permission overwrites. |
| Slash commands don't appear in Discord | Commands were never registered, or registered globally and haven't propagated yet (up to 1 hour). Run `scripts/register_discord_commands.py`; use `DISCORD_DEV_GUILD_ID` for instant guild-scoped registration while iterating. |
| `POST /discord/interactions` returns `401 Invalid signature` | `DISCORD_PUBLIC_KEY` is unset or doesn't match the application, or a reverse proxy is rewriting/re-encoding the raw request body (the signature is computed over the exact bytes Discord sent). |
| "You are not authorized to use Pioneer Square commands" | `DISCORD_ALLOWED_ROLE_IDS` is set and the invoking user has none of the listed roles. Note DMs are always denied once this restriction is configured. |
| `/ps` commands reply "Pioneer guild `` not found" | `DISCORD_PIONEER_GUILD_SLUG` is unset or doesn't match an existing Pioneer Square guild slug. |
| @-mentions show as plain `@login` instead of a real mention | No `discord_users` row for that GitHub login yet — the user needs to `PUT /api/discord-users/{their_login}` with their Discord user ID. |
| Duplicate threads for the same PR/issue | Shouldn't happen — `discord_thread_bindings` has a unique index on `(subject_type, subject_key)`. If seen, confirm all Discord-related Alembic migrations have actually been applied. |
