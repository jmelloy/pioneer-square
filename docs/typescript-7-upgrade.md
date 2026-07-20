# TypeScript 7 upgrade — blocked

> Investigated: 2026-07-20 · `frontend/` at commit ebd2e5a
> Point-in-time findings, not living documentation — re-check versions before retrying.

## Summary

Upgrading `frontend`'s TypeScript from `6.0.3` to `7.0.2` is **not currently viable**. It's blocked
by both a resolvable peer-dependency conflict and, more importantly, two hard runtime crashes in
tools the build depends on. This picks up from #950 / #951 (which stopped Dependabot from
proposing the bump) and documents what was found on a manual attempt.

## What was tried

```
npm install typescript@latest --save-dev --legacy-peer-deps
```

`npm install typescript@latest` alone fails with `ERESOLVE` — see below. Forcing it through with
`--legacy-peer-deps` installs cleanly, but two of the three consumers of `typescript` then break.

## Blockers found

### 1. `typescript-eslint@8.64.0` peer dependency caps at `<6.1.0` (known, see #950)

```
npm error peer typescript@">=4.8.4 <6.1.0" from typescript-eslint@8.64.0
```

Pulled in transitively via `@vue/eslint-config-typescript@14.9.0`. Checked the `canary` dist-tag
of `typescript-eslint` (8.64.1-alpha.12) — it still declares the same `<6.1.0` peer range, so this
isn't close to landing upstream. `@vue/eslint-config-typescript` itself already declares an open
`typescript: >=4.8.4` peer range, so it is not the blocker — `typescript-eslint` is.

### 2. `typescript-eslint` / `typescript-estree` crashes at runtime under TS7 (new finding)

Even bypassing the peer-dep check with `--legacy-peer-deps`, running `npm run lint:check` crashes:

```
TypeError: Cannot read properties of undefined (reading 'Cjs')
  at .../typescript-estree/dist/create-program/getWatchProgramsForProjects.js:45
```

This means the `<6.1.0` peer range isn't just an overcautious version bound — `typescript-estree`
reaches into TypeScript internals (`ts.ModuleKind` in this case) that TS7 restructured. Linting is
actually broken under TS7 today, not merely blocked by a version check.

### 3. `vue-tsc@3.3.7` (latest) crashes at runtime under TS7 (new finding)

`npm run type-check` (and therefore `npm run build`, which runs `vue-tsc -b && vite build`) fails
immediately:

```
Error [ERR_PACKAGE_PATH_NOT_EXPORTED]: Package subpath './lib/tsc' is not defined by "exports"
in .../node_modules/typescript/package.json
```

TypeScript 7 changed its package layout / `exports` map and no longer exposes the `lib/tsc`
subpath that `vue-tsc` requires directly. `vue-tsc@3.3.7` is the latest published version and
already declares `typescript: >=5.0.0` as its peer range, so this isn't a version-pinning problem
either — it's a real incompatibility that needs a `vue-tsc` release to fix.

### What still works

- `vite build` (esbuild-based transform, doesn't invoke `tsc`/`vue-tsc`) — succeeds.
- `vitest run` (also esbuild-based transform) — 112/116 tests pass, 4 skipped, same as before.

So the app still bundles and the test suite still runs under TS7 — only **type-checking** and
**linting** are broken.

## Conclusion

Do not upgrade `typescript` in `frontend/` until both:
- `typescript-eslint` (and its `typescript-estree` dependency) support TS7's internal API changes
  and lift the `<6.1.0` peer cap, and
- `vue-tsc` ships a release compatible with TS7's new `exports` map.

Dependabot is already configured (`.github/dependabot.yml`) to ignore `typescript >=7.0.0`. Revisit
by re-running the steps in this doc — if `npm run lint:check` and `npm run build` succeed after
`npm install typescript@latest --save-dev`, the ecosystem has caught up.
