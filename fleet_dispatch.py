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
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
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

# Guards the read-modify-write of allowed_tools.json and its commit in
# WORKSPACE_ROOT: concurrent dispatches now run in parallel threads, and two
# threads self-widening the allowlist at once would race on the file and on
# `git commit` (a second commit while one is mid-flight fails on index.lock).
_ALLOWLIST_LOCK = threading.Lock()

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
    # Read-only inspection: workers reach for these to look around even though
    # they have native Read/Glob/Grep tools; allowing the non-mutating ones up
    # front stops a run from dead-ending on a denied `ls`/`grep` (see the
    # SAFE_FAMILY_PATTERNS note in fleet_usage.py). `find`/`rm`/`pip`/`python -c`
    # are intentionally absent — those can mutate or run code and stay friction.
    "Bash(ls*)",
    "Bash(cat*)",
    "Bash(head*)",
    "Bash(tail*)",
    "Bash(wc*)",
    "Bash(grep*)",
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


def _with_rtk_allowlist(tools: list[str]) -> list[str]:
    # rtk's hook rewrites `git status` -> `rtk git status` (it prepends `rtk `).
    # Verified against rtk 0.43.0: its PreToolUse hook returns `updatedInput`
    # only -- NO `permissionDecision: allow` -- so the rewritten command is NOT
    # self-allowed and must independently satisfy the worker's --allowedTools, or
    # a headless worker stalls on a denied command. The denial auto-widen in
    # _record_usage can't recover it either (it would re-add `Bash(git *)`, not
    # the `rtk`-prefixed form). Mirror each Bash(X) entry with Bash(rtk X) so the
    # compact form is allowed exactly where the original was, never broader.
    mirrored = list(tools)
    for t in tools:
        if t.startswith("Bash(") and t.endswith(")"):
            inner = t[len("Bash(") : -1]
            mirrored.append(f"Bash(rtk {inner})")
    return mirrored


def _save_allowed_tools(tools: list[str], *, commit_message: str) -> None:
    ALLOWED_TOOLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALLOWED_TOOLS_PATH.write_text(json.dumps(tools, indent=2))
    # The commit itself *is* the audit trail for a self-edit -- git history
    # replaces the pre-git backup-copy approach, and `git revert` is the way
    # back out if a widened pattern turns out to be wrong. The ledger entry
    # that explains *why* rides along in the same commit.
    #
    # Commit with an explicit pathspec, NOT a bare `git commit`: WORKSPACE_ROOT
    # is a live checkout that may have unrelated staged changes, and a bare
    # commit would sweep them into this self-edit. The pathspec form commits a
    # snapshot of exactly these two files and leaves anything else staged alone.
    subprocess.run(
        ["git", "-C", str(WORKSPACE_ROOT), "add", str(ALLOWED_TOOLS_PATH), str(DISPATCH_LEDGER)],
        check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(WORKSPACE_ROOT), "commit", "-m", commit_message,
            "--", str(ALLOWED_TOOLS_PATH), str(DISPATCH_LEDGER),
        ],
        check=True,
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


