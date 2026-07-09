#!/usr/bin/env python3
"""Run one full autonomous fleet cycle by chaining every tool's own CLI.

Order mirrors the authority chain each tool's CLAUDE.md already declares:

  1. myplanner plan     - refresh the recommended sequence (feeds MyOrchestrator's
                           ranking via its own plan-ledger; never dispatches).
  2. fleet_dispatch.py   - MyOrchestrator picks the next unit(s) of work, workers
                           close them as PRs (existing script, reused as-is).
  3. myresearcher brief  - brief a bounded number of open `my-researcher` topic
                           issues in MyThingsLab/study (billed: one Engine call
                           each, so capped by --brief-count).
  4. mytester run        - per repo, add coverage for one uncovered unit.
  5. mychangelogger update - per repo, fold new dev-ledger entries into CHANGELOG.md.
  6. mydocs sync         - refresh the fleet docs site from each tool's README/CLAUDE.md.
  7. myprojector sync    - reconcile the org Project board + tracking-issue checklist.
  8. myreporter post     - post a fleet-wide digest comment on the tracking issue.
  9. mytelegrambot notify - push everything since the last notify to Telegram.

No tool calls another tool's CLI directly (each stays a separate `gh`-attributed
run, per their CLAUDE.md invariants) -- this script is the external driver that
chains them, the same role fleet_dispatch.py already plays for orchestrator+workers.

Defaults to a dry run (report only, no mutating subcommands). Pass --execute to
actually run myresearcher/mytester/mychangelogger/mydocs/myprojector/myreporter/mytelegrambot
for real; fleet_dispatch's own --execute is passed through separately since it
spawns billed headless sessions.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent
ORG = "MyThingsLab"
TRACKING_REPO = f"{ORG}/my-things-core"
TRACKING_ISSUE = "1"
PROJECT_NUMBER = "1"
DOCS_SITE_CLONE = "mythingslab-site-genesis"

# The study repo is content, not a tool: myuni files topic issues there and
# myresearcher turns them into cited brief PRs. Like the my-<x> entries, `study`
# is a symlink to the sibling checkout (see .gitignore).
STUDY_REPO = f"{ORG}/study"
STUDY_ROOT = WORKSPACE_ROOT / "study"
RESEARCH_LABEL = "my-researcher"

# Repos that get a mytester/mychangelogger pass: every checkout with a
# pyproject.toml (shipped tools + the core SDK), discovered at runtime so a
# newly scaffolded tool joins the cycle without editing this list. Excludes
# my-template (a scaffold, not a real tool); non-Python repos have no
# pyproject.toml and never match.
EXCLUDED_REPOS = {"my-template"}


def tool_repos(root: Path) -> list[str]:
    return sorted(
        p.parent.name
        for p in root.glob("*/pyproject.toml")
        if p.parent.name not in EXCLUDED_REPOS
    )


def _run(cmd: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> int:
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=WORKSPACE_ROOT, env=env)
    if check and result.returncode != 0:
        raise SystemExit(result.returncode)
    return result.returncode


def _gh_json(argv: list[str]) -> list[dict] | None:
    result = subprocess.run(["gh", *argv], cwd=WORKSPACE_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"gh {' '.join(argv)} failed: {result.stderr.strip()}", file=sys.stderr)
        return None
    return json.loads(result.stdout)


def _select_brief_issues(
    open_issues: list[int], open_pr_branches: list[str], count: int
) -> list[int]:
    # A brief PR (`Closes #N` on branch my-researcher/N) closes its issue on
    # merge, so an open issue with a pending brief PR is already briefed —
    # re-briefing it would burn an Engine call for nothing.
    pending = {
        branch.removeprefix(f"{RESEARCH_LABEL}/")
        for branch in open_pr_branches
        if branch.startswith(f"{RESEARCH_LABEL}/")
    }
    return [n for n in sorted(open_issues) if str(n) not in pending][:count]


def _brief_candidates(count: int) -> list[int] | None:
    issues = _gh_json([
        "issue", "list", "--repo", STUDY_REPO, "--label", RESEARCH_LABEL,
        "--state", "open", "--json", "number", "--limit", "200",
    ])
    prs = _gh_json([
        "pr", "list", "--repo", STUDY_REPO, "--state", "open",
        "--json", "headRefName", "--limit", "200",
    ])
    if issues is None or prs is None:
        return None
    return _select_brief_issues(
        [i["number"] for i in issues], [p["headRefName"] for p in prs], count
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", required=True, help="passed through to fleet_dispatch.py --accounts")
    parser.add_argument("--execute", action="store_true", help="run mutating subcommands for real (all steps)")
    parser.add_argument("--dispatch-execute", action="store_true", help="also let fleet_dispatch spawn real headless sessions (separate from --execute since it's billed)")
    parser.add_argument("--engine", choices=["noop", "claude-cli"], default="noop", help="Engine backend for planner/tester/projector/reporter")
    parser.add_argument("--skip-dispatch", action="store_true", help="skip step 2 (fleet_dispatch), useful when it already ran this cycle")
    parser.add_argument("--brief-count", type=int, default=1, help="max open my-researcher topic issues in MyThingsLab/study to brief per cycle (one billed Engine call each with --engine claude-cli; 0 disables the step)")
    args = parser.parse_args(argv)

    py = sys.executable

    # 1. MyPlanner: refresh the recommended sequence.
    _run([
        "myplanner", "plan",
        "--org", ORG,
        "--repo-root", str(WORKSPACE_ROOT),
        "--tracking-repo", TRACKING_REPO,
        "--tracking-issue", TRACKING_ISSUE,
        "--engine", args.engine,
    ])

    # 2. MyOrchestrator + workers: pick and close the next unit(s) of work.
    if not args.skip_dispatch:
        dispatch_cmd = [py, str(WORKSPACE_ROOT / "fleet_dispatch.py"), "--accounts", args.accounts]
        if args.dispatch_execute:
            dispatch_cmd.append("--execute")
        _run(dispatch_cmd)

    # 3. MyResearcher: brief a bounded number of open study topic issues.
    if args.brief_count > 0:
        if not STUDY_ROOT.exists():
            print(f"(skipping myresearcher briefs — no study clone at {STUDY_ROOT})")
        else:
            candidates = _brief_candidates(args.brief_count)
            if candidates is None:
                print(f"(skipping myresearcher briefs — could not query {STUDY_REPO})")
            elif not candidates:
                print(f"(no open {RESEARCH_LABEL} issues in {STUDY_REPO} left to brief)")
            else:
                # ClaudeCLIEngine needs an authenticated CLI; borrow the first
                # fleet account's CLAUDE_CONFIG_DIR (TAVILY_API_KEY is not set
                # on this host, so retrieval sticks to keyless arXiv).
                account = args.accounts.split(",")[0].strip()
                env = {**os.environ, "CLAUDE_CONFIG_DIR": str(Path(account).expanduser())}
                for number in candidates:
                    brief_cmd = [
                        "myresearcher", "brief",
                        "--issue", str(number),
                        "--repo", STUDY_REPO,
                        "--repo-root", str(STUDY_ROOT),
                        "--engine", args.engine,
                        "--sources", "arxiv",
                    ]
                    if args.execute:
                        _run(brief_cmd, env=env)
                    else:
                        print(f"(dry run — would run: {' '.join(brief_cmd)})")

    # 4-5. Per repo: add one test, fold the ledger into CHANGELOG.md.
    for repo in tool_repos(WORKSPACE_ROOT):
        repo_path = WORKSPACE_ROOT / repo
        tester_cmd = ["mytester", "run", "--source", str(repo_path), "--engine", args.engine]
        if not args.execute:
            tester_cmd.append("--local-only")
        _run(tester_cmd)

        changelogger_cmd = ["mychangelogger", "update", "--source", str(repo_path)]
        if args.execute:
            _run(changelogger_cmd)
        else:
            print(f"(dry run — would run: {' '.join(changelogger_cmd)})")

    # 6. MyDocs: refresh the fleet docs site from each tool's README/CLAUDE.md.
    # Deterministically skips fresh pages (hash check), so this is cheap when
    # nothing changed; it opens (never merges) one PR when pages are stale.
    docs_site_root = WORKSPACE_ROOT / DOCS_SITE_CLONE
    docs_cmd = [
        "mydocs", "sync", "--all",
        "--repo-root", str(docs_site_root),
        "--engine", args.engine,
    ]
    if not docs_site_root.is_dir():
        print(f"(skipping mydocs — no local docs-site clone at {docs_site_root})")
    elif args.execute:
        _run(docs_cmd)
    else:
        print(f"(dry run — would run: {' '.join(docs_cmd)})")

    # 7. MyProjector: reconcile the board + tracking-issue checklist.
    projector_cmd = [
        "myprojector", "sync",
        "--org", ORG,
        "--project-number", PROJECT_NUMBER,
        "--tracking-repo", TRACKING_REPO,
        "--tracking-issue", TRACKING_ISSUE,
        "--engine", args.engine,
    ]
    if args.execute:
        projector_cmd.append("--apply-checklist")
    else:
        projector_cmd.append("--dry-run")
    _run(projector_cmd)

    # 8. MyReporter: post the fleet-wide digest on the tracking issue.
    reporter_cmd = [
        "myreporter", "post",
        "--repo", TRACKING_REPO,
        "--issue", TRACKING_ISSUE,
        "--repo-root", str(WORKSPACE_ROOT),
        "--summarize",
        "--engine", args.engine,
    ]
    if args.execute:
        _run(reporter_cmd)
    else:
        print(f"(dry run — would run: {' '.join(reporter_cmd)})")

    # 9. MyTelegramBot: push everything since the last notify.
    if args.execute:
        _run(["mytelegrambot", "notify"])
    else:
        print("(dry run — would run: mytelegrambot notify)")

    if not args.execute:
        print("\n(dry run — pass --execute to run myresearcher/mytester/mychangelogger/mydocs/myprojector/myreporter/mytelegrambot for real; --dispatch-execute for fleet_dispatch's billed sessions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
