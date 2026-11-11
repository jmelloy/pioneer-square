#!/usr/bin/env node
import { type Command, createPrompter, parseArgs } from "./lib.js";
import { debugToken } from "./commands/debug-token.js";
import { mcpServerProd } from "./commands/mcp-server-prod.js";
import { issueManagement } from "./commands/issue-management.js";
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

export const commands: Command[] = [debugToken, mcpServerProd, issueManagement];

export function topHelp(): string {
  return `Usage: skills <command> [options]\n\nCommands:\n${commands
    .map((c) => `  ${c.name.padEnd(18)}${c.summary}`)
    .join("\n")}\n\nRun "skills <command> --help" for details.`;
}

export async function main(argv: string[], forced?: string): Promise<number> {
  const name = forced ?? (argv[0] && !argv[0].startsWith("-") ? argv[0] : undefined);
  const rest = forced || !name ? argv : argv.slice(1);
  const cmd = commands.find((c) => c.name === name);
  const args = parseArgs(rest);
  if (!cmd) {
    console.log(topHelp());
    return name ? 2 : args.help || argv.length === 0 ? 0 : 2;
  }
  if (args.help) { console.log(cmd.help); return 0; }
  const p = createPrompter();
  try { return await cmd.run(args, p, console.log); } finally { p.close(); }
}

const self = fileURLToPath(import.meta.url);
if (process.argv[1] && realpathSync(process.argv[1]) === self) {
  main(process.argv.slice(2)).then((c) => process.exit(c));
}
