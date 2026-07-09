#!/usr/bin/env python3
"""Backfill structured GitHub linkage (issue_repo/issue_number/pr_repo/pr_number)
onto existing task rows.

Historically create_task recorded the issue/PR reference only in prose, so the
sidebar tree couldn't group those tasks. This fills NULL linkage columns from,
in order:

1. pr_url parse                        -> pr_repo / pr_number
2. cached PR body ``closes #N``        -> issue_repo / issue_number
3. cached PR head_ref == task.branch   -> pr_repo / pr_number
4. text heuristics on name+description: "PR #N" -> PR linkage (any phase);
   "owner/repo#N" / "#N" with PR references stripped -> issue linkage
5. parent_task_id propagation of issue linkage, both directions

Usage:
    python scripts/backfill_task_linkage.py [--guild <slug>] [--apply]

Dry-run by default; pass --apply to write. Run scripts/backfill_github_cache.py
first so steps 2-3 have PR bodies and branches to match against. Requires
DATABASE_URL (same variable the backend reads).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from database import AsyncSessionLocal  # noqa: E402
from models import GithubPullRequest, Guild, Task  # noqa: E402
from sqlalchemy import update  # noqa: E402
from sqlmodel import col, select  # noqa: E402

_PR_URL_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
_CLOSES_RE = re.compile(r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE)
_REPO_NUM_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b")
_PR_NUM_RE = re.compile(r"\bPR\s*#(\d+)\b", re.IGNORECASE)
_NUM_RE = re.compile(r"#(\d+)\b")


async def backfill_guild(db, guild_pk: int, slug: str, primary_repo: str | None) -> int:
    result = await db.exec(select(Task).where(col(Task.guild_id) == guild_pk))
    tasks = list(result.all())

    pr_result = await db.exec(select(GithubPullRequest))
    prs = list(pr_result.all())
    pr_by_key = {(p.repo, p.number): p for p in prs}
    # ponytail: head_ref can repeat within a repo across old PRs; latest number wins
    pr_by_branch: dict[tuple[str, str], GithubPullRequest] = {}
    for p in sorted(prs, key=lambda p: p.number):
        if p.head_ref:
            pr_by_branch[(p.repo, p.head_ref)] = p

    changes: dict[str, dict] = {}

    def stage(task: Task, **fields) -> None:
        pending = changes.setdefault(task.id, {})
        for k, v in fields.items():
            if getattr(task, k) is None and k not in pending and v is not None:
                pending[k] = v

    def effective(task: Task, field: str):
        return changes.get(task.id, {}).get(field) or getattr(task, field)

    guild_repos = {t.issue_repo for t in tasks if t.issue_repo} | {
        t.pr_repo for t in tasks if t.pr_repo
    }
    if primary_repo:
        guild_repos.add(primary_repo)

    for task in tasks:
        # 1. pr_url -> pr_repo/pr_number
        if task.pr_url and (m := _PR_URL_RE.match(task.pr_url)):
            stage(task, pr_repo=m.group(1), pr_number=int(m.group(2)))

        # 3. branch match against cached PR head_ref (guild repos only)
        if task.branch and effective(task, "pr_number") is None:
            for repo in guild_repos:
                pr = pr_by_branch.get((repo, task.branch))
                if pr:
                    stage(task, pr_repo=pr.repo, pr_number=pr.number)
                    break

        # 4a. "PR #N" in name/description -> PR linkage (any phase)
        text = f"{task.name or ''} {task.description or ''}"
        if effective(task, "pr_number") is None and (m := _PR_NUM_RE.search(text)):
            repo_m = _REPO_NUM_RE.search(text)
            repo = repo_m.group(1) if repo_m else primary_repo
            if repo:
                stage(task, pr_repo=repo, pr_number=int(m.group(1)))

        # 2. closes-ref from the cached PR body -> issue linkage
        pr_repo, pr_number = effective(task, "pr_repo"), effective(task, "pr_number")
        if pr_repo and pr_number and effective(task, "issue_number") is None:
            pr = pr_by_key.get((pr_repo, pr_number))
            if pr and pr.body and (m := _CLOSES_RE.search(pr.body)):
                stage(task, issue_repo=pr_repo, issue_number=int(m.group(1)))

        # 4b. issue reference in text, with PR references stripped so a lone
        # "PR #N" is never misread as issue N
        if effective(task, "issue_number") is None:
            issue_text = _PR_NUM_RE.sub(" ", text)
            if m := _REPO_NUM_RE.search(issue_text):
                stage(task, issue_repo=m.group(1), issue_number=int(m.group(2)))
            elif (m := _NUM_RE.search(issue_text)) and primary_repo:
                stage(task, issue_repo=primary_repo, issue_number=int(m.group(1)))

    # 5. propagate issue linkage through parent links until stable
    by_id = {t.id: t for t in tasks}
    changed = True
    while changed:
        changed = False
        for task in tasks:
            parent = by_id.get(task.parent_task_id) if task.parent_task_id else None
            if not parent:
                continue
            for src, dst in ((parent, task), (task, parent)):
                if effective(dst, "issue_number") is None and effective(src, "issue_number"):
                    stage(
                        dst,
                        issue_repo=effective(src, "issue_repo"),
                        issue_number=effective(src, "issue_number"),
                    )
                    changed = True

    changes = {tid: fields for tid, fields in changes.items() if fields}
    for tid, fields in sorted(changes.items()):
        task = by_id[tid]
        print(f"[{slug}] {tid} ({task.phase}, {(task.name or '')[:50]!r}) <- {fields}")
    print(f"[{slug}] {len(changes)}/{len(tasks)} tasks updated")

    if changes:
        for tid, fields in changes.items():
            await db.exec(update(Task).where(col(Task.id) == tid).values(**fields))
    return len(changes)


async def main(guild_slug: str | None, apply: bool) -> None:
    async with AsyncSessionLocal() as db:
        stmt = select(col(Guild.id), col(Guild.slug), col(Guild.primary_repo))
        if guild_slug:
            stmt = stmt.where(col(Guild.slug) == guild_slug)
        guilds = (await db.exec(stmt)).all()
        if not guilds:
            print(f"No guild found{f' for slug {guild_slug!r}' if guild_slug else ''}")
            return
        total = 0
        for guild_pk, slug, primary_repo in guilds:
            total += await backfill_guild(db, guild_pk, slug, primary_repo)
        if apply:
            await db.commit()
            print(f"Applied updates to {total} tasks.")
        else:
            await db.rollback()
            print(f"Dry run — would update {total} tasks. Pass --apply to write.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guild", default=None, help="guild slug (default: all guilds)")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    asyncio.run(main(args.guild, args.apply))
