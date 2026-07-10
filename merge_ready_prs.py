#!/usr/bin/env python3
"""List (and optionally merge) every open PR across the org that's actually
mergeable: not a draft, no conflicts, every required check green.

This never runs on its own -- the user runs it by hand. Defaults to a dry
run (report only); pass --execute to actually merge. Uses a real merge
commit (`gh pr merge --merge`), matching the "Merge pull request #N from
..." shape already in every repo's history -- not squash, not rebase.

Never touches a draft PR (those aren't "ready" yet -- see fleet_dispatch.py's
own draft -> ready -> green -> merge shape) and never overrides a red/pending
check or a real conflict.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass

ORG = "MyThingsLab"


@dataclass(frozen=True)
class PR:
    repo: str
    number: int
    title: str
    is_draft: bool
    mergeable: str  # MERGEABLE | CONFLICTING | UNKNOWN
    merge_state: str  # CLEAN | BLOCKED | DIRTY | UNSTABLE | ...
    checks: list[dict]
    base: str = "main"
    head: str = ""

    @property
    def blocking_checks(self) -> list[str]:
        blockers = []
        for check in self.checks:
            conclusion = check.get("conclusion") or check.get("status")
            if conclusion not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
                blockers.append(f"{check.get('name', '?')}={conclusion}")
        return blockers

    @property
    def ready(self) -> bool:
        return (
            not self.is_draft
            and self.mergeable == "MERGEABLE"
            and self.merge_state == "CLEAN"
            and not self.blocking_checks
        )

    @property
    def reason_not_ready(self) -> str:
        if self.is_draft:
            return "still a draft"
        if self.mergeable == "CONFLICTING":
            return "has merge conflicts"
        if self.mergeable == "UNKNOWN":
            return "mergeability not yet computed by GitHub (re-run in a moment)"
        if self.blocking_checks:
            return f"checks not green: {', '.join(self.blocking_checks)}"
        if self.merge_state != "CLEAN":
            return f"mergeStateStatus={self.merge_state}"
        return "not ready"


def _run(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {proc.stderr.strip()}")
    return proc.stdout


def list_org_repos(org: str) -> list[str]:
    raw = _run(["gh", "repo", "list", org, "--limit", "200", "--json", "name"])
    return [obj["name"] for obj in json.loads(raw)]


def list_open_prs(repo: str) -> list[PR]:
    raw = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            f"{ORG}/{repo}",
            "--state",
            "open",
            "--json",
            "number,title,isDraft,mergeable,mergeStateStatus,statusCheckRollup,"
            "baseRefName,headRefName",
        ]
    )
    prs = []
    for obj in json.loads(raw):
        prs.append(
            PR(
                repo=repo,
                number=obj["number"],
                title=obj["title"],
                is_draft=obj["isDraft"],
                mergeable=obj["mergeable"],
                merge_state=obj["mergeStateStatus"],
                checks=obj.get("statusCheckRollup") or [],
                base=obj.get("baseRefName", "main"),
                head=obj.get("headRefName", ""),
            )
        )
    return prs


# Merging a stacked PR modifies its base PR's head: GitHub must recompute that
# PR's merge state ("Base branch was modified") and the synchronize-triggered CI
# run on the new head commit needs ~30s before the required check is green
# again ("not mergeable" / "not up to date"). All transient — wait and retry.
_TRANSIENT = ("Base branch was modified", "not mergeable", "not up to date")


def merge(pr: PR, *, retries: int = 4) -> None:
    for attempt in range(retries):
        try:
            _run(
                [
                    "gh",
                    "pr",
                    "merge",
                    str(pr.number),
                    "--repo",
                    f"{ORG}/{pr.repo}",
                    "--merge",
                ]
            )
            return
        except RuntimeError as exc:
            if any(t in str(exc) for t in _TRANSIENT) and attempt < retries - 1:
                time.sleep(15)
                continue
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default=ORG)
    parser.add_argument("--execute", action="store_true", help="actually merge; default is a dry run")
    parser.add_argument("--repo", action="append", help="limit to this repo (repeatable); default: every org repo")
    args = parser.parse_args(argv)

    repos = args.repo or list_org_repos(args.org)

    ready: list[PR] = []
    not_ready: list[PR] = []
    for repo in repos:
        try:
            for pr in list_open_prs(repo):
                (ready if pr.ready else not_ready).append(pr)
        except RuntimeError as exc:
            print(f"skipping {repo}: {exc}", file=sys.stderr)

    if not_ready:
        print("not ready:")
        for pr in not_ready:
            print(f"  {pr.repo}#{pr.number} {pr.title!r} — {pr.reason_not_ready}")
        print()

    if not ready:
        print("nothing mergeable right now")
        return 0

    print("mergeable:")
    for pr in ready:
        print(f"  {pr.repo}#{pr.number} {pr.title!r}")

    if not args.execute:
        print("\n(dry run — pass --execute to actually merge these)")
        return 0

    print()
    failures: list[str] = []
    for pr in ready:
        print(f"merging {pr.repo}#{pr.number}...")
        try:
            merge(pr)
        except RuntimeError as exc:
            # One stuck PR shouldn't strand the rest of the queue.
            print(f"  FAILED: {exc}", file=sys.stderr)
            failures.append(f"{pr.repo}#{pr.number}")
    if failures:
        print(f"\nfailed to merge: {', '.join(failures)} — re-run after checking them", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
