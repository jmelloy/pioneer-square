# @pioneer-square/shared-skills

Interactive CLI versions of the Pioneer Square shared skills. The markdown guides in this
directory remain the source of truth; the CLI renders them and adds prompts.

## Install / run

```bash
npx @pioneer-square/shared-skills <command>   # one-off
npm i -g @pioneer-square/shared-skills        # provides `skills`, `debug-token`, `mcp-server-prod`, `issue-management`
```

## Commands

| Command | Purpose |
|---------|---------|
| `skills debug-token` | Build/run a read-only `/debug/query` call using `$DEBUG_TOKEN` (never prompted or printed) |
| `skills mcp-server-prod` | Prod safety guide plus a pre-flight checklist (non-zero exit if unconfirmed) |
| `skills issue-management [triage\|execution\|review]` | Foreman issue workflows with guided triage |

Every command supports `--help` and `--dry-run`; `mcp-server-prod` also accepts `--yes`.

## Development

```bash
npm install
npm test        # tsc build + node:test smoke tests
npm publish     # runs tests first (prepublishOnly)
```
