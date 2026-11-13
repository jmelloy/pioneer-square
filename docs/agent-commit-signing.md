# Autonomous GitHub App commit signing

Status: implemented and live CLI-tested locally; not deployed. Independent review is pending.

## Conclusion

**GitHub App signing is a viable documented path for autonomous, multi-user
workers without distributing personal signing keys.** GitHub signs API-created
commits; the worker does not hold the commit-signing private key.

**Use the REST Git Database API for the proposed publisher.** The live probe
confirmed that omitting author, committer, and signature on an installation-token
request produces a verified GitHub-signed commit while preserving executable
files, symlinks, and submodule entries. GraphQL `createCommitOnBranch` also signs,
but cannot express a new executable file's mode. See live results below.

For both APIs, the observed author was `pioneer-square[bot]` and the committer was
`GitHub` (`web-flow`), not the bot in both fields. An explicit `committer` on the REST
request makes GitHub skip signing (`reason: unsigned`). With no author the
publisher sends no committer and accepts `web-flow`. With a human `author` it also
sends the App bot as `committer`: omitted, GitHub copies the author into committer
and the App committer check fails. That commit is unsigned, so publication falls
back to unsigned Git. Set
`PIONEER_PUBLISH_LOG_LEVEL=INFO` to trace identity, token expiry, and the
create-commit verification response. Use an **installation access
token** for bot authorship, not a human OAuth token. GitHub explicitly documents
GraphQL signing and support for Apps. [1][2]

This supersedes the earlier recommendation to use a separate automation account
or per-user SSH keys for shared workers. No additional GitHub account is needed.
An App must still be configured and installed with appropriate repository access.

This is **not** a setting that makes existing `git commit` / `git push` operations
signed. API-created commits are new canonical Git objects. Merely pushing a
local bot-attributed commit with an installation token does not sign it. [1][3]

## Usage and rollout

Inside an assigned worker worktree, run:

```bash
pioneer publish -m "Describe this checkpoint"
```

App signing is optional: missing/unavailable App credentials (including an unset
guild installation ID or an unreachable credential service), or GitHub not verifying
the App commit, log a warning and fall back to ordinary unsigned Git commits/pushes
with existing task credentials. The unverified API commit is not published.
Authentication/authorization errors, identity or tree mismatches, and branch
conflicts remain fatal and do not trigger fallback.
Repository rules can still reject unsigned pushes; this feature does not bypass
them.

When App signing is available, the command stages all non-ignored work in that
repository, including deletions, and combines unpublished local commits into
**one commit per checkpoint**. It
verifies App authorship/signature and the exact Git tree before advancing the
remote ref. It then moves local HEAD using a compare-and-swap update; it does
not reset the index or working files. Previous local commits remain in the
reflog. Changes made while publication is in flight remain local changes.
It does not run Git commit/push hooks: run required validation explicitly.

All three runners receive the command instructions and `PIONEER_BACKEND_URL`,
`PIONEER_GUILD_ID`, `PIONEER_TASK_ID`, `PIONEER_AUTH_TOKEN`, and
`PIONEER_PUBLISH_BRANCH` in their task-scoped environment. These are not written
to global Git configuration. The command is intentionally unavailable outside
an assigned task unless an authorized operator supplies that context. The
worker's final checkpoint uses the same publisher for every acquired repository.
Review tasks do not receive publishing instructions and cannot obtain publishing
credentials. Only signing-availability errors trigger an ordinary Git push;
integrity and authorization failures remain errors.

Deployment setup:

