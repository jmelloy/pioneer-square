import test from "node:test";
import assert from "node:assert/strict";
import { main, commands, topHelp } from "../src/cli.js";
import { parseArgs, section, loadDoc, scriptedPrompter } from "../src/lib.js";

const capture = async (fn: () => Promise<number>) => {
  const lines: string[] = [];
  const orig = console.log;
  console.log = (...a) => void lines.push(a.join(" "));
  try { return { code: await fn(), out: lines.join("\n") }; } finally { console.log = orig; }
};

test("parseArgs handles flags and values", () => {
  const f = parseArgs(["triage", "--dry-run", "--url=http://x", "--sql", "select 1"]);
  assert.equal(f.dryRun, true);
  assert.equal(f.values.url, "http://x");
  assert.equal(f.values.sql, "select 1");
  assert.deepEqual(f.rest, ["triage"]);
});

test("top-level help lists all three commands", () => {
  const h = topHelp();
  for (const n of ["debug-token", "mcp-server-prod", "issue-management"]) assert.match(h, new RegExp(n));
  assert.equal(commands.length, 3);
});

test("each command supports --help", async () => {
  for (const c of commands) {
    const r = await capture(() => main(["--help"], c.name));
    assert.equal(r.code, 0);
    assert.match(r.out, /Usage:/);
  }
});

test("section extracts markdown sections from shipped docs", () => {
  assert.match(section(loadDoc("issue-management"), "1. Triage"), /Categorise/);
});

test("debug-token --dry-run prints curl and never the token", async () => {
  process.env.DEBUG_TOKEN = "s3cret";
  const r = await capture(() => main(["debug-token", "--dry-run", "--url=http://localhost:8000", "--sql", "select 1"]));
  assert.equal(r.code, 0);
  assert.match(r.out, /curl .*\/debug\/query/);
  assert.doesNotMatch(r.out, /s3cret/);
});

test("debug-token rejects non-SELECT SQL", async () => {
  process.env.DEBUG_TOKEN = "x";
  const r = await capture(() => main(["debug-token", "--dry-run", "--url=http://localhost:8000", "--sql", "delete from tasks"]));
  assert.equal(r.code, 2);
});

test("mcp-server-prod fails when checklist unconfirmed, passes with --yes", async () => {
  const cmd = commands.find((c) => c.name === "mcp-server-prod")!;
  const out: string[] = [];
  assert.equal(await cmd.run(parseArgs([]), scriptedPrompter(["n", "y", "y", "y"]), (s) => out.push(s)), 1);
  assert.equal(await cmd.run(parseArgs(["--yes"]), scriptedPrompter([]), (s) => out.push(s)), 0);
});

test("issue-management prints requested phase and rejects unknown", async () => {
  const cmd = commands.find((c) => c.name === "issue-management")!;
  const out: string[] = [];
  assert.equal(await cmd.run(parseArgs(["review"]), scriptedPrompter([]), (s) => out.push(s)), 0);
  assert.match(out.join("\n"), /Review/);
  assert.equal(await cmd.run(parseArgs(["bogus"]), scriptedPrompter([]), (s) => out.push(s)), 2);
});