def _config_dir_has_rtk_hook(config_dir: Path) -> bool:
    # rtk installs itself with `rtk init -g` into a CLAUDE_CONFIG_DIR: it writes
    # a PreToolUse hook to settings.json that rewrites commands to their compact
    # `rtk` equivalents. We never write that hook ourselves — rtk owns it, and
    # its schema is versioned — we only read settings.json to confirm a worker
    # spawned under this dir will actually inherit the compression. The hook is
    # self-guarding (exits 0 if rtk/jq is missing), so this check is about
    # "compression is wired", not safety.
    settings = config_dir / "settings.json"
    if not settings.is_file():
        return False
    try:
        data = json.loads(settings.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    hooks = data.get("hooks", {}).get("PreToolUse", [])
    return "rtk" in json.dumps(hooks)


def _preflight_rtk(accounts: list[Account]) -> list[str]:
    # Read-only. Returns human-readable problems; an empty list means rtk
    # compression is correctly wired for every account. Refusing to --execute
    # on a non-empty result is the point: a paid run must never silently skip
    # the compression you asked for.
    problems = []
    if shutil.which("rtk") is None:
        problems.append("`rtk` is not on PATH — install it and run `rtk init -g --hook-only`")
    for account in accounts:
        if not _config_dir_has_rtk_hook(account.config_dir):
            problems.append(
                f"{account.name} ({account.config_dir}) has no rtk PreToolUse hook — "
                f"run `CLAUDE_CONFIG_DIR={account.config_dir} rtk init -g --hook-only`"
            )
    return problems


def _prompt_for(candidate: Candidate) -> str:
    repo, number = candidate.id.split("#")
    return (
        f"Work issue #{number} in the {repo} repo (`gh issue view {number} --repo "
        f"MyThingsLab/{repo}` for the full description; title: {candidate.title!r}).\n\n"
        f"You are running fully non-interactively, as a headless `claude -p` "
        f"session: no human is watching and no one can approve a permission "
        f"prompt. If a command is denied, do NOT ask for approval or wait for it — "
        f"it will never come. Work only with the tools you already have, and prefer "
        f"your Read, Edit, Write, Glob and Grep tools over shelling out to `ls`, "
        f"`cat`, `find` or `grep` to inspect the repo.\n\n"
        f"Follow this repo's own CLAUDE.md and HARNESS.md exactly. Make the smallest "
        f"change that closes the issue, with tests. Run the repo's test suite and "
        f"linter before finishing. Commit your work, then open a pull request with "
        f"`gh pr create` describing the change — do not push to main and do not "
        f"merge the PR yourself. Stay inside this repo; do not touch any other repo "
        f"in the workspace."
    )


def _dispatch_outcome(n_commits: int, pr_number: int | None) -> tuple[str, str]:
    # Translates what actually landed into an honest ledger outcome. A headless
    # worker exiting 0 is NOT proof it did the work -- it may have given up (e.g.
    # asked for a permission approval no one was there to grant). "success"
    # requires a real commit AND an open PR; anything less says so plainly.
    if n_commits == 0:
        return "no_changes", "worker committed nothing; branch left unpushed"
    if pr_number is None:
        return "needs_review", "committed but no PR was opened; branch pushed for review"
    return "success", f"opened PR #{pr_number}"


def _open_pr_number(org: str, repo: str, branch: str) -> int | None:
    result = subprocess.run(
        [
            "gh", "pr", "list", "--repo", f"{org}/{repo}", "--head", branch,
            "--state", "open", "--json", "number", "--jq", ".[0].number // empty",
        ],
        capture_output=True,
        text=True,
    )
    out = result.stdout.strip()
    return int(out) if out.isdigit() else None


def _branch_name(candidate: Candidate) -> str:
    return f"fleet-dispatch/{candidate.id.replace('#', '-')}"


def _record_usage(
    report: UsageReport, *, account: Account, candidate: Candidate, transcript_path: Path,
    ledger: Ledger, rtk: bool = False,
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
        # Marks whether rtk output compression was active for this run, so
        # rtk-on vs rtk-off `kind=usage` entries can be diffed after the fact --
        # the "measure it, don't assume it" half of the rtk integration.
        rtk=rtk,
    )
    if report.denials:
        print(
            f"  [{account.name}] {len(report.denials)} permission denial(s), "
            f"~{report.wasted_output_tokens} output tokens wasted"
        )

    with _ALLOWLIST_LOCK:
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
    account: Account,
    candidate: Candidate,
    *,
    execute: bool,
    max_budget_usd: float,
    ledger: Ledger,
    org: str,
    rtk: bool = False,
) -> None:
    repo, _number = candidate.id.split("#")
    repo_path = WORKSPACE_ROOT / repo
    branch = _branch_name(candidate)
    prompt = _prompt_for(candidate)

    # One print call, not several: with dispatches now running concurrently in
    # separate threads, individual print()s from different accounts could
    # otherwise interleave mid-block and produce unreadable output.
    print(
        f"\n=== {account.name} -> {candidate.id} ({repo}) ===\n"
        f"  branch: {branch}\n"
        f"  config: {account.config_dir}\n"
        f"  budget cap: ${max_budget_usd}\n"
        f"  prompt: {prompt}"
    )

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

    allowed_tools = _load_allowed_tools()
    if rtk:
        allowed_tools = _with_rtk_allowlist(allowed_tools)

    with Workspace(repo_path, base_ref="main") as tree:
        # -B, not -b: a leftover local branch ref from a prior aborted/no-op run
        # (the temp worktree is gone but its `checkout -b` ref persists in the
        # shared .git) must not crash a fresh dispatch. Resetting it to this
        # worktree's detached-main HEAD is safe -- any branch that still has real
        # work in flight was already filtered out by the open-PR skip in main().
        subprocess.run(["git", "-C", str(tree), "checkout", "-B", branch], check=True)
        # Snapshot the branch point now, so "did the worker commit anything?" is
        # measured against where it started -- not the `main` ref, which another
        # concurrent dispatch could advance underneath us.
        base_sha = subprocess.run(
            ["git", "-C", str(tree), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
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
                *allowed_tools,
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
            ledger=ledger, rtk=rtk,
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

        # A clean exit code is not proof of work. Gate on a real commit before
        # pushing anything: a worker that gave up (asked for an approval no one
        # could grant) exits 0 with the branch untouched, and pushing that dead
        # branch + logging "success" is the false positive this guards against.
        n_commits = int(
            subprocess.run(
                ["git", "-C", str(tree), "rev-list", "--count", f"{base_sha}..HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            or "0"
        )
        if n_commits == 0:
            outcome, msg = _dispatch_outcome(0, None)
            tail = (
                f" (worker's last words: {report.final_message[:160]!r})"
                if report.final_message
                else ""
            )
            print(f"  [{account.name}] {msg}{tail}")
            ledger.record(
                tool="fleet_dispatch",
                kind="dispatch",
                outcome=outcome,
                detail=f"{account.name} -> {candidate.id}: {msg}",
                candidate=candidate.id,
                account=account.name,
                branch=branch,
                final_message=report.final_message[:500],
                denials_count=len(report.denials),
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
            return

        # Pushed with real commits -- but the worker owns `gh pr create`, so
        # confirm a PR actually exists before calling it a success rather than
        # trusting the prompt was followed.
        pr_number = _open_pr_number(org, repo, branch)
        outcome, msg = _dispatch_outcome(n_commits, pr_number)
        print(f"  [{account.name}] pushed {branch} ({n_commits} commit(s)): {msg}")
        ledger.record(
            tool="fleet_dispatch",
            kind="dispatch",
            outcome=outcome,
            detail=f"{account.name} -> {candidate.id}: {msg}",
            candidate=candidate.id,
            account=account.name,
            branch=branch,
            commits=n_commits,
            pr_number=pr_number,
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
    parser.add_argument(
        "--rtk",
        action="store_true",
        help="enable rtk output compression: preflight-verify the rtk hook is "
        "installed in every account's config dir (never installs it — rtk's own "
        "`rtk init -g` owns that), and mirror each Bash(X) allowlist entry with "
        "Bash(rtk X) so the hook's rewritten `rtk <cmd>` commands still pass the "
        "headless worker's --allowedTools",
    )
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

    if args.rtk:
        problems = _preflight_rtk(accounts)
        if problems:
            print("rtk compression requested (--rtk) but not wired:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("rtk output-compression hook verified for every account")

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

    # Don't re-dispatch an issue that already has an open fleet-dispatch PR in
    # flight: the orchestrator ranks open issues without knowing one is already
    # being handled, and re-running it just burns an account to open a second,
    # duplicate PR for the same issue.
    in_flight = [
        c for c in dispatchable
        if _open_pr_number(args.org, c.repo, _branch_name(c)) is not None
    ]
    if in_flight:
        ids = {c.id for c in in_flight}
        names = ", ".join(sorted(ids))
        print(f"skipping (already has an open fleet-dispatch PR): {names}")
        dispatchable = [c for c in dispatchable if c.id not in ids]

    dispatch_ledger = Ledger(DISPATCH_LEDGER)
    pairs = list(zip(accounts, dispatchable))
    failures: list[tuple[Account, Candidate, BaseException]] = []
    if pairs:
        # One worker thread per account: each already runs in its own git
        # worktree under its own CLAUDE_CONFIG_DIR (mythings.isolation.Workspace),
        # so nothing about running them at the same time needs new isolation --
        # only the shared allowlist self-edit does (see _ALLOWLIST_LOCK).
        with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
            futures = {
                pool.submit(
                    _dispatch_one,
                    account,
                    candidate,
                    execute=args.execute,
                    max_budget_usd=args.max_budget_usd,
                    ledger=dispatch_ledger,
                    org=args.org,
                    rtk=args.rtk,
                ): (account, candidate)
                for account, candidate in pairs
            }
            # future.exception() blocks until that future is done but, unlike
            # future.result(), never raises -- so one account's crash can't
            # stop us from also collecting every other account's outcome.
            for future, (account, candidate) in futures.items():
                exc = future.exception()
                if exc is not None:
                    failures.append((account, candidate, exc))
    for account, candidate, exc in failures:
        print(f"  [{account.name}] {candidate.id} crashed: {exc!r}")
    for account in accounts[len(dispatchable) :]:
        print(f"\n=== {account.name}: no ready issue candidate ===")

    if not args.execute:
        print("\n(dry run — pass --execute to actually launch these sessions)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