1. Configure the backend App ID/private key through the existing secret path.
2. [Install Pioneer Square](https://github.com/apps/pioneer-square/installations/new),
   choose the account/org and destination repositories with Contents: write. Grant
   Workflows: write if tasks must modify `.github/workflows`; the scoped token
   includes that permission only when the installation has it.
3. Set the installation ID in **Guild settings > General > GitHub App
   Installation ID**. Find the number in the installation's Configure URL,
   `github.com/settings/installations/<id>` (or the org's installation settings).
   App signing uses this per-guild setting rather than a deployment-wide default.
   Leaving it blank logs a warning and uses unsigned Git; it does not stop tasks.
4. Deploy the new backend before upgrading workers (the publisher requires the
   new authenticated endpoint). Rebuild/restart worker images to ship the new
   CLI and shared task instructions. No new worker signing secret is needed.
5. Exercise an actual task before enabling signed-commit rules on task branches.
   Rule changes are not included here; instructions alone cannot prevent an
   unrestricted agent from making ordinary Git/API requests.

Implementation paths:

- `worker/pioneer_worker/publish.py`: CLI, per-worktree locking, snapshot upload,
  signature/tree verification, exact remote ref CAS, retry recovery, and safe
  local synchronization. Linux/macOS locking uses the standard-library `fcntl`.
- `cli/pioneer_cli/main.py`: `pioneer publish` dispatch.
- `backend/routes/auth.py`: `/auth/github/publish-token` requires the assigned
  worker, matching guild, active non-review task, allowed repository, and an
  explicitly configured guild installation. Responses are not cacheable.
- `backend/github_app_auth.py`: checks the repository's installation and mints
  repository-scoped App tokens, cached per installation/repository and renewed
  before expiry. No personal-token fallback.
- `worker/pioneer_worker/worker.py` and `github_pr.py`: task preflight,
  instructions/environment, and shared multi-repository fallback publication.
  Signing unavailability warns instead of aborting agent startup. The unsigned
  path stages/commits normally and pushes with task-user credentials; no App
  token is substituted for an authorization failure.

The worker receives a short-lived repository write capability, not a
cryptographically branch-restricted token. Task checks govern token issuance;
they do not revoke a token already obtained if a task later ends. Keep the App
private key backend-only and use repository rules to constrain writes. Backend-
mediated publication would be needed for a stricter task/branch security boundary.
Existing human-scoped PR/review credentials are unchanged.

Known limits: Git LFS pointer changes fail explicitly rather than publishing
without uploading their LFS objects. Unrelated remote changes, changed local
HEAD, and rejected ref updates fail without forcing or deleting work. The
release workflow is separate automation and is not changed by this agent-focused
implementation. Docker image startup and deployed-service rollout have not been
smoke-tested; the live CLI proof used an isolated local backend.

## API choices

### 1. GraphQL `createCommitOnBranch`: confirmed documented signing

The mutation:

- Appends one commit to an existing branch and advances that branch atomically.
- Requires `expectedHeadOid`, the expected prior branch tip.
- Accepts a message and multiple file additions/updates/deletions.
- Accepts complete base64-encoded file contents, including binary contents.
- Does not accept custom author or committer information, or a supplied
  signature. The credential owner determines authorship.
- Uses the existing branch tip as its single parent; it is not a general merge
  commit API. It cannot sign an existing local commit in place. [1][4]

Example request shape (illustrative, not executed):

```graphql
mutation PublishCheckpoint($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit {
      oid
      url
      author { name email user { login } }
      committer { name email user { login } }
      signature { isValid state wasSignedByGitHub }
      tree { oid }
    }
    ref { name target { oid } }
  }
}
```

```json
{
  "input": {
    "branch": {
      "repositoryNameWithOwner": "OWNER/REPO",
      "branchName": "pioneer/task-id"
    },
    "expectedHeadOid": "CURRENT_REMOTE_HEAD_SHA",
    "message": {
      "headline": "Implement task checkpoint",
      "body": "Pioneer-Task: t-example"
    },
    "fileChanges": {
      "additions": [{"path": "example.txt", "contents": "aGVsbG8K"}],
      "deletions": []
    }
  }
}
```

The schema's `FileAddition` has only `path` and `contents`: **no file mode, blob
SHA, or Git object type**. Its documentation supports file renames as deletion
plus addition; paths must be unique across both lists. Therefore do not assume
it can faithfully publish arbitrary codebase changes. The live probe confirmed
that a new intended executable becomes a regular `100644` file. Updating an
existing executable preserved `100755`; updating an existing symlink preserved
`120000`. Mode-only changes, new symlinks, new submodule gitlinks, and
file/directory replacements were not exercised through GraphQL. Unchanged
special entries are a separate case from creating or modifying them. [4]

No fixed per-mutation file-count/payload ceiling was established from the sources
read. Do not invent one or assume unlimited size. Base64 adds transfer overhead;
large files/changes and rate limits need testing. Git LFS content requires its
own upload path; transferring pointer files is not uploading LFS objects. [4][8]

### 2. REST Git Database: recommended after successful live probe

`POST /repos/{owner}/{repo}/git/commits` requires `message` and `tree`, accepts
`parents`, and allows omitting author, committer, and signature. Its docs default
authorship to the authenticated user and return signature verification details.
GitHub's general bot-signature documentation explicitly describes authenticated
App/bot requests without custom author/committer/signature fields. [3][5]

The REST Git Trees API represents executable files (`100755`), symlinks
(`120000`), and submodule entries (`160000`) and supports constructing a tree
from a base tree. The live probe created all three, plus a rename/deletion, and
the resulting tree ID matched an independently constructed local Git tree
exactly. The commit was verified even before updating the branch. [6]

The successful commit-creation body contained only `message`, `tree`, and
`parents`. Construct/upload blobs and trees, create the commit without identity
or signature overrides, verify it, then advance the branch without force. REST
object creation and ref update are separate operations; handle races and
unreachable objects safely. Endpoint-specific REST docs do not give the same
explicit automatic-signing promise as GraphQL, so retain signature verification
as a runtime check rather than assuming success from HTTP 201. [5][6]

### 3. REST Contents API: not the main publisher

The Contents API creates or updates one file per request and can default identity
to the authenticated user. It is not an atomic multi-file checkpoint interface.
Do not implement a source-code change as a series of unrelated per-file commits
just to get bot signing. Its actual signing result also needs a live check. [7]

## Repository integration findings

| Current seam | Impact |
| --- | --- |
| `backend/github_app_auth.py` | Already mints installation tokens and caches them per installation, refreshing near expiry. Keep the App JWT private key in the backend. |
| `backend/routes/auth.py::get_github_token` | Resolves the guild installation, but task-scoped requests prefer the triggering human's OAuth token. App publication selects installation credentials. The client logs signing unavailability before explicitly falling back to unsigned Git using existing task credentials. |
| `worker/pioneer_worker/worker.py::_refresh_github_token` | Configures local bot name/email, not signing. This does not turn local commits into verified bot commits. |
| `worker/pioneer_worker/worker.py` execute/follow-up prompts | Explicitly instruct agents to `git commit` and `git push` frequently. Changing only the end-of-task path would leave unsigned intermediate pushes. |
| `worker/pioneer_worker/worker.py` runner setup | Pins the agent's GitHub environment to the task token, often a human token. Bot publication and human PR/review authentication should be separate choices. |
| `worker/pioneer_worker/github_pr.py::push_branch` | Auto-commits dirty work and pushes local HEAD. Must use the same publishing policy as agent checkpoints. |
| `worker/pioneer_worker/git_ops.py` worktree helpers | Follow-ups/recovery attach to existing remote branches. Local history must match the canonical API-created history before continuing. |
| `backend/worker_runtime.py`, `Dockerfile`, `terraform/ecs.tf` | Docker/ECS already bootstrap workers against the backend. No SSH/GPG signing key or interactive signing agent would be needed for App publication. |
| `.github/workflows/release.yml` | Independently commits a version bump as `github-actions[bot]` using a deploy key for transport. This needs a separate App-authenticated publishing change if all automation commits must be verified. |

The App must be installed on the actual destination repository. A user's OAuth
access is not a substitute for App installation access, including on forks.
The REST write endpoints require Contents: write; workflow-file changes and
GraphQL permissions need explicit verification against the installed App.
Installation tokens expire after one hour and may be narrowed to repositories
and permissions. Existing token generation does not request narrower scope.
App signing does not bypass branch protection or rulesets. [5][9]

## Proposed architecture

Use a **worker-owned checkpoint publisher**, rather than asking each LLM to
implement GitHub requests correctly:

1. Resolve the task's guild, allowed repository, destination branch, and App
   installation. Do not let a worker choose an arbitrary installation ID.
2. Read the expected remote tip and collect the intended Git snapshot.
3. Publish through the selected API using an installation token, without custom
   author, committer, or signature fields.
4. Check signature validity and expected App authorship; compare the returned
   tree with the intended snapshot, not just the API success status.
5. Fetch the canonical commit and safely reconcile the worktree/local branch.
   Never discard dirty work or force-update a collaborator's branch.
6. Return the canonical SHA to task state/logs and subsequent follow-ups.

Keep commit-signing keys out of every worker. The App JWT private key remains
backend-only. Either execute publication in the backend with task/repository/
branch authorization, or give the publisher a short-lived, appropriately scoped
installation token. Backend-mediated publication provides a clearer trust
boundary; it also requires explicit transfer of snapshot data. Installing the
App on multiple tenants does not mean their workers may cross installation
boundaries. [9]

The implementation chooses **one remote commit per checkpoint**, explicitly
documented in CLI help and agent instructions. It combines unpublished local
commits, changes their IDs, and retains local reflog history. Changing the
instructions alone is not enforcement: unrestricted agents can still run
Git/HTTP directly. Use appropriate repository rules on intended branch patterns
and restrict bypass permissions if signed publication is a hard requirement.

Remote publication uses GraphQL `updateRefs` with `beforeOid`, `afterOid`, and
`force: false` for exact compare-and-swap of the ref after REST commit creation.
New refs use an all-zero expected prior OID. On an ambiguous timeout, the next
invocation recognizes its verified snapshot using task/source/tree message
trailers and synchronizes locally without publishing a duplicate checkpoint.
`clientMutationId` is not treated as an idempotency key. [4]

Task ownership remains in Pioneer and can be linked from commit messages.
`Co-authored-by` is optional credit, not proof that a human reviewed the work;
do not add it merely because a user requested a task. GitHub's actual signing
key is GitHub-managed: the verified record represents an authenticated API
operation, not a unique private signing key per worker. [1][3]

## Live results: 2026-11-09

The operator authorized disposable-branch writes in `jmelloy/pioneer-square`.
After refreshing the `jmelloy-gmail` AWS profile, the probe read the backend's
configured App identity and retrieved its SSM private key into process memory.
It did not write that private key or any token to disk or print their values.
Installation lookup confirmed App `pioneer-square`, installation `149025614`.
Each test token was explicitly limited to `jmelloy/pioneer-square` and
`contents:write`; the accessible repository list was checked before writing.

The successful run used branch
`pioneer-signing-probe-20261109T184113Z-12b02b`, based on
`b82b3a758552c158d1ef559bebe60cc11afd0e11`.

| Test | Result | Commit |
| --- | --- | --- |
| GraphQL text plus all 256 byte values | Valid signature; exact tree match | `9a66db3ed50d3f2c0a7d9200f391fb09c584d470` |
| REST new executable, symlink, submodule entry, rename/deletion | Valid signature before ref update; exact tree match; non-forced ref update succeeded | `743936dcceaab45ed5cf67dbc6789ba18fe1de52` |
| GraphQL new intended executable | Valid signature, but tree mismatch: created `100644` instead of `100755` | `e4a3904092970cfd9f98a9514bbd54e5d1d489f2` |
| GraphQL update existing executable | Valid signature; preserved `100755`; exact tree match | `bb477225bc44fcd2c7c79685e018fc654c28736b` |
| GraphQL update existing symlink | Valid signature; preserved `120000`; exact tree match | `122b5dc927597041f166d16d314e284d53cd4ef7` |
| GraphQL binary rename/deletion | Valid signature; exact tree match | `25a0a8cbb3204f42a4b6b3d62b6944875b9a35e1` |
| GraphQL stale expected head | Rejected with `STALE_DATA`; branch tip unchanged | No commit |

For every published commit, the probe checked REST verification
`verified: true`, `reason: valid`, App authorship, the expected single parent,
and the resulting branch tip. It fetched each canonical object into an isolated
temporary Git repository and confirmed a raw `gpgsig` header and the tree ID.
GraphQL also returned `signature.isValid: true` and
`signature.wasSignedByGitHub: true`. REST blob uploads used base64. Expected
snapshot trees were independently built with local `hash-object`, `read-tree`,
`update-index`, and `write-tree`, including exact Git modes.

Example independently rechecked result:
[REST verified App commit](https://github.com/jmelloy/pioneer-square/commit/743936dcceaab45ed5cf67dbc6789ba18fe1de52).
Deletion of a branch does not erase its commit objects, so these links may remain
accessible; cleanup means deleting refs, not purging GitHub's object storage.

Two preliminary attempts stopped because the local probe used a bare repository
with an index-removal command requiring a worktree. Both cleaned up their branch
and token before retrying. The corrected probe completed. All three temporary
branches were deleted and all three installation tokens revoked. A subsequent
matching-refs query returned no `pioneer-signing-probe-*` branches. Commit
messages included `[skip ci]`; `main`, existing task branches, App permissions,
SSM values, and deployed services were not changed.

The probe and non-secret JSON output were temporary local files. That initial
probe proved GitHub API signing and tree operations; the subsequent implementation
proof below also exercises the real CLI and backend credential route.

## Implementation proof

A second live test used the actual `pioneer publish` executable against an
isolated FastAPI backend with real worker/task authorization in the local test
Postgres database. Only the App configuration source was injected in memory;
the CLI obtained its scoped token through the implemented HTTP endpoint and
never received the App JWT private key.

- First checkpoint created verified App commit
  `22ce95b267bdf748822769b78d9bbf4281af4265`, including binary data, a new
  executable, and a symlink; local HEAD matched the remote and the worktree was
  clean.
- Second checkpoint created verified App commit
  `1f775962109bc53fdc258e727e3da98ba5b7163e`, including a mode-only executable
  change, binary rename, and file-to-directory transition. An intermediate local
  commit was combined into the checkpoint; its canonical parent was the first
  verified checkpoint.
- A third invocation reported no changes and created no duplicate commit.
- An explicit stale `updateRefs.beforeOid` request was rejected without changing
  the branch, validating the production CAS mechanism rather than only the
  simpler non-forced REST update used in the original probe.
- The test branch was deleted, its token revoked, and its isolated test DB rows
  removed. No deployment, real guild/task, or existing branch was changed.

After changing signing to optional, the real CLI/backend live proof was repeated:
verified commits `871bf328aaeea4b6b121c2fd2ae72165572d7af0` and
`963ef058bfcf0bc83af78b2ca377f40d798edf7e`, clean local synchronization,
no-change retry, and stale-ref rejection all passed again. Its temporary branch,
installation token, and isolated test DB rows were cleaned up.

Unsigned fallback is also tested through an actual Git push to an isolated local
bare repository, including a fresh worker with no Git identity configured and a
second invocation that creates no duplicate commit. Tests confirm warning logs,
existing task-token transport, and rejection of credential authorization errors.

Executable tests in `worker/tests/test_publish.py` use real Git trees and a
simulated GitHub API for signatures, tree mismatch, stale refs, ambiguous
response recovery, separate-clone follow-ups, detached linked worktrees,
concurrent publication locking, edits during publication, and LFS rejection.
`test_push_branch.py` checks shared fallback, startup rejection, and identical
instructions/context for Claude/Pi/Codex across execute/follow-up/plan phases.
`backend/tests/test_publish_auth.py` covers issuance authorization and fail-closed
credential behavior; client tests separately cover warning-and-unsigned fallback
for missing configuration/network outages and rejection of authorization errors.
`test_publish_credentials.py` covers repository/installation scope,
permission selection, caching, and renewal.

Validation on the final workspace:

- Backend suite: **1,576 passed, 3 skipped**.
- Worker suite after optional-signing revision: **465 passed** (includes
  concurrently added mise-setup tests).
- Focused publication/auth checks: **43 worker + 40 backend tests passed**.
- Frontend `npm run type-check` and Prettier checks passed for the updated
  settings hint/install link.
- `ruff check . --fix && ruff format .` and `git diff --check` passed.
- Standard-library `trace` line coverage for the selected implementation scope:
  publisher **315/332 (94.9%)**, App credential minting **36/36 (100%)**,
  credential endpoint **36/37 (97.3%)**, shared fallback **20/20 (100%)**;
  total **407/425 (95.8%)**. This is targeted line coverage, not whole-repository
  or branch coverage. Existing pytest suites report unawaited-coroutine and
  deprecation warnings; no test failures remain.

No designated specification index was found. This is locally implemented proof
for the requested behavior, not independently reviewed contract acceptance or
production readiness. No independent reviewer was delegated.

## Remaining deployment checks

- A real task with an actual LLM runner inside rebuilt Docker/ECS images;
  CLI/backend integration was tested locally, runner wiring through unit tests.
- Expiry/renewal and workflow-file permission changes against GitHub itself;
  cache renewal and tenant/repository authorization have automated tests.
- Signed-commit repository rules without App bypass, large payloads, and
  rate-limit behavior in deployed environments. Rate-limit errors currently fail
  visibly; retrying is explicit, not automatic.
- Live ambiguous-network recovery and competing workers: simulated failures,
  separate clones, per-worktree locking, and a live stale ref are covered, not a
  fault-injected deployed worker cluster.
- Directory-to-file transitions and real submodule checkout/configuration;
  file-to-directory transitions and Gitlink tree entries were verified.

The implementation deliberately linearizes each checkpoint rather than
preserving local merge topology. Git LFS upload support, release-workflow
signing, Windows locking, repository-rule changes, and production deployment
are not part of this change. No personal signing key is needed.

## Primary sources

[1]: https://docs.github.com/en/graphql/reference/commits#createcommitonbranch
[2]: https://github.blog/changelog/2021-09-13-a-simpler-api-for-authoring-commits/
[3]: https://docs.github.com/en/authentication/managing-commit-signature-verification/about-commit-signature-verification#signature-verification-for-bots
[4]: https://github.com/octokit/graphql-schema/blob/master/schema.graphql
[5]: https://docs.github.com/en/rest/git/commits#create-a-commit
[6]: https://docs.github.com/en/rest/git/trees#create-a-tree
[7]: https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents
[8]: https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api
[9]: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation

1. [GitHub GraphQL commit mutation: identity and automatic signing][1]
2. [GitHub announcement explicitly including GitHub Apps][2]
3. [GitHub bot verification requirements][3]
4. [Octokit public GitHub GraphQL schema: input shapes and signature fields][4]
5. [REST Git commit creation and verification response][5]
6. [REST Git trees and supported modes][6]
7. [REST Contents API][7]
8. [GraphQL primary and secondary rate limits][8]
9. [Installation authentication, permissions, token scope and expiration][9]
