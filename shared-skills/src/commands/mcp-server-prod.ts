import { type Command, loadDoc, section } from "../lib.js";

export const mcpServerProd: Command = {
  name: "mcp-server-prod",
  summary: "Production MCP safety guide with a pre-flight checklist",
  help: `Usage: skills mcp-server-prod [--dry-run] [--yes] [--help]

Shows the production-safety guidance for the MCP server, then runs an interactive
pre-flight checklist. Exits non-zero if any safety item is not confirmed.

Options:
  --dry-run    Print the guide and checklist without prompting
  -y, --yes    Assume "yes" to every checklist item
  -h, --help   Show this help`,
  async run(args, p, out) {
    const doc = loadDoc("mcp-server-prod");
    out(section(doc, "Safety precautions"));
    const items = [
      "Token is in an env var (not inline in scripts or shell history)?",
      "Queries are read-only and bounded by time and LIMIT?",
      "You understand the rate limits and quotas?",
      "No secrets or user PII will be pasted into chat or tickets?",
    ];
    if (args.dryRun) { items.forEach((i) => out(`[ ] ${i}`)); return 0; }
    let ok = true;
    for (const i of items) if (!(args.yes || (await p.confirm(i)))) { out(`  ✗ Not confirmed: ${i}`); ok = false; }
    if (!ok) return 1;
    out("\nChecklist complete. Common queries:\n");
    out(section(doc, "Common queries"));
    out("\n" + section(doc, "Example workflow"));
    return 0;
  },
};
