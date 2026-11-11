import { createInterface } from "node:readline/promises";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

export interface Flags {
  help: boolean;
  dryRun: boolean;
  yes: boolean;
  values: Record<string, string>;
  rest: string[];
}

/** Minimal argv parser: --help, --dry-run, --yes, --key=value / --key value. */
export function parseArgs(argv: string[]): Flags {
  const f: Flags = { help: false, dryRun: false, yes: false, values: {}, rest: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "-h" || a === "--help") f.help = true;
    else if (a === "--dry-run") f.dryRun = true;
    else if (a === "-y" || a === "--yes") f.yes = true;
    else if (a.startsWith("--")) {
      const [k, v] = a.slice(2).split("=", 2);
      if (v !== undefined) f.values[k] = v;
      else if (argv[i + 1] && !argv[i + 1].startsWith("--")) f.values[k] = argv[++i];
      else f.values[k] = "true";
    } else f.rest.push(a);
  }
  return f;
}

export interface Prompter {
  ask(question: string, def?: string): Promise<string>;
  choose(question: string, options: string[]): Promise<string>;
  confirm(question: string, def?: boolean): Promise<boolean>;
  close(): void;
}

export function createPrompter(): Prompter {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  const ask = async (q: string, def?: string) => {
    const a = (await rl.question(def ? `${q} [${def}]: ` : `${q}: `)).trim();
    return a || def || "";
  };
  return {
    ask,
    async choose(q, options) {
      options.forEach((o, i) => console.log(`  ${i + 1}) ${o}`));
      for (;;) {
        const a = await ask(q);
        const n = Number(a);
        if (Number.isInteger(n) && n >= 1 && n <= options.length) return options[n - 1];
        if (options.includes(a)) return a;
        console.log("Please enter a number from the list.");
      }
    },
    async confirm(q, def = false) {
      const a = (await ask(`${q} (y/n)`, def ? "y" : "n")).toLowerCase();
      return a.startsWith("y");
    },
    close: () => rl.close(),
  };
}

/** Scripted prompter for tests / non-interactive use. */
export function scriptedPrompter(answers: string[]): Prompter {
  const next = (def?: string) => answers.shift() ?? def ?? "";
  return {
    ask: async (_q, def) => next(def),
    choose: async (_q, options) => {
      const a = next();
      const n = Number(a);
      return Number.isInteger(n) && n >= 1 && n <= options.length ? options[n - 1] : options[0];
    },
    confirm: async (_q, def = false) => (next(def ? "y" : "n")).startsWith("y"),
    close: () => {},
  };
}

export function loadDoc(name: string): string {
  const here = dirname(fileURLToPath(import.meta.url));
  // dist/src/lib.js -> package root is two levels up
  return readFileSync(join(here, "..", "..", `${name}.md`), "utf8");
}

/** Extract a markdown section (by heading text prefix) up to the next heading of equal/higher level. */
export function section(doc: string, headingPrefix: string): string {
  const lines = doc.split("\n");
  const start = lines.findIndex((l) => /^#{1,6} /.test(l) && l.replace(/^#+ /, "").startsWith(headingPrefix));
  if (start < 0) return "";
  const level = lines[start].match(/^#+/)![0].length;
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i++) {
    const m = lines[i].match(/^(#+) /);
    if (m && m[1].length <= level) { end = i; break; }
  }
  return lines.slice(start, end).join("\n").trim();
}

export interface Command {
  name: string;
  summary: string;
  help: string;
  run(args: Flags, p: Prompter, out: (s: string) => void): Promise<number>;
}
