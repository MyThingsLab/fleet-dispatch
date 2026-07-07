#!/usr/bin/env python3
"""Fan out ready fleet work across multiple Claude Code accounts.

Reuses myorchestrator's ranking (myorchestrator next --count N) to pick one
distinct candidate per available worker, then runs each in its own git
worktree (mythings.isolation.Workspace) with a headless `claude -p` session
under a different CLAUDE_CONFIG_DIR — so two subscriptions can work the fleet
concurrently without touching each other's files.

Only "issue" candidates are dispatchable today; "scaffold" candidates (a
not-yet-built tool) need MyScaffolder, which doesn't exist yet, so they're
reported and skipped.

Each run ends at "PR opened" — never pushes to main, never merges. Defaults to
--dry-run; pass --execute to actually spawn the headless sessions.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mythings.isolation import Workspace
from mythings.ledger import Ledger

from fleet_usage import SAFE_FAMILY_PATTERNS, UsageReport, family_for, parse_transcript
from myorchestrator.candidates import Candidate
from myorchestrator.manifest import default_manifest_path
from myorchestrator.orchestrator import Orchestrator, Recommendation

WORKSPACE_ROOT = Path(__file__).resolve().parent
DISPATCH_LEDGER = WORKSPACE_ROOT / ".fleet-dispatch" / "ledger.jsonl"
ALLOWED_TOOLS_PATH = WORKSPACE_ROOT / ".fleet-dispatch" / "allowed_tools.json"
TRANSCRIPTS_DIR = WORKSPACE_ROOT / ".fleet-dispatch" / "transcripts"

DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Edit",
    "Write",
    "Bash(git *)",
    "Bash(pytest*)",
    "Bash(python -m pytest*)",
    "Bash(python3 -m pytest*)",
    "Bash(ruff*)",
    "Bash(python -m ruff*)",
    "Bash(python3 -m ruff*)",
    "Bash(gh issue view*)",
    "Bash(gh pr create*)",
]


def _utc_ts() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _load_allowed_tools() -> list[str]:
    if ALLOWED_TOOLS_PATH.exists():
        return json.loads(ALLOWED_TOOLS_PATH.read_text())
    ALLOWED_TOOLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALLOWED_TOOLS_PATH.write_text(json.dumps(DEFAULT_ALLOWED_TOOLS, indent=2))
    return list(DEFAULT_ALLOWED_TOOLS)


def _save_allowed_tools(tools: list[str], *, commit_message: str) -> None:
    ALLOWED_TOOLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALLOWED_TOOLS_PATH.write_text(json.dumps(tools, indent=2))
    # The commit itself *is* the audit trail for a self-edit -- git history
    # replaces the pre-git backup-copy approach, and `git revert` is the way
    # back out if a widened pattern turns out to be wrong. The ledger entry
    # that explains *why* rides along in the same commit.
    subprocess.run(
        ["git", "-C", str(WORKSPACE_ROOT), "add", str(ALLOWED_TOOLS_PATH), str(DISPATCH_LEDGER)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(WORKSPACE_ROOT), "commit", "-m", commit_message], check=True
    )


@dataclass(frozen=True)
class Account:
    name: str
    config_dir: Path


def _parse_accounts(raw: str) -> list[Account]:
    accounts = []
    for i, entry in enumerate(raw.split(",")):
        entry = entry.strip()
        if not entry:
            continue
        accounts.append(Account(name=f"account{i + 1}", config_dir=Path(entry).expanduser()))
    return accounts


def _prompt_for(candidate: Candidate) -> str:
    repo, number = candidate.id.split("#")
    return (
        f"Work issue #{number} in the {repo} repo (`gh issue view {number} --repo "
        f"MyThingsLab/{repo}` for the full description; title: {candidate.title!r}).\n\n"
        f"Follow this repo's own CLAUDE.md and HARNESS.md exactly. Make the smallest "
        f"change that closes the issue, with tests. Run the repo's test suite and "
        f"linter before finishing. Commit your work, then open a pull request with "
        f"`gh pr create` describing the change — do not push to main and do not "
        f"merge the PR yourself. Stay inside this repo; do not touch any other repo "
        f"in the workspace."
    )


def _branch_name(candidate: Candidate) -> str:
    return f"fleet-dispatch/{candidate.id.replace('#', '-')}"


def _record_usage(
    report: UsageReport, *, account: Account, candidate: Candidate, transcript_path: Path,
    ledger: Ledger,
) -> None:
    ledger.record(
        tool="fleet_dispatch",
        kind="usage",
        outcome="success",
        detail=f"{account.name} -> {candidate.id}: ${report.cost_usd:.4f}, "
        f"{report.num_turns} turns, {len(report.denials)} denials",
        candidate=candidate.id,
        account=account.name,
        cost_usd=report.cost_usd,
        input_tokens=report.input_tokens,
        output_tokens=report.output_tokens,
        cache_creation_input_tokens=report.cache_creation_input_tokens,
        cache_read_input_tokens=report.cache_read_input_tokens,
        num_turns=report.num_turns,
        wasted_output_tokens=report.wasted_output_tokens,
        denials_count=len(report.denials),
        transcript_path=str(transcript_path),
    )
    if report.denials:
        print(
            f"  [{account.name}] {len(report.denials)} permission denial(s), "
            f"~{report.wasted_output_tokens} output tokens wasted"
        )

    tools = _load_allowed_tools()
    all_added: list[str] = []
    for d in report.denials:
        family = family_for(d.command) if d.tool_name == "Bash" else None
        if family is None:
            ledger.record(
                tool="fleet_dispatch",
                kind="friction",
                outcome="needs_review",
                detail=f"unrecognized denied command, no auto-widen: {d.command!r}",
                candidate=candidate.id,
                turn=d.turn,
                preceding_reasoning=d.preceding_reasoning,
            )
            print(f"  [{account.name}] friction (needs human review): {d.command!r}")
            continue
        missing = [p for p in SAFE_FAMILY_PATTERNS[family] if p not in tools]
        if missing:
            tools.extend(missing)
            all_added.extend(missing)
            ledger.record(
                tool="fleet_dispatch",
                kind="self_edit",
                outcome="widened_allowlist",
                detail=f"auto-widened '{family}' family after a denial: added {missing}",
                candidate=candidate.id,
                added=missing,
                triggering_command=d.command,
                turn=d.turn,
                preceding_reasoning=d.preceding_reasoning,
            )
            print(f"  [{account.name}] self-widened allowlist ({family}): +{missing}")
    if all_added:
        _save_allowed_tools(
            tools,
            commit_message=(
                f"fleet_dispatch: auto-widen allowlist after {candidate.id} denials\n\n"
                f"Added: {all_added}\n"
                f"Triggered by {len(report.denials)} permission denial(s) dispatching "
                f"{account.name} -> {candidate.id}. See .fleet-dispatch/ledger.jsonl "
                f"(kind=self_edit) for the reasoning behind each addition."
            ),
        )


def _dispatch_one(
    account: Account, candidate: Candidate, *, execute: bool, max_budget_usd: float, ledger: Ledger
) -> None:
    repo, _number = candidate.id.split("#")
    repo_path = WORKSPACE_ROOT / repo
    branch = _branch_name(candidate)
    prompt = _prompt_for(candidate)

    print(f"\n=== {account.name} -> {candidate.id} ({repo}) ===")
    print(f"  branch: {branch}")
    print(f"  config: {account.config_dir}")
    print(f"  budget cap: ${max_budget_usd}")
    print(f"  prompt: {prompt}")

    if not execute:
        print("  [dry-run] not launched")
        return

    ledger.record(
        tool="fleet_dispatch",
        kind="dispatch",
        outcome="started",
        detail=f"{account.name} -> {candidate.id}",
        candidate=candidate.id,
        account=account.name,
        branch=branch,
    )

    with Workspace(repo_path, base_ref="main") as tree:
        subprocess.run(["git", "-C", str(tree), "checkout", "-b", branch], check=True)
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(account.config_dir)}
        result = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "stream-json",
                "--verbose",
                "--max-budget-usd",
                str(max_budget_usd),
                "--allowedTools",
                *_load_allowed_tools(),
            ],
            cwd=tree,
            env=env,
            capture_output=True,
            text=True,
        )

        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        transcript_path = TRANSCRIPTS_DIR / f"{branch.replace('/', '_')}-{_utc_ts()}.jsonl"
        transcript_path.write_text(result.stdout)
        report = parse_transcript(result.stdout.splitlines())
        _record_usage(
            report, account=account, candidate=candidate, transcript_path=transcript_path,
            ledger=ledger,
        )

        if result.returncode != 0:
            print(f"  [{account.name}] claude exited {result.returncode}; leaving branch for review")
            ledger.record(
                tool="fleet_dispatch",
                kind="dispatch",
                outcome="failed",
                detail=f"{account.name} -> {candidate.id}: claude exited {result.returncode}",
                candidate=candidate.id,
                account=account.name,
                branch=branch,
            )
            return
        push = subprocess.run(
            ["git", "-C", str(tree), "push", "-u", "origin", branch],
            capture_output=True,
            text=True,
        )
        if push.returncode != 0:
            print(f"  [{account.name}] push failed: {push.stderr.strip()}")
            ledger.record(
                tool="fleet_dispatch",
                kind="dispatch",
                outcome="failed",
                detail=f"{account.name} -> {candidate.id}: push failed: {push.stderr.strip()}",
                candidate=candidate.id,
                account=account.name,
                branch=branch,
            )
        else:
            print(f"  [{account.name}] pushed {branch}; PR should already be open via gh pr create")
            ledger.record(
                tool="fleet_dispatch",
                kind="dispatch",
                outcome="success",
                detail=f"{account.name} -> {candidate.id}: pushed {branch}",
                candidate=candidate.id,
                account=account.name,
                branch=branch,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--accounts",
        required=True,
        help="comma-separated CLAUDE_CONFIG_DIR paths, one per available worker "
        "(each must already be `claude auth login`'d)",
    )
    parser.add_argument("--execute", action="store_true", help="actually launch headless sessions")
    parser.add_argument("--org", default="MyThingsLab")
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=3.0,
        help="dollar cap passed to each headless claude session (default: $3)",
    )
    args = parser.parse_args(argv)

    accounts = _parse_accounts(args.accounts)
    if not accounts:
        parser.error("--accounts must list at least one CLAUDE_CONFIG_DIR")

    orch = Orchestrator(
        org=args.org,
        manifest_path=default_manifest_path(),
        repo_root=WORKSPACE_ROOT,
        ledger=Ledger(WORKSPACE_ROOT / "my-orchestrator" / ".mythings" / "ledger.jsonl"),
    )
    # Overfetch the ranked pool so a worker slot falls through to the next
    # dispatchable candidate instead of sitting idle behind an undispatchable
    # scaffold proposal.
    pool: list[Recommendation] = orch.next_n(max(len(accounts) * 5, 20))
    dispatchable = [r.chosen for r in pool if r.chosen is not None and r.chosen.kind == "issue"]
    skipped = [r.chosen for r in pool if r.chosen is not None and r.chosen.kind != "issue"]

    if skipped:
        names = ", ".join(c.id for c in skipped)
        print(f"skipping (need MyScaffolder, not built yet): {names}")

    dispatch_ledger = Ledger(DISPATCH_LEDGER)
    for account, candidate in zip(accounts, dispatchable):
        _dispatch_one(
            account,
            candidate,
            execute=args.execute,
            max_budget_usd=args.max_budget_usd,
            ledger=dispatch_ledger,
        )
    for account in accounts[len(dispatchable) :]:
        print(f"\n=== {account.name}: no ready issue candidate ===")

    if not args.execute:
        print("\n(dry run — pass --execute to actually launch these sessions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
