#!/usr/bin/env python3
"""Analyze PR merge stats for jmelloy/pioneer-square using gh CLI."""

import json
import subprocess
from collections import defaultdict
from datetime import datetime


def fetch_prs():
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            "jmelloy/pioneer-square",
            "--state",
            "all",
            "--json",
            "number,title,state,mergedAt,createdAt,closedAt,author,headRefName",
            "--limit",
            "1000",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def parse_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def branch_prefix(name):
    if not name:
        return "manual"
    parts = name.split("/")
    if len(parts) >= 2:
        prefix = parts[0] + "/"
        known = {"claude/", "fix/", "feat/", "feature/", "chore/", "refactor/", "docs/", "test/"}
        return prefix if prefix in known else "other/"
    return "manual"


def main():
    prs = fetch_prs()

    total = len(prs)
    merged = [p for p in prs if p["state"] == "MERGED"]
    closed_no_merge = [p for p in prs if p["state"] == "CLOSED" and not p.get("mergedAt")]
    open_prs = [p for p in prs if p["state"] == "OPEN"]

    merge_rate = len(merged) / total * 100 if total else 0

    # By author
    author_counts = defaultdict(lambda: {"opened": 0, "merged": 0})
    for p in prs:
        login = p["author"]["login"] if p.get("author") else "unknown"
        author_counts[login]["opened"] += 1
        if p["state"] == "MERGED":
            author_counts[login]["merged"] += 1

    top_authors = sorted(author_counts.items(), key=lambda x: x[1]["opened"], reverse=True)[:10]

    # By branch prefix
    prefix_counts = defaultdict(lambda: {"opened": 0, "merged": 0})
    for p in prs:
        prefix = branch_prefix(p.get("headRefName", ""))
        prefix_counts[prefix]["opened"] += 1
        if p["state"] == "MERGED":
            prefix_counts[prefix]["merged"] += 1

    prefix_sorted = sorted(prefix_counts.items(), key=lambda x: x[1]["opened"], reverse=True)

    # Avg time open → merge (hours)
    merge_durations = []
    for p in merged:
        created = parse_dt(p.get("createdAt"))
        merged_at = parse_dt(p.get("mergedAt"))
        if created and merged_at:
            hours = (merged_at - created).total_seconds() / 3600
            merge_durations.append(hours)

    avg_merge_hours = sum(merge_durations) / len(merge_durations) if merge_durations else 0

    # PRs per ISO week
    week_counts = defaultdict(int)
    for p in prs:
        created = parse_dt(p.get("createdAt"))
        if created:
            iso = created.isocalendar()
            week_key = f"{iso.year}-W{iso.week:02d}"
            week_counts[week_key] += 1

    weeks_sorted = sorted(week_counts.items())

    # Print summary
    print("=" * 60)
    print("  Pioneer Square — PR Stats Summary")
    print("=" * 60)
    print(f"\nTotal PRs opened:           {total}")
    print(f"Merged:                     {len(merged)}")
    print(f"Closed without merging:     {len(closed_no_merge)}")
    print(f"Currently open:             {len(open_prs)}")
    print(f"Merge rate:                 {merge_rate:.1f}%")
    print(f"Avg time open → merge:      {avg_merge_hours:.1f} hours")

    print("\n--- Top 10 Authors ---")
    print(f"{'Author':<25} {'Opened':>7} {'Merged':>7} {'Rate':>7}")
    print("-" * 48)
    for login, counts in top_authors:
        rate = counts["merged"] / counts["opened"] * 100 if counts["opened"] else 0
        print(f"{login:<25} {counts['opened']:>7} {counts['merged']:>7} {rate:>6.0f}%")

    print("\n--- By Branch Prefix ---")
    print(f"{'Prefix':<15} {'Opened':>7} {'Merged':>7} {'Rate':>7}")
    print("-" * 38)
    for prefix, counts in prefix_sorted:
        rate = counts["merged"] / counts["opened"] * 100 if counts["opened"] else 0
        print(f"{prefix:<15} {counts['opened']:>7} {counts['merged']:>7} {rate:>6.0f}%")

    print("\n--- PRs Opened per ISO Week ---")
    for week, count in weeks_sorted:
        bar = "#" * min(count, 50)
        print(f"  {week}  {count:>4}  {bar}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
