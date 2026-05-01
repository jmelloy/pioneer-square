# Claude auth in the worker — investigation notes

Context for the change in commit `0b29af1` (worker: fix Claude auth in
headless containers via setup-token + PTY) and a sketch of what would
be needed to upgrade to a full-scope login.

## The original symptom

A worker container kicked off `claude auth login`, printed the OAuth
URL, the user authenticated and pasted the `code#state` into the
foreman UI, and then the worker hung forever. The pasted code reached
the worker (`worker-auth-response received` and `Auth code queued`
both logged) but claude never reacted.

## How we narrowed it down

1. Added logger.info around every step of `_run_claude_login` (queue
   dequeue, stdin write, drain). All the writes succeeded; claude
   simply did nothing afterwards.
2. `strace -p <claude>` showed claude in `do_epoll_wait` with **zero
   `read()` syscalls on stdin**. The bytes we wrote were sitting in
   the kernel pipe buffer untouched.
3. First hypothesis: the prompt library checks `process.stdin.isTTY`
   and refuses to read from a pipe. Switched to a PTY pair on stdin —
   no change.
4. `cat /proc/<claude>/stat` field 7 was `0` — claude had **no
   controlling terminal**. The CLI opens `/dev/tty` directly (not
   stdin), so the slave PTY needed to be the child's ctty, not just
   plumbed into stdin.
5. Added `setsid()` + `ioctl(slave_fd, TIOCSCTTY, 0)` in a `preexec_fn`
   so the slave PTY became the child's controlling terminal. Confirmed
   `tty_nr=34816` and `session=pid` after — still hung.
6. Strace then showed claude reading on `/dev/pts/0` *only* when we
   forced a write through gdb (`call write(6, ...)` on the parent's
   master fd). So claude *was* reachable through the PTY — but the
   `claude auth login` CLI command produced just two
   `process.stdout.write` calls (the URL) and then awaited a Promise
   that nothing in the CLI path ever resolves.
7. Spelunking the binary's strings showed the **paste prompt**
   ("Paste code here if prompted > ") lives inside an Ink/React TUI:
   `<TextInput value=… onSubmit=… mask=…>`. That React tree is mounted
   only by `claude setup-token` and the in-session `/login` slash
   command — *not* by `claude auth login`. The CLI `auth login`
   function literally only has `process.stdout.write` for the URL and
   `await waitForAuthorizationCode(...)`, where the only way to
   resolve that promise is either a hit on the localhost OAuth
   callback listener (which needs an auto-opened browser) or
   `handleManualAuthCodeInput()` (which is only called from the React
   `onSubmit`).

So `claude auth login` is genuinely unsupported in headless contexts:
no browser → no localhost callback → no resolver → eternal wait.

## The fix that landed

Switched to `claude setup-token`, which mounts the Ink TUI with the
paste prompt. To make Ink mount and accept input we needed all of:

- A PTY pair attached to **stdin, stdout, *and* stderr** of the child
  (Ink mounts only when stdout is a TTY).
- The slave PTY made the child's **controlling terminal**, via
  `setsid()` + `ioctl(TIOCSCTTY)` between fork and exec, so the
  prompt's `open("/dev/tty")` resolves to our slave.
- The auth code written in **two writes**: the paste, ~200 ms sleep,
  then a CR alone. Including the CR in the same write as the paste
  makes Ink batch them as a single paste event and never fire
  `onSubmit`.
- A regex stripper for the ANSI/CSI escapes Ink emits, so the URL and
  the resulting token can be located in the rendered byte stream.
- Token extraction anchored on the literal "Store this token securely"
  marker — the token is the longest `[A-Za-z0-9_-]{40,}` run in the
  ~400 chars before that marker, which reliably picks up the
  `sk-ant-oat01-…` value.

The token is then placed in `os.environ["CLAUDE_CODE_OAUTH_TOKEN"]`
(inherited by every spawned `claude`) and persisted to the backend as
a base64 JSON `{"oauth_token": "..."}` blob using the existing
`/auth/claude/credentials` endpoint. The startup restore path detects
JSON vs the legacy tarball format.

## Trade-off

`setup-token` issues an **inference-only** token (`scope=user:inference`)
rather than the full claude.ai scope (`org:create_api_key`,
`user:profile`, `user:inference`, `user:sessions:claude_code`,
`user:mcp_servers`, `user:file_upload`) that `claude auth login`
*would* produce.

For the worker's day job — running `claude` against a task description
and capturing the model output — the inference scope is sufficient.
Features that won't work with this credential:

- File upload tools inside Claude Code sessions
- MCP servers requiring claude.ai-scoped auth
- Remote Control / `claude.ai/code` browser bridge
- Anything checking for `user:sessions:claude_code` scope

If a future task needs any of those, the worker has to acquire the
full-scope token instead.

## How a full-scope `/login` flow could work

The full-scope React/Ink login lives inside an interactive `claude`
session, behind the `/login` slash command. There is no CLI subcommand
that triggers it directly, so the worker would need to drive the
interactive REPL.

### Option A — drive `claude` interactive + `/login`

Highest fidelity to the validated harness. Sketch:

1. Spawn `claude` (no `-p`) under the same PTY + ctty harness as
   `setup-token`. Set `claude_path` env, repo root for cwd, etc.
2. Wait for the interactive prompt to settle. Tricky parts:
   - Onboarding wizards on first run (model selection, theme, etc.)
     may run before any prompt. The worker may need to send Enter or
     skip them. Empirically check what a fresh ctty `claude` opens
     with under a non-onboarded `~/.claude/`.
   - Trust dialogs for the working directory.
   - "Tip of the day" / version banner that scrolls past.
