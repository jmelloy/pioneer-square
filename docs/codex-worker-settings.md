# Codex worker settings

Workers now default to a workspace sandbox with command networking and write
access to the current repository's Git metadata. They do not disable the sandbox.

## Default policy

When `codex.args` / `PIONEER_CODEX_ARGS` / `--codex-arg` is empty, the runner
supplies task-scoped configuration overrides equivalent to:

```toml
approval_policy = "never"
default_permissions = "pioneer"

[permissions.pioneer]
extends = ":workspace"

[permissions.pioneer.network]
enabled = true

[permissions.pioneer.filesystem]
"/absolute/path/to/clone/.git" = "write"
# Add exact sibling worktrees only when this task needs to modify them.
```

The Git path is an example, not a literal setting. The runner resolves both
`git rev-parse --path-format=absolute --git-common-dir` and `--absolute-git-dir`
per invocation, including resumed turns. Only those Git directories are added;
sibling source worktrees are not automatically made writable.

A non-empty custom argument list replaces the generated permission defaults.
Configure the complete policy when overriding; do not mix named-profile and
legacy `sandbox_mode` / `sandbox_workspace_write` settings. The task's model
selection is independent and does not disable the defaults.

`never` means denied operations fail instead of requesting a human approval;
it does not grant access. Network access is needed for GitHub and dependencies.
A common Git-directory grant necessarily permits changes to metadata shared by
other worktrees; this is not strong per-task isolation.

## Authentication is separate

- Supply the API key through Codex's invocation-scoped `CODEX_API_KEY`, or use an
  intentional saved login. The runner maps configured / inherited `OPENAI_API_KEY`
  to `CODEX_API_KEY` unless an explicit `CODEX_API_KEY` is already present, and
  credential probing recognizes either variable.
- Supply task-scoped Git authentication to agent-issued Git commands. Passing
  `GH_TOKEN` / `GITHUB_TOKEN` alone authenticates `gh`, not plain Git. Worker-owned
  Git commands already inject an authentication header via `git_ops._auth_env`.
- Shell-environment filtering and Codex's own API authentication are different
  boundaries. Version 0.163.0 defaults to inheriting all variables without its
  automatic secret-name exclusions; do not assume model keys are hidden from
  subprocesses. Deliberately scope the subprocess environment.

## Evidence and limits

A disposable linked-worktree probe using local Codex **0.151.0** showed:

| Policy / operation | Result |
| --- | --- |
| `:workspace`, source edit | Allowed |
| `:workspace`, `git add` outside `/tmp` | `index.lock: Operation not permitted` |
| Explicit common Git-directory write grant, `git add` and `git commit` | Allowed |
| Same explicit grant, write to unrelated sibling outside `/tmp` | Denied |
| `:workspace`, GitHub connectivity | DNS failure |
| Named profile with `network.enabled=true`, GitHub connectivity | HTTP 200 |

The equivalent worktree fixture under `/tmp` allowed `git add` with the default
workspace profile. Default writable temporary directories matter: deployment
uses `/tmp/pioneer-work` and `/tmp/pioneer-repos`, so these tests do not establish
per-task isolation there. The same allow/deny and network checks were rerun using the runner's generated
arguments. Profile syntax and network compilation were checked against the pinned
0.163.0 source, but runtime probes were on macOS 0.151.0, not the deployed Linux
binary. Validate the deployed Linux version before rollout.

Executable regression tests in `worker/tests/test_codex_runner.py` cover the
fresh/resumed linked-worktree defaults (including quoted paths), explicit policy
overrides, API-key forwarding/precedence, and native-key credential detection.
The worker suite passes (405 tests); `ruff check . --fix` and `ruff format .` pass.

The saved deployed Codex transcript for `t-wh5k3f` recorded `read-only`, approval
`never`, and an OpenAI 401 for a missing authentication header. `t-3rblng` ran
Claude and had plain Git push authentication failures, not observed sandbox denials.

Workers share the backend's ECS task role (`terraform/ecs.tf`, `terraform/iam.tf`),
including S3 session-log access and ECS dispatch privileges. Containerization
alone does not make no-sandbox execution safe. Enabling networking also exposes
reachable credential endpoints; this profile does not replace IAM/egress isolation.
No deployment changes or authenticated GitHub push were performed.

## Primary sources

Pinned OpenAI Codex `rust-v0.163.0`:

- [Configuration schema](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/core/config.schema.json)
- [Permission-profile compilation](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/core/src/config/permissions.rs)
- [Authentication manager](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/login/src/auth/manager.rs)
- [Shell-environment defaults](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/config/src/shell_environment_policy.rs)
- [Noninteractive execution](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/exec/src/lib.rs)
