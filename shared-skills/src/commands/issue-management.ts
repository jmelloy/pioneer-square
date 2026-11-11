import { type Command, loadDoc, section } from "../lib.js";

const PHASES = { triage: "1. Triage", execution: "2. Execution", review: "3. Review" } as const;

export const issueManagement: Command = {
  name: "issue-management",
  summary: "Issue triage / execution / review workflows (foreman patterns)",
  help: `Usage: skills issue-management [triage|execution|review] [--dry-run] [--help]

Prints the chosen workflow from the issue-management guide. If no phase is given,
you are prompted to choose one; triage also asks guiding questions to suggest an action.

Options:
  --dry-run    Print the workflow without prompting
  -h, --help   Show this help`,
  async run(args, p, out) {
    const doc = loadDoc("issue-management");
    let phase = args.rest[0];
    if (!phase) {
      if (args.dryRun) { out(section(doc, "Tool cheat-sheet")); return 0; }
      phase = await p.choose("Which phase?", Object.keys(PHASES));
    }
    if (!(phase in PHASES)) { out(`Unknown phase "${phase}". Use triage, execution, or review.`); return 2; }
    if (phase === "triage" && !args.dryRun) {
      const dup = await p.confirm("Does an existing issue already cover this?");
      if (dup) { out("→ Link to the existing issue instead of filing a new one (see 1.3)."); }
      else {
        const kind = await p.choose("Category?", ["bug", "feature", "refactor"]);
        out(`→ File a new ${kind} issue (see 1.5).`);
      }
      out("");
    }
    out(section(doc, PHASES[phase as keyof typeof PHASES]));
    return 0;
  },
};
