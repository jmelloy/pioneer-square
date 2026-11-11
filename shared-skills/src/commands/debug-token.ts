import { type Command, loadDoc, section } from "../lib.js";

export const debugToken: Command = {
  name: "debug-token",
  summary: "Interactive helper for using the operator DEBUG_TOKEN against /debug/query",
  help: `Usage: skills debug-token [--url <backend-url>] [--sql <query>] [--dry-run] [--help]

Walks through checking DEBUG_TOKEN availability and building a read-only /debug/query call.
The token is read from the DEBUG_TOKEN environment variable only; it is never prompted for,
printed, or stored.

Options:
  --url <url>   Backend URL (default: $PIONEER_BACKEND_URL or http://localhost:8000)
  --sql <query> SQL to run (read-only); prompted if omitted
  --dry-run     Print the curl command instead of executing the request
  -h, --help    Show this help`,
  async run(args, p, out) {
    const doc = loadDoc("debug-token");
    out(section(doc, "What it is"));
    if (!process.env.DEBUG_TOKEN) {
      out("\nDEBUG_TOKEN is not set in your environment, so this capability is not available to you.");
      out(section(doc, "How to obtain it"));
      if (!args.dryRun) return 1;
    }
    const url = args.values.url ?? (await p.ask("Backend URL", process.env.PIONEER_BACKEND_URL ?? "http://localhost:8000"));
    const sql = args.values.sql ?? (await p.ask("Read-only SQL", "SELECT id, status FROM tasks ORDER BY created_at DESC LIMIT 10"));
    if (!/^\s*(select|with)\b/i.test(sql)) {
      out("Refusing: only read-only SELECT/WITH queries are allowed.");
      return 2;
    }
    const body = JSON.stringify({ query: sql });
    const cmd = `curl -sS -X POST "${url.replace(/\/$/, "")}/debug/query" -H "X-Debug-Token: $DEBUG_TOKEN" -H "Content-Type: application/json" -d '${body.replace(/'/g, "'\\''")}'`;
    if (args.dryRun) { out(`\n${cmd}`); return 0; }
    const res = await fetch(`${url.replace(/\/$/, "")}/debug/query`, {
      method: "POST",
      headers: { "X-Debug-Token": process.env.DEBUG_TOKEN!, "Content-Type": "application/json" },
      body,
    });
    out(`HTTP ${res.status}${res.status === 404 ? " (debug routes are mounted only when DEBUG_TOKEN is set on the backend)" : ""}`);
    out(await res.text());
    return res.ok ? 0 : 1;
  },
};
