#!/usr/bin/env python3
"""Run one full autonomous fleet cycle by chaining every tool's own CLI.

Order mirrors the authority chain each tool's CLAUDE.md already declares:

  1. myplanner plan     - refresh the recommended sequence (feeds MyOrchestrator's
                           ranking via its own plan-ledger; never dispatches).
  2. fleet_dispatch.py   - MyOrchestrator picks the next unit(s) of work, workers
                           close them as PRs (existing script, reused as-is).
  3. mytester run        - per repo, add coverage for one uncovered unit.
  4. mychangelogger update - per repo, fold new dev-ledger entries into CHANGELOG.md.
  5. mydocs sync         - refresh the fleet docs site from each tool's README/CLAUDE.md.
  6. myprojector sync    - reconcile the org Project board + tracking-issue checklist.
  7. myreporter post     - post a fleet-wide digest comment on the tracking issue.
  8. mytelegrambot notify - push everything since the last notify to Telegram.

No tool calls another tool's CLI directly (each stays a separate `gh`-attributed
run, per their CLAUDE.md invariants) -- this script is the external driver that
chains them, the same role fleet_dispatch.py already plays for orchestrator+workers.

Defaults to a dry run (report only, no mutating subcommands). Pass --execute to
actually run mytester/mychangelogger/mydocs/myprojector/myreporter/mytelegrambot for
real; fleet_dispatch's own --execute is passed through separately since it
spawns billed headless sessions.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent
ORG = "MyThingsLab"
TRACKING_REPO = f"{ORG}/my-things-core"
TRACKING_ISSUE = "1"
PROJECT_NUMBER = "1"
DOCS_SITE_CLONE = "mythingslab-site-genesis"

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


def _run(cmd: list[str], *, check: bool = False) -> int:
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=WORKSPACE_ROOT)
    if check and result.returncode != 0:
        raise SystemExit(result.returncode)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", required=True, help="passed through to fleet_dispatch.py --accounts")
    parser.add_argument("--execute", action="store_true", help="run mutating subcommands for real (all steps)")
    parser.add_argument("--dispatch-execute", action="store_true", help="also let fleet_dispatch spawn real headless sessions (separate from --execute since it's billed)")
    parser.add_argument("--engine", choices=["noop", "claude-cli"], default="noop", help="Engine backend for planner/tester/projector/reporter")
    parser.add_argument("--skip-dispatch", action="store_true", help="skip step 2 (fleet_dispatch), useful when it already ran this cycle")
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

    # 3-4. Per repo: add one test, fold the ledger into CHANGELOG.md.
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

    # 5. MyDocs: refresh the fleet docs site from each tool's README/CLAUDE.md.
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

    # 6. MyProjector: reconcile the board + tracking-issue checklist.
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

    # 7. MyReporter: post the fleet-wide digest on the tracking issue.
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

    # 8. MyTelegramBot: push everything since the last notify.
    if args.execute:
        _run(["mytelegrambot", "notify"])
    else:
        print("(dry run — would run: mytelegrambot notify)")

    if not args.execute:
        print("\n(dry run — pass --execute to run mytester/mychangelogger/mydocs/myprojector/myreporter/mytelegrambot for real; --dispatch-execute for fleet_dispatch's billed sessions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