3. Send `/login\r` to the master fd. Same two-write idiom isn't
   strictly required for a 6-byte string but matches setup-token.
4. The same OAuth URL detection logic from `_run_claude_login` works
   here — strip ANSI, regex for `https://claude.com/cai/oauth/...state=…`.
5. Wait for the paste-code from the worker's `_auth_code_queue` (same
   path the UI already uses). Send paste + 200ms sleep + CR.
6. Wait for the success indicator. From the binary strings the React
   tree transitions `waiting_for_login → processing → success` and
   shows "Claude Code login successful". Treat that as the signal.
7. **Critically different from setup-token:** `/login` runs `tGH(M)`,
   which writes the full credentials to `~/.claude/credentials.json`.
   The worker should:
   - Send `/quit\r` (or send Ctrl-D / Ctrl-C) to exit the REPL
     cleanly.
   - Tar up `~/.claude/` (legacy path) **or** read
     `~/.claude/credentials.json` and serialize that single file.
   - POST to `/auth/claude/credentials` so the next worker startup
     restores the full creds.
8. With creds on disk, `CLAUDE_CODE_OAUTH_TOKEN` is no longer the
   auth source — claude reads from the keychain/credentials file.
   Either continue setting the env var (if extractable) or rely on
   the file-based path. Restore should preserve permissions
   (`~/.claude/credentials.json` is 0600 on disk).

Risks / unknowns specific to Option A:

- Onboarding state machines change between Claude Code versions; the
  driver becomes a brittle scraper.
- The `/login` prompt may sub-prompt for "claude.ai vs Console" or
  "select organization" — those would each need detection + canned
  responses, exposed via the foreman UI when human input is needed.
- We need to be conservative about leaving an interactive `claude`
  process running after auth. Sending `/quit` and waiting for clean
  exit is required.

### Option B — SDK control protocol (`claude_authenticate`)

Cleaner, less brittle, but requires more upfront work to plumb the
control protocol. From the binary, the SDK protocol exposes:

```js
// pseudo-code from minified strings:
{type:"control_request", request:{subtype:"claude_authenticate",
  loginWithClaudeAi: true}}
// response: {manualUrl, automaticUrl}

{type:"control_request", request:{subtype:"claude_oauth_callback",
  authorizationCode: <code>, state: <state>}}
// internally calls handleManualAuthCodeInput()

{type:"control_request", request:{subtype:"claude_oauth_wait_for_completion"}}
// resolves when the flow settles, returns the account info
```

To use it the worker would:

1. Spawn `claude --print --input-format stream-json --output-format stream-json`
   (or similar — confirm the right flags via `claude --help` and the
   binary's argv parser).
2. Send a JSON-Lines control request `claude_authenticate` over the
   child's stdin pipe. No PTY required because everything is JSON.
3. Receive both URLs in the response; prefer `manualUrl` (the user is
   not on the same machine).
4. Wait for the user paste; split on `#` into `authorizationCode` /
   `state`; send `claude_oauth_callback` request.
5. Wait for `claude_oauth_wait_for_completion` resolution.
6. The flow internally calls `tGH(M)` so creds end up in
   `~/.claude/credentials.json` and disk-tar persistence works as
   before.
7. Tear down the child process cleanly.

Pros: structured I/O, no TUI parsing, identical credentials shape to
the legacy `claude auth login` path. Cons: the protocol is not
well-documented externally, so a chunk of work goes into mapping out
the request/response envelopes (`type`, `request_id`, `subtype`,
field names) by reading the binary strings and probably tcpdumping a
local SDK session that's known-good.

### Option C (don't recommend) — spoof the localhost callback

We considered this and it's a dead end: the token exchange uses a
redirect_uri that depends on which resolver fired. Hitting
`localhost:<port>/callback` makes claude send
`redirect_uri=http://localhost:<port>/callback` to Anthropic's token
endpoint, which won't match the original auth request that used the
manual URL (`https://platform.claude.com/oauth/code/callback`).
Anthropic rejects with a redirect_uri mismatch.

We could only make this work by also opening claude's auth flow with
the *automatic* URL (so the original request used the localhost
redirect) and then somehow getting the user's browser to redirect to
that localhost listener — which it can't reach across the docker
boundary. Not worth the complexity.

## Recommendation

Go with **Option B** (SDK control protocol) when full-scope auth is
needed. The PTY/Ink driver from `setup-token` is good enough for now,
but interactive `/login` scraping (Option A) is fragile across Claude
Code versions and onboarding changes; the SDK protocol is the
designed-for-machines interface and produces credentials in the same
shape as `claude auth login`, so the existing tarball persistence path
just works.

## Diagnostic harness

`worker/test_pty_login.py` is a standalone reproduction of the worker's
PTY+ctty harness, useful for debugging future Claude Code auth changes
without rebuilding the worker image:

```bash
docker cp worker/test_pty_login.py <worker-container>:/tmp/
docker exec -it <worker-container> python3 /tmp/test_pty_login.py \
    claude setup-token            # or `claude auth login`, or `claude`
```

If the script can't read input directly (e.g. it's running detached),
you can inject bytes via gdb on the parent process:

```bash
gdb -p <python-pid> --batch -nh \
  -ex 'call (long)write(6, "<paste>", <len>)' \
  -ex 'call (long)write(6, "\r", 1)' \
  -ex detach -ex quit
```

That's how we narrowed down the two-write idiom — the master fd is
fd 6 in the test script.
