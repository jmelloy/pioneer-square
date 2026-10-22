# Codex worker settings

Recommendation from the October 22, 2026 investigation: keep a workspace sandbox,
explicitly enable command networking, and grant only task-required Git metadata
and sibling worktrees. Do not use full access as the default.

## Proposed policy

For the deployed Codex 0.163.0, generate a task-scoped Codex configuration like:

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

The Git path is an example, not a literal setting. Resolve it from the worktree
with `git rev-parse --path-format=absolute --git-common-dir`. Also grant the
`--absolute-git-dir` if it is outside that common directory. Generate these
paths per task rather than granting the entire repositories/worktrees parent.
Select this profile explicitly; do not mix it with legacy `sandbox_mode` /
`sandbox_workspace_write` overrides or a bypass flag.

`never` means denied operations fail instead of requesting a human approval;
it does not grant access. Network access is needed for GitHub and dependencies.
A common Git-directory grant necessarily permits changes to metadata shared by
other worktrees; this is not strong per-task isolation.

## Authentication is separate

- Supply the API key through Codex's invocation-scoped `CODEX_API_KEY`, or use an
  intentional saved login. The worker currently exports configured API keys as
  `OPENAI_API_KEY`; the adapter/probe needs to recognize the actual auth mode.
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
per-task isolation there. Profile syntax and network compilation were checked
against the pinned 0.163.0 source, but runtime probes were on macOS 0.151.0, not
the deployed Linux binary. Validate the pinned Linux version before rollout.

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
